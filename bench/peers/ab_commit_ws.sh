#!/usr/bin/env bash
# bench/peers/ab_commit_ws.sh — WebSocket-lane A/B of two BlackBull commits.
#
# The WebSocket analogue of ab_commit.sh.  The four-row ship rule
# (reduce-loop-exposure.md §5) needs a throughput measurement on the WS lane,
# not just the machine-independent loop-touch count — ab_commit.sh is
# HTTP/1.1-only, so it cannot see a WS-local change.  This harness reuses the
# exact ab-commit discipline:
#   * one stack, two code states, everything else held fixed;
#   * arms interleaved ABBA inside a single session;
#   * only the files that actually differ are swapped, in place, with an
#     import-hash proof that the interpreter really loaded the ref's bytes;
#   * a `null` phase serving identical bytes under both labels, measured in
#     the same session, so the box's resolution floor is known in-session.
#
# Lane: WebSocket echo, k6 floods N concurrent connections (bench/k6/
# websocket_echo_throughput.js); the server's echo rate (msg/s, the
# `ws_echoed` counter's rate) is the measured axis.
#
# Usage:
#   bash bench/peers/ab_commit_ws.sh
#   REF_BASE=HEAD~1 REF_TREAT=HEAD ROUNDS=4 bash bench/peers/ab_commit_ws.sh
#
# Env (ab_commit.sh shared):
#   REF_BASE / REF_TREAT / PATHSPEC / ROUNDS / PORT / BB_UVLOOP / PHASES
#   SERVER_CPUS / LOAD_CPUS
# PATHSPEC defaults to blackbull/ — the bench app is the instrument and is
# never swapped (both arms serve the working tree's app).
# WS-specific:
#   WS_VUS        concurrent WS connections   (default 100)
#   WS_DURATION   k6 run length               (default 10s)
#   WS_TICK_MS    message burst period        (default 1)
#   WS_BURST      messages per tick           (default 8)
#   WS_LIFETIME_MS socket lifetime            (default 8000)
#
# Output: bench/results/ab-ws-<UTC>/report.md + raw.tsv + k6 summaries.

set -uo pipefail

REF_BASE="${REF_BASE:-HEAD~1}"
REF_TREAT="${REF_TREAT:-HEAD}"
# The bench app is the MEASURING INSTRUMENT, so it must be byte-identical on
# both arms — exactly like wrk/k6.  Swapping it between refs changes the
# instrument mid-A/B (the arms differ in more than one variable), and a
# commit that fixed the app's WS route would otherwise leave the base arm
# unable to echo at all.  Swap only the framework.
PATHSPEC="${PATHSPEC:-blackbull/}"
ROUNDS="${ROUNDS:-3}"
PORT="${PORT:-8443}"
BB_UVLOOP="${BB_UVLOOP:-0}"
PHASES="${PHASES:-null real}"
WS_VUS="${WS_VUS:-100}"
WS_DURATION="${WS_DURATION:-10s}"
WS_TICK_MS="${WS_TICK_MS:-1}"
WS_BURST="${WS_BURST:-8}"
WS_LIFETIME_MS="${WS_LIFETIME_MS:-8000}"
# Server and load generator on disjoint cores — same bimodality argument as
# ab_commit.sh: unpinned, the two fight for the same cores and the
# distribution goes bimodal, swamping a refactor-scale delta.
SERVER_CPUS="${SERVER_CPUS:-0-1}"
LOAD_CPUS="${LOAD_CPUS:-4-9}"
if command -v taskset >/dev/null 2>&1; then
    PIN_SERVER=(taskset -c "$SERVER_CPUS")
    PIN_LOAD=(taskset -c "$LOAD_CPUS")
else
    PIN_SERVER=() ; PIN_LOAD=()
fi

# Python / blackbull entry points resolved ONCE, directly from the repo's
# venv.  Not `uv run` (ab_commit.sh's historical form): `uv run python`
# can recreate the venv and drop the `blackbull` console script mid-harness
# ("Failed to spawn"), which is a silent no-op failure on the very first
# arm.  Direct .venv paths are deterministic across the whole run.
REPO="$(git rev-parse --show-toplevel)"
PY="${PY:-$REPO/.venv/bin/python}"
BB="${BB:-$REPO/.venv/bin/blackbull}"
for _bin in "$PY" "$BB"; do
    if [ ! -x "$_bin" ]; then
        echo "ab_commit_ws.sh: missing $_bin — is the venv installed?" >&2
        exit 1
    fi
done

cd "$REPO"

TS="$(date -u +%Y%m%d-%H%M%S)"
OUTDIR="bench/results/ab-ws-${TS}Z"
mkdir -p "$OUTDIR"
REPORT="$OUTDIR/report.md"
RAW="$OUTDIR/raw.tsv"
BASE_URL="http://127.0.0.1:${PORT}"
WS_URL="ws://127.0.0.1:${PORT}/ws"

SHA_BASE="$(git rev-parse --short "$REF_BASE")"
SHA_TREAT="$(git rev-parse --short "$REF_TREAT")"
HEAD_REF="$(git rev-parse HEAD)"

# --- the file set under test ----------------------------------------------
mapfile -t FILES < <(git diff --name-only "$REF_BASE" "$REF_TREAT" -- $PATHSPEC)
if [ "${#FILES[@]}" -eq 0 ]; then
    echo "ab_commit_ws.sh: $SHA_BASE..$SHA_TREAT touch nothing under $PATHSPEC" >&2
    exit 1
fi

for f in "${FILES[@]}"; do
    if ! git diff --quiet -- "$f" || ! git diff --cached --quiet -- "$f"; then
        echo "ab_commit_ws.sh: $f has uncommitted changes — commit or stash first" >&2
        exit 1
    fi
done

restore_tree() {
    git checkout "$HEAD_REF" -- "${FILES[@]}" 2>/dev/null || true
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

swap_to() {
    local ref="$1"
    git checkout "$ref" -- "${FILES[@]}" || return 1
    find blackbull -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
    # Prove the interpreter loaded the ref's bytes for the framework, not the
    # bench app: the A/B measures a blackbull/ change, so the proof must cover
    # it.  ``git diff --name-only`` lists paths in name order, which would
    # put ``bench/peers/native_app.py`` first; pick the first blackbull/ file
    # instead.
    local proof_file=""
    for f in "${FILES[@]}"; do
        if [[ "$f" == blackbull/* ]]; then
            proof_file="$f"
            break
        fi
    done
    [ -n "$proof_file" ] || proof_file="${FILES[0]}"
    "$PY" - "$proof_file" <<'PY'
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
}

start_server() {
    BB_UVLOOP="$BB_UVLOOP" BB_WORKERS=1 BB_ACCESS_LOG=0 \
        setsid "${PIN_SERVER[@]}" "$BB" bench.peers.native_app:app \
            --bind "127.0.0.1:${PORT}" \
            >"$OUTDIR/server.log" 2>&1 &
    SERVER_PID=$!
    for _ in $(seq 1 60); do
        if curl -s --max-time 2 "$BASE_URL/plaintext" 2>/dev/null | grep -q Hello; then
            return 0
        fi
        sleep 0.5
    done
    echo "ab_commit_ws.sh: server not ready on $BASE_URL" >&2
    tail -20 "$OUTDIR/server.log" >&2
    return 1
}

# One measured run.  Echoes msg/s on stdout.
measure() {
    local tag="$1"
    local k6json="$OUTDIR/k6_${tag}.json"
    "${PIN_LOAD[@]}" k6 run --quiet --summary-export "$k6json" \
        --env WS_URL="$WS_URL" \
        --env WS_VUS="$WS_VUS" --env WS_DURATION="$WS_DURATION" \
        --env WS_TICK_MS="$WS_TICK_MS" --env WS_BURST="$WS_BURST" \
        --env WS_LIFETIME_MS="$WS_LIFETIME_MS" \
        bench/k6/websocket_echo_throughput.js >"$OUTDIR/k6_${tag}.log" 2>&1
    "$PY" - "$k6json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
try:
    print(d['metrics']['ws_echoed']['rate'])
except KeyError:
    print('NaN')
PY
}

run_arm() {
    local phase="$1" arm="$2" round="$3"          # arm = base|treat
    local ref tag
    if [ "$arm" = "base" ]; then ref="$REF_BASE"; else ref="$REF_TREAT"; fi
    # The null phase serves the treatment bytes under both labels.  Its delta
    # is therefore zero by construction, so whatever it reports is this box's
    # resolution floor — measured in this session, never recalled.
    [ "$phase" = "null" ] && ref="$REF_TREAT"
    tag="${phase}_r${round}_${arm}"

    kill_server
    local proof
    proof="$(swap_to "$ref")" || { echo "swap to $ref failed: $proof" >&2; return 1; }
    start_server || return 1
    local msgs
    msgs="$(measure "$tag")"
    kill_server
    printf '%s\t%s\t%s\t%s\t%s\n' "$phase" "$round" "$arm" "${msgs:-NaN}" "$proof" >>"$RAW"
    echo "  [$phase] round $round  $arm  ${msgs:-NaN} msg/s   [${proof##* }]"
}

# --- drive -----------------------------------------------------------------
printf 'phase\tround\tarm\trps\tproof\n' >"$RAW"

echo "ab_commit_ws.sh: $SHA_BASE (base) vs $SHA_TREAT (treat)"
echo "  files: ${FILES[*]}"
echo "  lane : WebSocket echo, k6 -$WS_VUS conns -d$WS_DURATION (tick ${WS_TICK_MS}ms x${WS_BURST}) -> msg/s"
echo "  uvloop=$BB_UVLOOP  rounds=$ROUNDS (ABBA)"
echo "  pin  : server=${SERVER_CPUS:-none} load=${LOAD_CPUS:-none}"
echo "  phases: $PHASES"
echo ""

for phase in $PHASES; do
    for r in $(seq 1 "$ROUNDS"); do
        # ABBA per round cancels linear drift within the round; the pattern
        # flips each round so one arm never owns the cold first slot.
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
    echo "# WS A/B — $SHA_BASE (base) vs $SHA_TREAT (treat)"
    echo ""
    echo "Local box, WebSocket echo (k6 flood), single worker."
    echo ""
    echo "| | |"
    echo "|---|---|"
    echo "| Lane | k6 \`-vus $WS_VUS -d$WS_DURATION\`, \`ws_echoed\` msg/s |"
    echo "| Rounds | $ROUNDS ABBA per phase |"
    echo "| uvloop | $BB_UVLOOP |"
    echo "| Pinning | server \`$SERVER_CPUS\` / load \`$LOAD_CPUS\` |"
    echo "| Files swapped | \`${FILES[*]}\` |"
    echo ""
    "$PY" bench/peers/ab_report.py "$RAW"
} >"$REPORT"

echo ""
cat "$REPORT"
echo ""
echo "Artefacts: $OUTDIR/"
