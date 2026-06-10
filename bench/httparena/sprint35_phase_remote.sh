#!/usr/bin/env bash
# bench/httparena/sprint35_phase_remote.sh — runs on EC2.
#
# Phase-trace probe for Sprint 35.  For each of bb-before / bb-after:
#   - start container with BB_PHASE_TRACE=1 BB_ACCESS_LOG=1
#   - drive wrk on static profile at c=512 for 30s (warm cache, then sample)
#   - capture docker logs (one [ACCESS] line per completed request, with
#     the trailing "[loop_start→parsed=Xw/Yc parsed→dispatch_done=...]")
#   - tear down
#
# Output: per-framework log files under ~/sprint35_phase_results/.
# Local launcher post-processes them to compute mean/p50/p99 per phase.
#
# Args: <duration_s> <conns> <workers>

set -euo pipefail

DURATION="${1:-30}"
CONNS="${2:-512}"
WORKERS="${3:-16}"
THREADS=64
PORT=8080
LUA=/tmp/static-rotate.lua
STATIC_DIR=/tmp/static
OUT_DIR="$HOME/sprint35_phase_results"
mkdir -p "$OUT_DIR"

ulimit -n 65536 || true

_wait_for_port() {
    local probe="$1" attempts=40
    for _ in $(seq 1 "$attempts"); do
        if curl -fsS -m 1 "$probe" -o /dev/null 2>/dev/null; then
            return 0
        fi
        sleep 0.5
    done
    echo "FAIL: $probe did not become ready" >&2
    return 1
}

_probe() {
    local fw="$1"
    local image="httparena-${fw}"
    local logfile="${OUT_DIR}/${fw}_access.log"
    local wrk_out="${OUT_DIR}/${fw}_wrk.log"

    echo "=== framework: $fw ==="
    sudo docker rm -f bench-fw 2>/dev/null || true

    # BB_ACCESS_LOG=1 lights up the access logger so [ACCESS] lines emit.
    # BB_PHASE_TRACE=1 fills AccessLogRecord.phases, appended to each line.
    sudo docker run -d --name bench-fw --network host \
        --ulimit nofile=65536:65536 \
        --security-opt seccomp=unconfined \
        -e WEB_WORKERS="$WORKERS" \
        -e BB_HTTPARENA_PORTS=http \
        -e BB_ACCESS_LOG=1 \
        -e BB_PHASE_TRACE=1 \
        -v "${STATIC_DIR}:/data/static:ro" \
        -v /tmp/dataset.json:/data/dataset.json:ro \
        "$image" >/dev/null

    _wait_for_port "http://localhost:${PORT}/pipeline"

    # Warm-up: 5s of wrk to populate the static cache + the resolved-target
    # cache (after) so we don't sample the cold-miss path.
    echo "  warmup..."
    wrk -t"${THREADS}" -c"${CONNS}" -d5s -s "$LUA" "http://localhost:${PORT}" \
        >/dev/null 2>&1 || true

    # Drain warm-up access lines, then start fresh capture.
    sudo docker logs bench-fw >/dev/null 2>&1 || true
    sudo docker logs --tail 0 -f bench-fw > "$logfile" 2>&1 &
    local logger_pid=$!

    # Measurement window.
    echo "  measure ${DURATION}s at c=${CONNS}..."
    wrk -t"${THREADS}" -c"${CONNS}" -d"${DURATION}s" \
        -s "$LUA" "http://localhost:${PORT}" 2>&1 | tee "$wrk_out"

    # Give the logger a moment to flush.
    sleep 1
    kill "$logger_pid" 2>/dev/null || true
    wait "$logger_pid" 2>/dev/null || true

    sudo docker rm -f bench-fw 2>/dev/null || true
    echo "  [done] $fw"
}

for fw in bb-before bb-after; do
    _probe "$fw"
done

echo "=== done — artefacts under ${OUT_DIR} ==="
wc -l "${OUT_DIR}"/*_access.log || true
