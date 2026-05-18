#!/usr/bin/env bash
# bench/benchmark.sh — comprehensive single-command benchmark entry point.
#
# Usage:
#   bash bench/benchmark.sh               # full run → bench/results/YYYYMMDD-HHMMSS.md
#   bash bench/benchmark.sh --no-profile  # skip py-spy
#   bash bench/benchmark.sh --quick       # RUNS=1, K6_RUNS=1 (smoke-check)
#
# Env overrides:
#   RUNS=N     h2load passes per scenario (default 3)
#   K6_RUNS=N  k6 stress repetitions (default 3)

set -e

BASE="https://localhost:8443"
RESULT_DIR="bench/results"
mkdir -p "$RESULT_DIR"

export TIMESTAMP="$(date +%Y%m%d-%H%M%S)"
OUTFILE="$RESULT_DIR/${TIMESTAMP}.md"

DO_PROFILE=1
export RUNS="${RUNS:-3}"
export K6_RUNS="${K6_RUNS:-3}"

for arg in "$@"; do
    case "$arg" in
        --no-profile) DO_PROFILE=0 ;;
        --quick)      RUNS=1; K6_RUNS=1 ;;
    esac
done

# --- helpers ---
need() {
    command -v "$1" >/dev/null 2>&1 || { echo "ERROR: $1 not found. Run: bash bench/install.sh"; exit 1; }
}

# Locate mkcert CA (same logic as h2load_run.sh)
MKCERT_CA=""
for path in \
    /mnt/c/Users/*/AppData/Local/mkcert/rootCA.pem \
    "$HOME/.local/share/mkcert/rootCA.pem" \
    /usr/local/share/ca-certificates/mkcert-rootCA.crt; do
    # shellcheck disable=SC2086
    found=$(ls $path 2>/dev/null | head -1)
    if [ -n "$found" ]; then
        MKCERT_CA="$found"
        break
    fi
done
[ -n "$MKCERT_CA" ] && export SSL_CERT_FILE="$MKCERT_CA"

SEP="$(printf '=%.0s' {1..60})"
echo "$SEP"
echo "BlackBull benchmark — $TIMESTAMP"
echo "RUNS=$RUNS  K6_RUNS=$K6_RUNS  profile=$DO_PROFILE"
echo "$SEP"

# --- 1. Verify server is reachable ---
echo ""
echo "Checking server at $BASE/ping ..."
if ! curl -sk --max-time 5 "$BASE/ping" >/dev/null 2>&1; then
    echo "ERROR: server not reachable at $BASE"
    echo "  Start it with: python bench/app.py --port 8443 --cert cert.pem --key key.pem"
    exit 1
fi
echo "Server OK."

# --- 2. Query /config for actual worker count ---
_CFG=$(curl -sk "$BASE/config" 2>/dev/null)
if [ -n "$_CFG" ]; then
    _workers=$(echo "$_CFG" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['workers'])" 2>/dev/null)
    _uvloop=$(echo  "$_CFG" | python3 -c "import sys,json; d=json.load(sys.stdin); print(1 if d['uvloop'] else 0)" 2>/dev/null)
    _s1w=$(echo     "$_CFG" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['h2_active_streams_1w'])" 2>/dev/null)
    _snw=$(echo     "$_CFG" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['h2_active_streams'])" 2>/dev/null)
    echo "Config: workers=$_workers  uvloop=$_uvloop  streams_1w=$_s1w  streams_nw=$_snw"
else
    echo "WARNING: /config unreachable — falling back to env vars"
    _workers="${BB_WORKERS:-?}"; _uvloop="${BB_UVLOOP:-?}"
    _s1w="${BB_H2_ACTIVE_STREAMS_1W:-?}"; _snw="${BB_H2_ACTIVE_STREAMS:-?}"
fi

# Pre-write environment section to OUTFILE; h2load_run.sh appends its own
# summary to a file with the same name — both use the same TIMESTAMP so the
# summarize.py output overwrites this file.  We inject the config line below.
# (h2load_run.sh's summarize.py call recreates the .md; we inject a preamble
#  after it completes.)

# --- 3. Global warmup ---
echo ""
echo "Global warmup (2000 reqs) ..."
need h2load
h2load -n 2000 -c 10 -m 10 "$BASE/ping" >/dev/null 2>&1 || true
echo "Warmup done."

# --- 4. h2load scenarios ---
echo ""
echo "Running h2load scenarios (RUNS=$RUNS) ..."
TIMESTAMP="$TIMESTAMP" RUNS="$RUNS" bash bench/h2load_run.sh

# h2load_run.sh created bench/results/$TIMESTAMP.md via summarize.py.
# Inject a "run info" line at the top so it is visible in the report.
H2_MD="$RESULT_DIR/${TIMESTAMP}.md"
if [ -f "$H2_MD" ]; then
    OUTFILE="$H2_MD"
    # Prepend config/runs line after the title
    python3 - "$OUTFILE" "$_workers" "$_uvloop" "$_s1w" "$_snw" "$RUNS" "$K6_RUNS" <<'PYEOF'
import sys
path, workers, uvloop, s1w, snw, runs, k6runs = sys.argv[1:]
lines = open(path).read().splitlines()
insert = [
    "",
    f"> workers={workers}  uvloop={uvloop}  streams_1w={s1w}  streams_nw={snw}"
    f"  |  h2load RUNS={runs}  K6_RUNS={k6runs}",
    "",
]
# Insert after the first line (the # heading)
result = [lines[0]] + insert + lines[1:]
open(path, "w").write("\n".join(result) + "\n")
PYEOF
fi

# --- 5. k6 scenarios ---
echo ""
echo "Running k6 scenarios (K6_RUNS=$K6_RUNS) ..."
need k6
TIMESTAMP="$TIMESTAMP" K6_RUNS="$K6_RUNS" bash bench/k6_run.sh "$OUTFILE"

# --- 6. Event loop lag snapshot ---
echo ""
echo "Fetching /metrics ..."
_METRICS=$(curl -sk --max-time 5 "$BASE/metrics" 2>/dev/null || true)
if [ -n "$_METRICS" ]; then
    {
        printf '\n---\n\n## Event loop lag\n\n```\n'
        echo "$_METRICS"
        printf '```\n\n'
    } >> "$OUTFILE"
    echo "Metrics appended."
else
    echo "WARNING: /metrics not reachable — skipping."
fi

# --- 7. py-spy profile (optional) ---
if [ "$DO_PROFILE" -eq 1 ]; then
    SVG="$RESULT_DIR/profile_stress_${TIMESTAMP}.svg"
    if command -v py-spy >/dev/null 2>&1; then
        echo ""
        echo "py-spy profile: attach to running server for 30 s during load ..."
        echo "  (Run manually: py-spy record --duration 30 --rate 200 --output $SVG --pid <server-pid>)"
        {
            printf '\n---\n\n## Profile\n\n'
            printf '_Attach py-spy manually during load:_\n```\n'
            printf 'py-spy record --duration 30 --rate 200 --output %s --pid <server-pid>\n' "$SVG"
            printf '```\n\n'
        } >> "$OUTFILE"
    else
        echo "py-spy not found — skipping profile (install with: pip install py-spy)"
    fi
fi

echo ""
echo "$SEP"
echo "Summary: $OUTFILE"
echo "$SEP"
