#!/usr/bin/env bash
# Reproduces the HttpArena static-profile run-2/3 degradation shape
# locally on a single-process BlackBull, with per-run instrumentation.
#
# Sprint 30 Task 1 — local reproduction harness.
#
# Usage:
#   bash bench/static_repro/run.sh
#   CONCURRENCY=4096 DURATION=5 RUNS=3 bash bench/static_repro/run.sh
#
# Output: r/s, RSS, FD count, and socket-state distribution captured
# before/after each wrk run.  Server is left alive between runs so
# any state accumulation (cache, FDs, sockets) carries over.
set -euo pipefail

cd "$(dirname "$0")/../.."     # repo root

CONCURRENCY="${CONCURRENCY:-1024}"
DURATION="${DURATION:-5}"
RUNS="${RUNS:-3}"
PORT="${PORT:-8080}"
THREADS="${THREADS:-8}"

STATIC_DIR="$(pwd)/bench/httparena/_local/data/static/"
DATASET_PATH="$(pwd)/bench/httparena/_local/data/dataset.json"

if [ ! -d "$STATIC_DIR" ]; then
    echo "FATAL: $STATIC_DIR does not exist." >&2
    echo "Local sandbox isn't set up; see bench/httparena/README.md." >&2
    exit 1
fi

echo "=== static reproduction probe ==="
echo "  concurrency: $CONCURRENCY"
echo "  duration:    ${DURATION}s × $RUNS runs"
echo "  static dir:  $STATIC_DIR"
echo "  port:        $PORT"
echo

# Boot the server in the background.
BB_ACCESS_LOG=0 \
STATIC_DIR="$STATIC_DIR" \
DATASET_PATH="$DATASET_PATH" \
    python3 bench/httparena/app.py --port "$PORT" >/tmp/static_repro_server.log 2>&1 &
SERVER_PID=$!
echo ">>> server pid: $SERVER_PID"
trap 'kill -TERM $SERVER_PID 2>/dev/null || true' EXIT

# Wait for readiness.
for i in $(seq 1 30); do
    if curl -sf "http://localhost:$PORT/static/app.js" >/dev/null 2>&1; then
        break
    fi
    sleep 0.2
done
if ! curl -sf "http://localhost:$PORT/static/app.js" >/dev/null 2>&1; then
    echo "FATAL: server didn't come up; see /tmp/static_repro_server.log" >&2
    exit 1
fi

# Sanity probe: confirm precompressed-variant path fires.
echo ">>> sanity — Accept-Encoding negotiation"
plain=$(curl -s "http://localhost:$PORT/static/app.js" | wc -c)
br=$(curl -s -H 'Accept-Encoding: br' "http://localhost:$PORT/static/app.js" | wc -c)
echo "  plain bytes:    $plain"
echo "  br-sibling bytes: $br"
if [ "$br" -ge "$plain" ]; then
    echo "  WARNING: br response not smaller than plain — precompressed path may not be firing"
fi
echo

# Helper: capture per-run instrumentation.
capture() {
    local tag="$1"
    local rss=$(ps -p "$SERVER_PID" -o rss= | tr -d ' ')
    local nlwp=$(ps -p "$SERVER_PID" -o nlwp= | tr -d ' ')
    local fds=$(ls "/proc/$SERVER_PID/fd" 2>/dev/null | wc -l)
    local ss_summary=$(ss -tan 2>/dev/null \
        | awk 'NR>1 {print $1}' \
        | sort | uniq -c | tr '\n' ' ')
    printf "  [%s] RSS=%skB threads=%s fds=%s | %s\n" \
        "$tag" "$rss" "$nlwp" "$fds" "$ss_summary"
}

echo ">>> baseline (server idle, before any wrk)"
capture "idle"
echo

for r in $(seq 1 "$RUNS"); do
    echo ">>> run $r/$RUNS"
    capture "pre"
    wrk -t"$THREADS" -c"$CONCURRENCY" -d"${DURATION}s" \
        -s bench/static_repro/rotate.lua \
        --latency \
        "http://localhost:$PORT" 2>&1 \
      | grep -E "Requests/sec|Transfer/sec|Latency|Socket errors|requests in"
    capture "post"
    echo
done

echo ">>> done; server still alive (pid=$SERVER_PID)."
echo "    To attach a profiler:"
echo "      py-spy record --native --duration 10 -p $SERVER_PID -o /tmp/static_repro_profile.svg"
echo "      py-spy dump -p $SERVER_PID"
echo "    Server log: /tmp/static_repro_server.log"
echo "    Kill: kill $SERVER_PID"

# Disable trap so the server survives this script's exit (so we can
# attach a profiler from another shell).  Caller terminates it.
trap - EXIT
