#!/usr/bin/env bash
# bench/peers/ab_commit.sh — A/B two BlackBull commits on the local box.
#
# compare_servers.sh answers "how does BlackBull compare to uvicorn"; this
# answers "did my commit cost anything", which needs a different shape:
#   * one stack, two code states, everything else held fixed;
#   * arms interleaved ABBA inside a single session, because a number from
#     one run differenced against a number from another run measures the
#     gap between the two sessions as much as the gap between the commits;
#   * only the files that actually differ are swapped, in place, so both
#     arms load through the same venv and the same editable install.
#
# Usage:
#   bash bench/peers/ab_commit.sh
#   REF_BASE=v0.67.0 REF_TREAT=HEAD ROUNDS=4 bash bench/peers/ab_commit.sh
#
# Env:
#   REF_BASE   baseline commit-ish            (default HEAD~1)
#   REF_TREAT  treatment commit-ish           (default HEAD)
#   PATHSPEC   limit the swapped file set     (default blackbull/)
#   ROUNDS     ABBA rounds; 4 runs each       (default 3)
#   DURATION   wrk seconds per run            (default 10)
#   WARMUP     wrk seconds before each run    (default 5, discarded)
#   THREADS    wrk threads                    (default 4)
#   CONNS      wrk connections                (default 32)
#   URL_PATH   path to hammer                 (default /plaintext)
#   PORT       bind port                      (default 8443)
#   BB_UVLOOP  0 = pure-Python identity       (default 0)
#   PIPELINE   wrk pipeline depth             (default 1 = serialized keep-alive)
#   PHASES     which phases to run            (default "null real")
#
# The `null` phase is an A/A control: it serves identical bytes under both
# labels, so its delta is known to be zero and whatever it reports is this
# box's floor.  It runs by default and in the same session as the real phase,
# because a floor recalled from another session is not this session's floor.
# A real delta smaller than the null delta is a property of the box.
#
# Output: bench/results/ab-commit-<UTC>/report.md + raw wrk logs.

set -uo pipefail

REF_BASE="${REF_BASE:-HEAD~1}"
REF_TREAT="${REF_TREAT:-HEAD}"
PATHSPEC="${PATHSPEC:-blackbull/}"
ROUNDS="${ROUNDS:-3}"
DURATION="${DURATION:-10}"
WARMUP="${WARMUP:-5}"
THREADS="${THREADS:-4}"
CONNS="${CONNS:-32}"
URL_PATH="${URL_PATH:-/plaintext}"
PORT="${PORT:-8443}"
BB_UVLOOP="${BB_UVLOOP:-0}"
BB_FORCE_ASGI_SCOPE="${BB_FORCE_ASGI_SCOPE:-0}"
PIPELINE="${PIPELINE:-1}"
PHASES="${PHASES:-null real}"
# Extra wrk header args, e.g. -H 'Accept-Encoding: gzip'.  Without an
# Accept-Encoding the Compression middleware takes its vary_send (selection is
# None) branch and the native-complete path under test is never exercised —
# both arms measure identical middleware code.  Kept unquoted at the call site
# so multi-arg header flags word-split correctly.
WRK_HEADERS="${WRK_HEADERS:-}"
# Optional wrk Lua script for non-GET lanes (e.g. bench/wrk/post_echo.lua for
# the upload body-read path).  WRK_SCRIPT_ARGS (word-split) is passed after --.
WRK_SCRIPT="${WRK_SCRIPT:-}"
WRK_SCRIPT_ARGS="${WRK_SCRIPT_ARGS:-}"
# Server and load generator on disjoint cores.  Unpinned, the two fight for
# the same cores and the throughput distribution goes bimodal (two scheduler
# placements, ~15 % apart), which swamps anything a refactor of this size
# could do.  Pinning is what makes the floor small enough to measure against.
SERVER_CPUS="${SERVER_CPUS:-0-1}"
LOAD_CPUS="${LOAD_CPUS:-4-9}"
if command -v taskset >/dev/null 2>&1; then
    PIN_SERVER=(taskset -c "$SERVER_CPUS")
    PIN_LOAD=(taskset -c "$LOAD_CPUS")
else
    PIN_SERVER=() ; PIN_LOAD=()
fi

REPO="$(git rev-parse --show-toplevel)"
cd "$REPO"

TS="$(date -u +%Y%m%d-%H%M%S)"
OUTDIR="bench/results/ab-commit-${TS}Z"
mkdir -p "$OUTDIR"
REPORT="$OUTDIR/report.md"
RAW="$OUTDIR/raw.tsv"
BASE_URL="http://127.0.0.1:${PORT}"

SHA_BASE="$(git rev-parse --short "$REF_BASE")"
SHA_TREAT="$(git rev-parse --short "$REF_TREAT")"
HEAD_REF="$(git rev-parse HEAD)"

# --- the file set under test ----------------------------------------------
mapfile -t FILES < <(git diff --name-only "$REF_BASE" "$REF_TREAT" -- "$PATHSPEC")
if [ "${#FILES[@]}" -eq 0 ]; then
    echo "ab_commit.sh: $SHA_BASE..$SHA_TREAT touch nothing under $PATHSPEC" >&2
    exit 1
fi

# Refuse to run on a dirty file set.  The restore trap puts back HEAD's
# bytes, which would silently discard uncommitted work in exactly the
# files being swapped.
for f in "${FILES[@]}"; do
    if ! git diff --quiet -- "$f" || ! git diff --cached --quiet -- "$f"; then
        echo "ab_commit.sh: $f has uncommitted changes — commit or stash first" >&2
        exit 1
    fi
done

# A file may exist in only one of the two refs (added or deleted between
# them).  `git checkout <ref> -- <path>` cannot handle a path absent from
# the target ref, so each file is checked out when it exists at the target
# and REMOVED when it does not.  `$PROOF_FILE` is the first swapped file
# that exists in BOTH refs — the import-hash proof below needs a module
# that is importable under either arm.
PROOF_FILE=""
for f in "${FILES[@]}"; do
    if git cat-file -e "$REF_BASE:$f" 2>/dev/null \
            && git cat-file -e "$REF_TREAT:$f" 2>/dev/null; then
        PROOF_FILE="$f"
        break
    fi
done

_swap_file_set() {  # $1 = ref (or HEAD_REF for restore)
    local ref="$1" f
    for f in "${FILES[@]}"; do
        if git cat-file -e "$ref:$f" 2>/dev/null; then
            git checkout "$ref" -- "$f" || return 1
        else
            # Absent at the target ref.  Removing the worktree file alone
            # leaves the earlier `git checkout`'s staged entry behind ("AD"
            # state), which trips the dirty check on the next session.
            # Unstage it too.
            git rm --cached -q --ignore-unmatch -- "$f" 2>/dev/null || true
            rm -f "$f"
        fi
    done
    find blackbull -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
    return 0
}

restore_tree() {
    _swap_file_set "$HEAD_REF" 2>/dev/null || true
}
kill_server() {
    if command -v fuser >/dev/null 2>&1; then
        fuser -k -9 -n tcp "$PORT" 2>/dev/null || true
    fi
    pkill -9 -f "bench.peers.native_app" 2>/dev/null || true
    for _ in $(seq 1 20); do
        ss -tln 2>/dev/null | grep -q ":$PORT " || return 0
        sleep 0.25
    done
}
cleanup() { kill_server; restore_tree; }
trap cleanup EXIT INT TERM HUP

# --- one arm ---------------------------------------------------------------

# Swap in a ref's bytes and prove the interpreter will import them.  An
# editable install resolves `blackbull` through a meta-path finder, so the
# only trustworthy check is asking Python for the file it actually loaded
# and hashing that, not hashing the path we think it will use.
swap_to() {
    local ref="$1"
    _swap_file_set "$ref" || return 1
    if [ -n "$PROOF_FILE" ]; then
        # Use the install.sh-provisioned venv interpreter directly: `uv run`
        # can re-resolve the project and *recreate* .venv, dropping the
        # editable blackbull install (and its console script) — the recurring
        # "Failed to spawn: blackbull" gotcha.  .venv/bin/python carries the
        # editable meta-path finder, so the swapped files are still the ones
        # imported.
        .venv/bin/python - "$PROOF_FILE" <<'PY'
import hashlib, importlib, pathlib, sys
rel = sys.argv[1]
mod = importlib.import_module(
    rel.removesuffix('.py').replace('/', '.'))
p = pathlib.Path(mod.__file__).resolve()
want = pathlib.Path(rel).resolve()
if p != want:
    print(f'IMPORT-MISMATCH {p} != {want}')
    raise SystemExit(1)
print(f'{p} {hashlib.sha1(p.read_bytes()).hexdigest()[:12]}')
PY
    fi
}

start_server() {
    BB_UVLOOP="$BB_UVLOOP" BB_WORKERS=1 BB_ACCESS_LOG=0 \
        BB_FORCE_ASGI_SCOPE="$BB_FORCE_ASGI_SCOPE" \
        setsid "${PIN_SERVER[@]}" .venv/bin/blackbull bench.peers.native_app:app \
            --bind "127.0.0.1:${PORT}" \
            >"$OUTDIR/server.log" 2>&1 &
    SERVER_PID=$!
    for _ in $(seq 1 60); do
        # Accept any HTTP 200 — the old `grep -q Hello` body probe only
        # matched /plaintext-style responses and failed binary/compressed
        # lanes (/preencoded, /1kb) with "server not ready".  Send the same
        # Accept-Encoding as wrk so the probe exercises the same middleware
        # branch the measurement will.
        if curl -s -o /dev/null --max-time 2 -w '%{http_code}' \
                $WRK_HEADERS "$BASE_URL$URL_PATH" 2>/dev/null | grep -q '^200'; then
            return 0
        fi
        sleep 0.5
    done
    echo "ab_commit.sh: server not ready on $BASE_URL" >&2
    tail -20 "$OUTDIR/server.log" >&2
    return 1
}

# One measured run.  Echoes req/s on stdout.
measure() {
    local tag="$1"
    local script_args=()
    if [ -n "$WRK_SCRIPT" ]; then
        script_args=(-s "$WRK_SCRIPT" ${WRK_SCRIPT_ARGS:+-- $WRK_SCRIPT_ARGS})
    elif [ "$PIPELINE" != "1" ]; then
        script_args=(-s bench/wrk/pipeline.lua -- "$PIPELINE")
    fi
    "${PIN_LOAD[@]}" wrk -t"$THREADS" -c"$CONNS" -d"${WARMUP}s" --latency \
        $WRK_HEADERS "$BASE_URL$URL_PATH" "${script_args[@]}" >/dev/null 2>&1
    "${PIN_LOAD[@]}" wrk -t"$THREADS" -c"$CONNS" -d"${DURATION}s" --latency \
        $WRK_HEADERS "$BASE_URL$URL_PATH" "${script_args[@]}" >"$OUTDIR/wrk_${tag}.txt" 2>&1
    awk '/Requests\/sec:/ {print $2}' "$OUTDIR/wrk_${tag}.txt"
}

run_arm() {
    local phase="$1" arm="$2" round="$3"          # arm = base|treat
    local ref tag
    if [ "$arm" = "base" ]; then ref="$REF_BASE"; else ref="$REF_TREAT"; fi
    # The null phase serves the treatment bytes under both labels.  Its delta
    # is therefore zero by construction, so whatever it reports is this box's
    # resolution floor — the number the real delta has to clear to mean
    # anything.  It is measured in this session, never recalled from another.
    [ "$phase" = "null" ] && ref="$REF_TREAT"
    tag="${phase}_r${round}_${arm}"

    kill_server
    local proof
    proof="$(swap_to "$ref")" || { echo "swap to $ref failed: $proof" >&2; return 1; }
    start_server || return 1
    local rps
    rps="$(measure "$tag")"
    kill_server
    printf '%s\t%s\t%s\t%s\t%s\n' "$phase" "$round" "$arm" "${rps:-NaN}" "$proof" >>"$RAW"
    echo "  [$phase] round $round  $arm  ${rps:-NaN} req/s   [${proof##* }]"
}

# --- drive -----------------------------------------------------------------
printf 'phase\tround\tarm\trps\tproof\n' >"$RAW"

echo "ab_commit.sh: $SHA_BASE (base) vs $SHA_TREAT (treat)"
echo "  files: ${FILES[*]}"
echo "  lane : HTTP/1.1 cleartext keep-alive, wrk -t$THREADS -c$CONNS -d${DURATION}s $URL_PATH"
echo "  uvloop=$BB_UVLOOP  rounds=$ROUNDS (ABBA)  pipeline=$PIPELINE"
echo "  pin  : server=${SERVER_CPUS:-none} load=${LOAD_CPUS:-none}"
echo "  phases: $PHASES"
echo ""

for phase in $PHASES; do
    for r in $(seq 1 "$ROUNDS"); do
        # ABBA per round cancels linear drift within the round.  The *first*
        # slot of a session is additionally cold (allocator, page cache) in a
        # way that is not linear, so the pattern flips each round — otherwise
        # one arm owns the cold slot every time and the bias survives
        # averaging.
        if [ $((r % 2)) -eq 1 ]; then
            order=(base treat treat base)
        else
            order=(treat base base treat)
        fi
        for arm in "${order[@]}"; do
            run_arm "$phase" "$arm" "$r" || exit 1
        done
    done
done

kill_server
restore_tree

# --- report ----------------------------------------------------------------
{
    echo "# A/B — $SHA_BASE (base) vs $SHA_TREAT (treat)"
    echo ""
    echo "Local box, HTTP/1.1 cleartext keep-alive, single worker."
    echo ""
    echo "| | |"
    echo "|---|---|"
    echo "| Lane | \`wrk -t$THREADS -c$CONNS -d${DURATION}s $URL_PATH\` |"
    echo "| Rounds | $ROUNDS ABBA per phase |"
    echo "| uvloop | $BB_UVLOOP |"
    echo "| scope | $BB_FORCE_ASGI_SCOPE |"
    echo "| Pinning | server \`$SERVER_CPUS\` / load \`$LOAD_CPUS\` |"
    echo "| Files swapped | \`${FILES[*]}\` |"
    echo ""
    .venv/bin/python bench/peers/ab_report.py "$RAW"
} >"$REPORT"

echo ""
cat "$REPORT"
echo ""
echo "Artefacts: $OUTDIR/"
