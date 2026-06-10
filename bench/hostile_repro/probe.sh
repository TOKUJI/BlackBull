#!/usr/bin/env bash
# Drive one hostile attack against a fresh BlackBull server, capturing
# event-loop health metrics.
#
# Sprint 30 Tier 1 verification harness.
#
# Usage:
#   bash bench/hostile_repro/probe.sh slowloris_headers
#   bash bench/hostile_repro/probe.sh slow_read
#   bash bench/hostile_repro/probe.sh idle_park
#   bash bench/hostile_repro/probe.sh slowloris_body
#
# Env vars:
#   CONCURRENCY  (default 1024)
#   DURATION     (default 30)
#   PORT         (default 8080)
set -euo pipefail

cd "$(dirname "$0")/../.."

ATTACK="${1:-}"
if [ -z "$ATTACK" ]; then
    echo "Usage: $0 {slowloris_headers|slow_read|idle_park|slowloris_body}" >&2
    exit 1
fi
ATTACK_SCRIPT="bench/hostile_repro/attacks/${ATTACK}.py"
if [ ! -f "$ATTACK_SCRIPT" ]; then
    echo "Unknown attack: $ATTACK" >&2
    exit 1
fi

CONCURRENCY="${CONCURRENCY:-1024}"
DURATION="${DURATION:-30}"
PORT="${PORT:-8080}"

STATIC_DIR="$(pwd)/bench/httparena/_local/data/static/"
DATASET_PATH="$(pwd)/bench/httparena/_local/data/dataset.json"

echo "=== hostile-load probe: $ATTACK ==="
echo "  concurrency: $CONCURRENCY"
echo "  duration:    ${DURATION}s"
echo "  port:        $PORT"
echo

# Boot a fresh server using the SIGUSR1-instrumented wrapper.
BB_ACCESS_LOG=0 \
STATIC_DIR="$STATIC_DIR" \
DATASET_PATH="$DATASET_PATH" \
    python3 bench/static_repro/server_probe.py --port "$PORT" \
    >/tmp/hostile_server.log 2>&1 &
SERVER_PID=$!
trap 'kill -TERM $SERVER_PID 2>/dev/null || true' EXIT

for i in $(seq 1 30); do
    if curl -sf "http://localhost:$PORT/healthz" >/dev/null 2>&1; then break; fi
    sleep 0.2
done
if ! curl -sf "http://localhost:$PORT/healthz" >/dev/null 2>&1; then
    echo "FATAL: server didn't come up; see /tmp/hostile_server.log" >&2
    exit 1
fi

# Health helper: legitimate-traffic latency.
healthcheck() {
    local tag="$1"
    local before after rss fds
    before=$(date +%s%N)
    if curl -sf -m 5 "http://localhost:$PORT/healthz" >/dev/null 2>&1; then
        after=$(date +%s%N)
        local ms=$(( (after - before) / 1000000 ))
        local rss_kb=$(awk '/VmRSS/{print $2}' /proc/$SERVER_PID/status 2>/dev/null || echo '?')
        local fd_count=$(ls /proc/$SERVER_PID/fd 2>/dev/null | wc -l)
        echo "  [$tag] /healthz: ${ms}ms  RSS=${rss_kb}kB  FDs=${fd_count}"
    else
        local rss_kb=$(awk '/VmRSS/{print $2}' /proc/$SERVER_PID/status 2>/dev/null || echo '?')
        local fd_count=$(ls /proc/$SERVER_PID/fd 2>/dev/null | wc -l)
        echo "  [$tag] /healthz: FAIL (timeout/error)  RSS=${rss_kb}kB  FDs=${fd_count}"
    fi
}

echo ">>> baseline (no attack):"
healthcheck "baseline"

echo
echo ">>> starting attack: $ATTACK"
python3 "$ATTACK_SCRIPT" \
    --host localhost --port "$PORT" \
    --connections "$CONCURRENCY" --duration "$DURATION" \
    >/tmp/hostile_attack.log 2>&1 &
ATTACK_PID=$!

# Sample mid-attack at t+5s, t+15s, t+(duration-2s)
sleep 5
healthcheck "t+5s"
kill -USR1 $SERVER_PID 2>/dev/null && sleep 0.3 && \
  tail -25 /tmp/hostile_server.log | grep -E "(task dump|tasks ----|\.py:|^=)" | head -10

if [ "$DURATION" -gt 20 ]; then
    sleep 10
    healthcheck "t+15s"
fi

if [ "$DURATION" -gt 5 ]; then
    sleep $(( DURATION - (DURATION > 20 ? 17 : 7) ))
    healthcheck "t+near-end"
    kill -USR1 $SERVER_PID 2>/dev/null && sleep 0.3 && \
      tail -25 /tmp/hostile_server.log | grep -E "(task dump|tasks ----|\.py:|^=)" | head -10
fi

# Wait for attack to fully finish
wait $ATTACK_PID 2>/dev/null || true

echo
echo ">>> attack ended; recovery check:"
sleep 1
healthcheck "t+recovery+1"
sleep 5
healthcheck "t+recovery+6"

echo
echo "Server log: /tmp/hostile_server.log"
echo "Attack log: /tmp/hostile_attack.log"

# Trap will tear down the server.
