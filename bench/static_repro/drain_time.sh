#!/usr/bin/env bash
# Drain-time probe — measures how long the server holds FDs after a
# burst of wrk connections all FIN simultaneously.
#
# Sprint 30 Tier 1.5 Step 5 — answers the question:
# "Does the custom protocol actually shorten connection-close time?"
#
# Methodology:
#   1. Boot server.
#   2. Run wrk c=4096 d=5s.
#   3. Immediately after wrk exits, sample
#      /proc/$PID/fd | wc -l every 200 ms until FD count drops back
#      below 50 (idle baseline ~ 8).
#   4. Record the elapsed seconds.
#
# Usage:
#   bash bench/static_repro/drain_time.sh [toggle_on|toggle_off]
set -euo pipefail
cd "$(dirname "$0")/../.."

MODE="${1:-toggle_off}"
PORT="${PORT:-8080}"
CONCURRENCY="${CONCURRENCY:-4096}"
DURATION="${DURATION:-5}"

if [ "$MODE" = "toggle_on" ]; then
    EXTRA_ENV="BB_USE_CUSTOM_PROTOCOL=1"
elif [ "$MODE" = "toggle_off" ]; then
    EXTRA_ENV="BB_USE_CUSTOM_PROTOCOL=0"
else
    echo "Usage: $0 {toggle_on|toggle_off}" >&2
    exit 1
fi

STATIC_DIR="$(pwd)/bench/httparena/_local/data/static/"
DATASET_PATH="$(pwd)/bench/httparena/_local/data/dataset.json"

echo "=== drain-time probe: $MODE ==="
echo "  $EXTRA_ENV"
echo "  c=$CONCURRENCY d=${DURATION}s"
echo

# Boot server.
pkill -f static_repro/server_probe 2>/dev/null || true
pkill -f bench/httparena/app 2>/dev/null || true
sleep 1
env $EXTRA_ENV \
    BB_ACCESS_LOG=0 \
    BB_MAX_CONNECTIONS=0 \
    STATIC_DIR="$STATIC_DIR" \
    DATASET_PATH="$DATASET_PATH" \
    python3 bench/static_repro/server_probe.py --port "$PORT" \
    >/tmp/drain_server.log 2>&1 &
SERVER_PID=$!
trap 'kill -TERM $SERVER_PID 2>/dev/null || true' EXIT

# Wait for ready.
for i in $(seq 1 30); do
    curl -sf "http://localhost:$PORT/static/app.js" >/dev/null 2>&1 && break
    sleep 0.2
done

idle_fds=$(ls /proc/$SERVER_PID/fd 2>/dev/null | wc -l)
echo "Idle FD count: $idle_fds"

# Confirm setting took effect by inspecting the server log.
grep -iE "use_custom_protocol|BlackBullProtocol|start_server|create_server" \
    /tmp/drain_server.log | head -3 || true
echo

# Run wrk for the burst.
echo "--- running wrk c=$CONCURRENCY d=${DURATION}s ---"
wrk -t8 -c"$CONCURRENCY" -d"${DURATION}s" -s bench/static_repro/rotate.lua \
    -H 'Accept-Encoding: br;q=1, gzip;q=0.8' \
    "http://localhost:$PORT" 2>&1 \
  | grep -E "Requests/sec|requests in" || true

wrk_end_ns=$(date +%s%N)

# Sample FDs AND socket states every 20ms — fine enough to expose
# any sub-200ms ordering difference between toggle-off and toggle-on.
echo "--- drain sampling (every 20 ms, FDs + per-process socket states) ---"
THRESHOLD=50
BACK_TO_BASELINE_NS=0
for i in $(seq 1 1500); do  # up to 30 s
    fds=$(ls /proc/$SERVER_PID/fd 2>/dev/null | wc -l)
    # Count this PID's TCP sockets in each state via /proc/$PID/net/tcp
    # (this filters to TCP — kernel-wide ss output mixes wrk + server).
    states=$(awk 'NR>1 {print $4}' /proc/$SERVER_PID/net/tcp 2>/dev/null \
             | sort | uniq -c | tr '\n' ' ')
    now_ns=$(date +%s%N)
    elapsed_ms=$(( (now_ns - wrk_end_ns) / 1000000 ))
    # Print every 5th sample to keep output manageable but at full
    # resolution near the drain inflection (last 200ms).
    if [ "$fds" -le "$THRESHOLD" ] || [ "$((i % 5))" -eq 0 ] || \
       [ "$elapsed_ms" -gt 600 ]; then
        printf "  t+%6dms  fds=%4d  tcp_states={%s}\n" \
            "$elapsed_ms" "$fds" "$states"
    fi
    if [ "$fds" -le "$THRESHOLD" ] && [ "$BACK_TO_BASELINE_NS" -eq 0 ]; then
        BACK_TO_BASELINE_NS=$now_ns
        break
    fi
    sleep 0.02
done

if [ "$BACK_TO_BASELINE_NS" -eq 0 ]; then
    echo "WARN: FDs didn't drop below $THRESHOLD within 30 s"
else
    drain_ms=$(( (BACK_TO_BASELINE_NS - wrk_end_ns) / 1000000 ))
    echo "*** drain time ($MODE): ${drain_ms} ms ***"
fi

echo
echo "Server log: /tmp/drain_server.log"
echo "Server still alive (pid=$SERVER_PID).  Caller: kill $SERVER_PID."

# Don't kill on exit — let the next probe see a fresh state by
# explicitly killing in its own setup.
trap - EXIT
