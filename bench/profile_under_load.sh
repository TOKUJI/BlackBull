#!/usr/bin/env bash
# bench/profile_under_load.sh — capture a py-spy flame graph during k6 stress load.
#
# Launches the server *through* py-spy so no sudo/ptrace is needed.
# Kills any existing bench/app.py process first.
#
# Usage:
#   BB_WORKERS=1 BB_UVLOOP=1 bash bench/profile_under_load.sh

set -e

BASE="https://localhost:8443"
RESULT_DIR="bench/results"
mkdir -p "$RESULT_DIR"
TS="$(date +%Y%m%d-%H%M%S)"
SVG="$RESULT_DIR/profile_stress_${TS}.svg"
K6_JSON="$RESULT_DIR/k6_stress_profile_${TS}.json"

CERT="${CERT:-tests/cert.pem}"
KEY="${KEY:-tests/key.pem}"

# Locate mkcert CA
MKCERT_CA=""
for path in \
    /mnt/c/Users/*/AppData/Local/mkcert/rootCA.pem \
    "$HOME/.local/share/mkcert/rootCA.pem" \
    /usr/local/share/ca-certificates/mkcert-rootCA.crt; do
    # shellcheck disable=SC2086
    found=$(ls $path 2>/dev/null | head -1)
    [ -n "$found" ] && { MKCERT_CA="$found"; break; }
done
[ -n "$MKCERT_CA" ] && export SSL_CERT_FILE="$MKCERT_CA"

# --- stop any existing bench server ---
EXISTING=$(pgrep -f "bench/app.py" | head -1)
if [ -n "$EXISTING" ]; then
    echo "Stopping existing server (PID $EXISTING) ..."
    kill "$EXISTING" 2>/dev/null || true
    sleep 1
fi

# py-spy duration: 15s warmup window + 90s stress = 105s total
PY_SPY_DURATION=105

echo "Starting server under py-spy (duration=${PY_SPY_DURATION}s, rate=200 Hz) ..."
echo "  SVG → $SVG"
BB_WORKERS="${BB_WORKERS:-1}" BB_UVLOOP="${BB_UVLOOP:-1}" \
py-spy record \
    --duration "$PY_SPY_DURATION" \
    --rate 200 \
    --output "$SVG" \
    -- python bench/app.py --port 8443 --cert "$CERT" --key "$KEY" &
PY_SPY_PID=$!

# --- wait for server to start ---
echo "Waiting for server to become ready ..."
for i in $(seq 1 20); do
    if curl -sk --max-time 2 "$BASE/ping" >/dev/null 2>&1; then
        echo "Server ready (${i}s)."
        break
    fi
    sleep 1
done
curl -sk --max-time 3 "$BASE/ping" >/dev/null || { echo "ERROR: server not ready"; kill "$PY_SPY_PID" 2>/dev/null; exit 1; }

# --- warmup ---
echo "Warmup (5000 reqs) ..."
h2load -n 5000 -c 10 -m 10 "$BASE/ping" >/dev/null 2>&1 || true
echo "Warmup done. Starting k6 stress now (500 VU, 90 s) ..."

# --- k6 stress ---
k6 run \
    --summary-export="$K6_JSON" \
    --summary-trend-stats="p(50),p(95),p(99),max" \
    bench/k6/http_stress.js &
K6_PID=$!

wait "$K6_PID" || true
echo "k6 done."

# --- event loop lag ---
echo ""
echo "Event loop lag (post-stress):"
curl -sk --max-time 5 "$BASE/metrics" || true
echo ""

# --- wait for py-spy to finish writing the SVG ---
echo "Waiting for py-spy to finish ..."
wait "$PY_SPY_PID" 2>/dev/null || true

# --- k6 summary ---
echo ""
echo "k6 summary:"
python3 - "$K6_JSON" <<'PYEOF'
import sys, json
from pathlib import Path
d = json.loads(Path(sys.argv[1]).read_text())
dur = d["metrics"]["http_req_duration"]
reqs = d["metrics"]["http_reqs"]
print(f"  req/s  : {reqs['rate']:.0f}")
print(f"  p50    : {dur['p(50)']:.2f} ms")
print(f"  p95    : {dur['p(95)']:.2f} ms")
print(f"  p99    : {dur['p(99)']:.2f} ms")
print(f"  max    : {dur['max']:.2f} ms")
PYEOF

echo ""
echo "Profile written: $SVG"
