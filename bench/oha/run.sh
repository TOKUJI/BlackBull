#!/usr/bin/env bash
# bench/oha/run.sh — drive HTTP/1.1 scenarios B1-B7 from CHARACTERIZATION.md
# using oha (granian-style methodology).
#
# Differs from wrk in tool and methodology:
#   - oha uses fixed connection count + duration (like wrk)
#   - oha reports P50/P95/P99/P99.9 natively via HdrHistogram-style output
#   - oha supports HTTP/2 via -2 flag (we use HTTP/1.1 here; lane A uses h2load)
#   - --insecure accepts self-signed certs (wrk needs env vars)
#
# Granian methodology: primer (4s c8) → warmup (3s) → measure 10s.
# We use a longer 30s measurement because WSL2 loopback is noisy.
#
# Usage:
#   BASE=https://localhost:8443 OUTDIR=bench/results bash bench/oha/run.sh
#
# Env:
#   BASE      target URL          (default https://localhost:8443)
#   OUTDIR    raw-log directory   (default bench/results)
#   LABEL     stack label         (default <none> — header only)
#   DURATION  per-scenario time   (default 30 — seconds)

# NOTE: no `set -e`. oha may fail (target dies, connection refused);
# individual scenarios already wrap their result in an `err` row, and
# we don't want one scenario crash to abort the whole stack's run.

BASE="${BASE:-https://localhost:8443}"
OUTDIR="${OUTDIR:-bench/results}"
LABEL="${LABEL:-}"
LABEL_PREFIX="${LABEL_PREFIX:-}"
D="${DURATION:-30}"

mkdir -p "$OUTDIR"

# --insecure: accept self-signed/mkcert TLS
# --output-format json: machine-readable
# --no-tui: disable real-time UI
OHA="oha --insecure --no-tui --output-format json"

primer() {
    oha --insecure --no-tui -c 8 -z 4s "$1" >/dev/null 2>&1 || true
}

run_one() {
    local label="$1" conns="$2" body_args="$3" url="$4"
    local logfile="$OUTDIR/oha_${LABEL_PREFIX}${label}.json"
    # warmup at scenario concurrency
    oha --insecure --no-tui -c "$conns" -z 3s $body_args "$url" >/dev/null 2>&1 || true
    # measurement
    $OHA -c "$conns" -z "${D}s" $body_args "$url" > "$logfile" 2>/dev/null || true

    python3 - "$logfile" "$label" <<'PYEOF'
import json, sys
try:
    d = json.loads(open(sys.argv[1]).read())
    s = d['summary']
    l = d['latencyPercentiles']
    print(f"| {sys.argv[2]} | "
          f"{s['requestsPerSec']:.0f} | "
          f"{s['average'] * 1000:.2f}ms | "
          f"{l['p50'] * 1000:.2f}ms | "
          f"{l['p99'] * 1000:.2f}ms | "
          f"{s['slowest'] * 1000:.2f}ms |")
except Exception as e:
    print(f"| {sys.argv[2]} | err | err | err | err | err |  ({e})")
PYEOF
}

# Header
echo "| Scenario | req/s | mean lat | p50 | p99 | max |"
echo "|---|---|---|---|---|---|"

# initial primer per granian methodology
primer "$BASE/plaintext"

# B1 — /plaintext (no pipeline; oha doesn't support pipelining)
run_one "B1_plaintext_c256"   256 ""                            "$BASE/plaintext"
# B3 — /json
run_one "B3_json_c256"        256 ""                            "$BASE/json"
# B4 — /16kb
run_one "B4_16kb_c100"        100 ""                            "$BASE/16kb"
# B5 — /64kb
run_one "B5_64kb_c50"          50 ""                            "$BASE/64kb"
# B6 + B7 — /echo POST. Skipped for static-only references (nginx).
if [ "${SKIP_POST:-0}" != "1" ]; then
    # B6 — 1 KiB body via -D file (oha's -d string mangles binary bytes).
    B6_BODY_FILE="/tmp/oha_1kb.bin"
    head -c 1024 /dev/urandom > "$B6_BODY_FILE"
    run_one "B6_echo_1kb"         100 "-m POST -T application/octet-stream -D $B6_BODY_FILE" "$BASE/echo"
    rm -f "$B6_BODY_FILE"
    # B7 — 100 KiB body.
    B7_BODY_FILE="/tmp/oha_100kb.bin"
    head -c 102400 /dev/urandom > "$B7_BODY_FILE"
    run_one "B7_echo_100kb"        50 "-m POST -T application/octet-stream -D $B7_BODY_FILE" "$BASE/echo"
    rm -f "$B7_BODY_FILE"
fi
