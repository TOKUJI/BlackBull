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
#   --insecure  skip TLS cert verification (mkcert cert not in system trust)

set -e

BASE="https://localhost:8443"
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
h2load -n 10000 -c 50 -m 1 --insecure "$BASE/ping"

echo ""
echo "--- /ping  streams/conn=10  (browser-like) ---"
h2load -n 10000 -c 50 -m 10 --insecure "$BASE/ping"

echo ""
echo "--- /ping  streams/conn=50  (heavy multiplexing) ---"
h2load -n 10000 -c 50 -m 50 --insecure "$BASE/ping"

echo ""
echo "--- /1kb   streams/conn=10 ---"
h2load -n 5000 -c 50 -m 10 --insecure "$BASE/1kb"

echo ""
echo "--- /64kb  streams/conn=5  (large responses + flow control) ---"
h2load -n 500 -c 20 -m 5 --insecure "$BASE/64kb"

echo ""
echo "$SEP"
echo "Columns: req/s | min/mean/sd/max latency (ms) | +/-sd | traffic"
echo "$SEP"
