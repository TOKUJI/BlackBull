#!/usr/bin/env bash
# h2load multiplexing comparison — run from repo root after starting bench/app.py
#
# Usage:
#   bash bench/h2load_run.sh
#   bash bench/h2load_run.sh 2>&1 | tee bench/results/h2load_$(date +%Y%m%d_%H%M%S).txt
#
# h2load flags used:
#   -n  total requests
#   -c  concurrent connections
#   -m  max concurrent streams per connection (stream multiplexing depth)
#
# TLS: h2load uses the system CA store.  The mkcert root CA is pointed to via
#   SSL_CERT_FILE; try Windows AppData first, then the system store.

set -e

BASE="https://localhost:8443"

# Create results directory and tee all output to a timestamped raw log
RESULT_DIR="bench/results"
mkdir -p "$RESULT_DIR"
TIMESTAMP="${TIMESTAMP:-$(date +%Y%m%d-%H%M%S)}"
RUNS="${RUNS:-3}"
RAW_LOG="$RESULT_DIR/raw_${TIMESTAMP}.txt"
exec > >(tee "$RAW_LOG") 2>&1

# Locate the mkcert CA so h2load trusts the dev cert
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

if [ -n "$MKCERT_CA" ]; then
    export SSL_CERT_FILE="$MKCERT_CA"
    echo "Using mkcert CA: $MKCERT_CA"
else
    echo "WARNING: mkcert CA not found; h2load may fail TLS verification"
fi
SEP="$(printf '=%.0s' {1..60})"

need() {
  command -v "$1" >/dev/null 2>&1 || { echo "ERROR: $1 not found. Run: bash bench/install.sh"; exit 1; }
}
need h2load

# Run one scenario: one suppressed warmup pass then RUNS measured passes.
# Usage: run_h2 LABEL -n N -c C -m M URL
run_h2() {
    local label="$1"; shift
    local url="${@: -1}"
    h2load -n 1000 -c 10 -m 10 "$url" >/dev/null 2>&1 || true
    local i
    for i in $(seq 1 "$RUNS"); do
        echo ""
        echo "--- $label ---"
        h2load "$@"
    done
}

echo "$SEP"
echo "h2load benchmark — BlackBull"
echo "$(date)"
echo "$SEP"
echo ""
echo "Date       : $(date +%Y-%m-%dT%H:%M:%S)"

# Query the server's actual runtime configuration
_CFG=$(curl -sk "$BASE/config" 2>/dev/null)
if [ -n "$_CFG" ]; then
    _workers=$(echo "$_CFG" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['workers'])" 2>/dev/null)
    _uvloop=$(echo "$_CFG" | python3 -c "import sys,json; d=json.load(sys.stdin); print(1 if d['uvloop'] else 0)" 2>/dev/null)
    _streams_1w=$(echo "$_CFG" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['h2_active_streams_1w'])" 2>/dev/null)
    _streams_nw=$(echo "$_CFG" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['h2_active_streams'])" 2>/dev/null)
else
    echo "WARNING: could not reach $BASE/config — server config unknown"
    _workers="${BB_WORKERS:-?}"
    _uvloop="${BB_UVLOOP:-?}"
    _streams_1w="${BB_H2_ACTIVE_STREAMS_1W:-?}"
    _streams_nw="${BB_H2_ACTIVE_STREAMS:-?}"
fi

echo "Workers    : $_workers"
echo "uvloop     : $_uvloop"
echo "Streams 1w : $_streams_1w"
echo "Streams Nw : $_streams_nw"

run_h2 "/ping  streams/conn=1  (comparable to HTTP/1.1)" -n 50000 -c 50 -m 1 "$BASE/ping"
run_h2 "/ping  streams/conn=10  (browser-like)" -n 90000 -c 50 -m 10 "$BASE/ping"
run_h2 "/ping  streams/conn=50  (heavy multiplexing)" -n 90000 -c 50 -m 50 "$BASE/ping"
run_h2 "/1kb   streams/conn=10" -n 90000 -c 50 -m 10 "$BASE/1kb"
run_h2 "/16kb  streams/conn=5  (larger responses)" -n 50000 -c 20 -m 5 "$BASE/16kb"
run_h2 "/64kb  streams/conn=5  (flow-control exercise)" -n 15000 -c 10 -m 5 "$BASE/64kb"
run_h2 "/1mb   streams/conn=3  (large response, 1 MiB window)" -n 600 -c 5 -m 3 "$BASE/1mb"

echo ""
echo "$SEP"
echo "Columns: req/s | min/mean/sd/max latency (ms) | +/-sd | traffic"
echo "$SEP"

# Generate markdown summary from the raw log
python bench/summarize.py "$RAW_LOG"
