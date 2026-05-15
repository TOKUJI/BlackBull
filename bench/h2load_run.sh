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

echo "$SEP"
echo "h2load benchmark — BlackBull single-process baseline"
echo "$(date)"
echo "$SEP"

echo ""
echo "--- /ping  streams/conn=1  (comparable to HTTP/1.1) ---"
h2load -n 10000 -c 50 -m 1 "$BASE/ping"

echo ""
echo "--- /ping  streams/conn=10  (browser-like) ---"
h2load -n 10000 -c 50 -m 10 "$BASE/ping"

echo ""
echo "--- /ping  streams/conn=50  (heavy multiplexing) ---"
h2load -n 10000 -c 50 -m 50 "$BASE/ping"

echo ""
echo "--- /1kb   streams/conn=10 ---"
h2load -n 5000 -c 50 -m 10 "$BASE/1kb"

echo ""
echo "--- /16kb  streams/conn=5  (larger responses) ---"
h2load -n 500 -c 20 -m 5 "$BASE/16kb"

echo ""
echo "$SEP"
echo "Columns: req/s | min/mean/sd/max latency (ms) | +/-sd | traffic"
echo "$SEP"
