#!/usr/bin/env bash
# Sprint 30 Tier 2 — DIRECT LATENCY PROBE.
#
# Methodology (per the user's correction):
#   - r/s is an indirect metric; Tier 2 was designed to shorten
#     waiting time.  Measure that directly via the access log's
#     ``{duration}ms`` field.
#   - 5 s wrk runs are too short.  Use a 30 s warm-up window
#     followed by a 60 s measurement window so transient warm-up
#     effects don't pollute the percentiles.
#
# Usage:
#   bash bench/static_repro/latency_probe.sh {on|off}
#
# Env knobs (forwarded to the server):
#   BB_MAX_CONNECTIONS              (default 0 = disabled)
#   BB_ACCEPT_PAUSE_HIGH_WATERMARK  (only meaningful when {on})
#   BB_ACCEPT_PAUSE_LOW_WATERMARK   (only meaningful when {on})
#   CONCURRENCY                     (default 4096)
#   WARMUP                          (default 30 — seconds)
#   MEASURE                         (default 60 — seconds)
set -euo pipefail
cd "$(dirname "$0")/../.."

MODE="${1:-off}"
PORT="${PORT:-8080}"
CONCURRENCY="${CONCURRENCY:-4096}"
WARMUP="${WARMUP:-30}"
MEASURE="${MEASURE:-60}"

case "$MODE" in
    on)
        EXTRA_ENV="BB_ACCEPT_PAUSE_HIGH_WATERMARK=${BB_ACCEPT_PAUSE_HIGH_WATERMARK:-900} BB_ACCEPT_PAUSE_LOW_WATERMARK=${BB_ACCEPT_PAUSE_LOW_WATERMARK:-750}"
        ;;
    off)
        EXTRA_ENV="BB_ACCEPT_PAUSE_HIGH_WATERMARK=0 BB_ACCEPT_PAUSE_LOW_WATERMARK=0"
        ;;
    *)
        echo "Usage: $0 {on|off}" >&2
        exit 1
        ;;
esac

TOTAL=$(( WARMUP + MEASURE ))
ACCESS_LOG="/tmp/latency_probe_${MODE}.log"
SERVER_LOG="/tmp/latency_server_${MODE}.log"

STATIC_DIR="$(pwd)/bench/httparena/_local/data/static/"
DATASET_PATH="$(pwd)/bench/httparena/_local/data/dataset.json"

echo "=== latency_probe: $MODE ==="
echo "  BB_MAX_CONNECTIONS=${BB_MAX_CONNECTIONS:-0}"
echo "  $EXTRA_ENV"
echo "  c=$CONCURRENCY  warmup=${WARMUP}s  measure=${MEASURE}s  total=${TOTAL}s"
echo "  access log → $ACCESS_LOG"
echo

# Kill any stale server.
pkill -f static_repro/access_log_server 2>/dev/null || true
pkill -f static_repro/server_probe 2>/dev/null || true
pkill -f bench/httparena/app 2>/dev/null || true
sleep 2

# Boot server with access log enabled.
env $EXTRA_ENV \
    BB_MAX_CONNECTIONS="${BB_MAX_CONNECTIONS:-0}" \
    STATIC_DIR="$STATIC_DIR" \
    DATASET_PATH="$DATASET_PATH" \
    ACCESS_LOG_PATH="$ACCESS_LOG" \
    python3 bench/static_repro/access_log_server.py --port "$PORT" \
    >"$SERVER_LOG" 2>&1 &
SERVER_PID=$!
trap 'kill -TERM $SERVER_PID 2>/dev/null || true' EXIT

# Wait for ready.
for i in $(seq 1 30); do
    curl -sf "http://localhost:$PORT/static/app.js" >/dev/null 2>&1 && break
    sleep 0.2
done

echo "Server PID: $SERVER_PID"
echo "Idle FDs:   $(ls /proc/$SERVER_PID/fd 2>/dev/null | wc -l)"
echo

# Note warm-up boundary BEFORE starting wrk.  ISO timestamp matching
# the access-log format prefix.
WARMUP_END=$(date -d "+${WARMUP} seconds" +'%Y-%m-%dT%H:%M:%S')
WINDOW_END=$(date -d "+${TOTAL} seconds" +'%Y-%m-%dT%H:%M:%S')

echo "Window markers:"
echo "  warmup_end:  $WARMUP_END  (measurements start)"
echo "  window_end:  $WINDOW_END"
echo

# Single sustained wrk run covering both warmup and measurement.
echo "--- wrk -t8 -c${CONCURRENCY} -d${TOTAL}s (Accept-Encoding: br) ---"
wrk -t8 -c"$CONCURRENCY" -d"${TOTAL}s" -s bench/static_repro/rotate.lua \
    -H 'Accept-Encoding: br;q=1, gzip;q=0.8' \
    --latency \
    "http://localhost:$PORT" 2>&1 \
  | tee /tmp/wrk_${MODE}.log \
  | grep -E "Requests/sec|requests in|Latency"

echo
echo "--- post-load FDs: $(ls /proc/$SERVER_PID/fd 2>/dev/null | wc -l) ---"

# Give the server a moment to flush remaining access-log entries.
sleep 2

echo
echo "--- access-log latency percentiles (post-warmup window) ---"
echo "  config: mode=$MODE  BB_MAX_CONNECTIONS=${BB_MAX_CONNECTIONS:-0}  $EXTRA_ENV"
python3 bench/static_repro/parse_access_log.py "$ACCESS_LOG" \
    "$WARMUP_END" "$WINDOW_END" || true

echo
echo "Access log preserved at $ACCESS_LOG"
echo "Server log preserved at $SERVER_LOG"

# Trap cleans up the server.
