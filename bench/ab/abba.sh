# bench/ab/abba.sh — the ABBA core every bench/ab protocol shares.
#
# A protocol script sources this file, sets
#   AB_TOOL            the tool name its run directory is filed under
#   AB_LANE            one-line description of the load, for the report
# defines
#   ab_start_server    start the server with `ab_spawn CMD...` and return once
#                      it answers (`ab_wait_ready CURL-ARGS... URL`)
#   ab_measure TAG     run one measurement; print its rate per second
# and calls abba_main.
#
# Arms: base = REF_BASE's bytes, treat = REF_TREAT's.  Only the files under
# PATHSPEC that differ between the two are swapped, in place, and after each
# swap the interpreter is asked which file it imports and hashes it.  The
# working tree under PATHSPEC must already be REF_TREAT, so nothing outside the
# swapped set can differ from the treatment.  Identical refs compare
# BASE_EXTRA_ENV against TREAT_EXTRA_ENV on one build.
#
# Schedule: per phase, ROUNDS rounds of base treat treat base, mirrored on even
# rounds so neither arm owns the cold first slot.  The null phase serves the
# treatment under both labels — its Δ is 0 by construction, so what it reports
# is this session's resolution floor.
#
# Env: REF_BASE(=HEAD~1) REF_TREAT(=HEAD) PATHSPEC(=blackbull/) ROUNDS(=3)
#      PHASES(=null real) PORT(=8443) BB_UVLOOP(=0) BB_FORCE_ASGI_SCOPE(=0)
#      BB_WORKERS(=1) BASE_EXTRA_ENV TREAT_EXTRA_ENV
#      SERVER_CPUS(=0-1) LOAD_CPUS(=4-9) — empty disables pinning
#      PY(=.venv/bin/python)

set -uo pipefail

REF_BASE="${REF_BASE:-HEAD~1}"
REF_TREAT="${REF_TREAT:-HEAD}"
PATHSPEC="${PATHSPEC:-blackbull/}"
ROUNDS="${ROUNDS:-3}"
PHASES="${PHASES:-null real}"
PORT="${PORT:-8443}"
BB_UVLOOP="${BB_UVLOOP:-0}"
BB_FORCE_ASGI_SCOPE="${BB_FORCE_ASGI_SCOPE:-0}"
BB_WORKERS="${BB_WORKERS:-1}"
BASE_EXTRA_ENV="${BASE_EXTRA_ENV:-}"
TREAT_EXTRA_ENV="${TREAT_EXTRA_ENV:-}"
# Disjoint cores for server and load generator; shared cores make the
# throughput distribution bimodal and swamp a refactor-sized delta.
SERVER_CPUS="${SERVER_CPUS-0-1}"
LOAD_CPUS="${LOAD_CPUS-4-9}"
PIN_SERVER=() ; PIN_LOAD=()
if command -v taskset >/dev/null 2>&1; then
    [ -z "$SERVER_CPUS" ] || PIN_SERVER=(taskset -c "$SERVER_CPUS")
    [ -z "$LOAD_CPUS" ] || PIN_LOAD=(taskset -c "$LOAD_CPUS")
fi

REPO="$(git rev-parse --show-toplevel)" || exit 1
cd "$REPO" || exit 1
# The venv's interpreter, not `uv run`: uv may recreate the venv mid-run and
# drop the editable install the swap relies on.
PY="${PY:-$REPO/.venv/bin/python}"

_ab_die() { echo "${AB_TOOL:-ab}: $*" >&2; exit 1; }

# Start the arm's server in its own session, with the arm's environment and
# the server pinning; _ab_kill_server stops exactly that process group.
ab_spawn() {
    # shellcheck disable=SC2086  # ARM_ENV is a word list
    env $ARM_ENV setsid "${PIN_SERVER[@]}" "$@" > "$AB_RUN/server.log" 2>&1 &
    AB_SERVER_PID=$!
}

ab_wait_ready() {  # $@ = curl arguments ending in the URL
    for _ in $(seq 1 60); do
        if curl -sk -o /dev/null --max-time 2 -w '%{http_code}' "$@" 2>/dev/null \
                | grep -q '^200'; then
            return 0
        fi
        sleep 0.5
    done
    echo "$AB_TOOL: server not ready (${*: -1})" >&2
    tail -20 "$AB_RUN/server.log" >&2 2>/dev/null
    return 1
}

_ab_kill_server() {
    if [ -n "${AB_SERVER_PID:-}" ]; then
        kill -9 -- "-$AB_SERVER_PID" 2>/dev/null || true
        AB_SERVER_PID=
    fi
    if command -v fuser >/dev/null 2>&1; then
        fuser -k -9 -n tcp "$PORT" 2>/dev/null || true
    fi
    for _ in $(seq 1 20); do
        ss -tln 2>/dev/null | grep -q ":$PORT " || return 0
        sleep 0.25
    done
}

_ab_swap() {  # $1 = commit
    local f
    for f in "${AB_FILES[@]}"; do
        if git cat-file -e "$1:$f" 2>/dev/null; then
            git checkout "$1" -- "$f" || return 1
        else
            # Absent at this ref: unstage too, or the next dirty check trips
            # over the "AD" entry the earlier checkout left behind.
            git rm --cached -q --ignore-unmatch -- "$f" 2>/dev/null || true
            rm -f "$f"
        fi
    done
    find "${PATHSPEC%%/*}" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
}

# Swap, then ask Python which file it imports — an editable install resolves
# through a meta-path finder, so only the loaded file's hash is proof.
_ab_swap_and_prove() {
    _ab_swap "$1" || return 1
    "$PY" - "$AB_PROOF_FILE" <<'PY'
import hashlib, importlib, pathlib, sys
rel = sys.argv[1]
mod = importlib.import_module(rel.removesuffix('.py').replace('/', '.'))
p = pathlib.Path(mod.__file__).resolve()
if p != pathlib.Path(rel).resolve():
    print(f'IMPORT-MISMATCH {p} != {pathlib.Path(rel).resolve()}')
    raise SystemExit(1)
print(f'{p} {hashlib.sha1(p.read_bytes()).hexdigest()[:12]}')
PY
}

_ab_cleanup() {
    _ab_kill_server
    _ab_swap "$SHA_TREAT" 2>/dev/null || true
}

_ab_arm() {  # $1 phase, $2 arm, $3 round
    local ref="$SHA_BASE" extra="$BASE_EXTRA_ENV" tag="$1_r$3_$2" proof value
    if [ "$2" = treat ] || [ "$1" = null ]; then ref="$SHA_TREAT"; fi
    if [ "$2" = treat ]; then extra="$TREAT_EXTRA_ENV"; fi
    ARM_ENV="BB_UVLOOP=$BB_UVLOOP BB_FORCE_ASGI_SCOPE=$BB_FORCE_ASGI_SCOPE"
    ARM_ENV+=" BB_WORKERS=$BB_WORKERS BB_ACCESS_LOG=0 $extra"
    _ab_kill_server
    proof="$(_ab_swap_and_prove "$ref")" || { echo "$AB_TOOL: swap to $ref failed: $proof" >&2; return 1; }
    ab_start_server || return 1
    value="$(ab_measure "$tag")"
    _ab_kill_server
    printf '%s\t%s\t%s\t%s\t%s\n' "$1" "$3" "$2" "${value:-NaN}" "$proof" >> "$AB_RUN/raw.tsv"
    echo "  [$1] round $3  $2  ${value:-NaN}/s   [${proof##* }]"
}

# EC2 Nitro instances name themselves in DMI: vendor "Amazon EC2", product
# the instance type — no metadata-service token needed.
_ab_host() {
    if grep -qi 'amazon ec2' /sys/class/dmi/id/sys_vendor 2>/dev/null; then
        printf 'EC2 %s (nproc=%s)' "$(cat /sys/class/dmi/id/product_name)" "$(nproc)"
    else
        printf 'local %s (nproc=%s)' "$(uname -sr)" "$(nproc)"
    fi
}

abba_main() {
    [[ "$ROUNDS" =~ ^[1-9][0-9]*$ ]] || _ab_die "ROUNDS must be a positive integer"
    local phase
    for phase in $PHASES; do
        [ "$phase" = null ] || [ "$phase" = real ] || _ab_die "PHASES may name only null and real"
    done
    SHA_BASE="$(git rev-parse --verify -q "$REF_BASE^{commit}")" || _ab_die "unknown REF_BASE $REF_BASE"
    SHA_TREAT="$(git rev-parse --verify -q "$REF_TREAT^{commit}")" || _ab_die "unknown REF_TREAT $REF_TREAT"
    git diff --quiet "$SHA_TREAT" -- "$PATHSPEC" || _ab_die \
        "the working tree under $PATHSPEC differs from REF_TREAT ($REF_TREAT) — check out REF_TREAT and commit or stash uncommitted changes"

    mapfile -t AB_FILES < <(git diff --name-only "$SHA_BASE" "$SHA_TREAT" -- "$PATHSPEC")
    if [ "${#AB_FILES[@]}" -eq 0 ]; then
        [ "$SHA_BASE" = "$SHA_TREAT" ] && [ "$BASE_EXTRA_ENV" != "$TREAT_EXTRA_ENV" ] || _ab_die \
            "REF_BASE and REF_TREAT do not differ under $PATHSPEC (identical refs need differing BASE_EXTRA_ENV / TREAT_EXTRA_ENV)"
    fi
    AB_PROOF_FILE=""
    local f
    for f in "${AB_FILES[@]}"; do
        if [[ "$f" == *.py ]] && git cat-file -e "$SHA_BASE:$f" 2>/dev/null \
                && git cat-file -e "$SHA_TREAT:$f" 2>/dev/null; then
            AB_PROOF_FILE="$f"; break
        fi
    done
    [ -n "$AB_PROOF_FILE" ] || AB_PROOF_FILE="$(git ls-files -- "$PATHSPEC" | grep '\.py$' \
        | grep -v '__init__\.py$' | head -1)"
    [ -n "$AB_PROOF_FILE" ] || _ab_die "no Python module under $PATHSPEC to prove the swap with"

    source "$REPO/bench/lib/results.sh"
    bench_run_dir "$AB_TOOL" ref_base="$SHA_BASE" ref_treat="$SHA_TREAT" pathspec="$PATHSPEC" \
        rounds="$ROUNDS" phases="$PHASES" lane="$AB_LANE" uvloop="$BB_UVLOOP" \
        scope="$BB_FORCE_ASGI_SCOPE" workers="$BB_WORKERS" base_env="$BASE_EXTRA_ENV" \
        treat_env="$TREAT_EXTRA_ENV" server_cpus="$SERVER_CPUS" load_cpus="$LOAD_CPUS" || exit 1
    AB_RUN="$BENCH_RUN_DIR"
    trap _ab_cleanup EXIT INT TERM HUP

    printf 'phase\tround\tarm\trps\tproof\n' > "$AB_RUN/raw.tsv"
    echo "$AB_TOOL: ${SHA_BASE:0:8} (base) vs ${SHA_TREAT:0:8} (treat) -> $AB_RUN"
    echo "  lane : $AB_LANE"
    echo "  files: ${AB_FILES[*]:-<none: $BASE_EXTRA_ENV vs $TREAT_EXTRA_ENV>}"
    local r arm order
    for phase in $PHASES; do
        for r in $(seq 1 "$ROUNDS"); do
            if [ $((r % 2)) -eq 1 ]; then order=(base treat treat base)
            else order=(treat base base treat); fi
            for arm in "${order[@]}"; do
                _ab_arm "$phase" "$arm" "$r" || exit 1
            done
        done
    done
    _ab_cleanup

    {
        echo "# A/B — ${SHA_BASE:0:8} (base) vs ${SHA_TREAT:0:8} (treat)"
        echo ""
        echo "$(_ab_host), $BB_WORKERS worker(s)."
        echo ""
        echo "| | |"
        echo "|---|---|"
        echo "| Lane | \`$AB_LANE\` |"
        echo "| Rounds | $ROUNDS ABBA per phase ($PHASES) |"
        echo "| uvloop / scope | $BB_UVLOOP / $BB_FORCE_ASGI_SCOPE |"
        echo "| Pinning | server \`${SERVER_CPUS:-none}\` / load \`${LOAD_CPUS:-none}\` |"
        echo "| Swapped | \`${AB_FILES[*]:-none}\` |"
        echo "| Arm env | base \`${BASE_EXTRA_ENV:-—}\` / treat \`${TREAT_EXTRA_ENV:-—}\` |"
        echo ""
        "$PY" "$REPO/bench/ab/report.py" "$AB_RUN/raw.tsv"
    } > "$AB_RUN/report.md"
    cat "$AB_RUN/report.md"
}
