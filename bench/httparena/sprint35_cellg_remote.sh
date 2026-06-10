#!/usr/bin/env bash
# bench/httparena/sprint35_cellg_remote.sh — runs on EC2.
#
# Three-way sweep for Sprint 35 Cell G A/B:
#   blackbull-before — master HEAD baseline wheel
#   blackbull-after  — candidate-branch wheel
#   fastapi          — HttpArena's stock FastAPI image (apples-to-apples reference)
#
# Same wrk shape and dataset across all three.  The BB/FA ratio
# before vs after is the regression test for the candidate.
#
# Layout assumed:
#   /tmp/static/                cloned from HttpArena's data/static
#   /tmp/dataset.json           cloned from HttpArena's data/dataset.json
#   /tmp/static-rotate.lua      cloned from HttpArena's requests/
#   httparena-bb-before, httparena-bb-after, httparena-fastapi — docker images
#
# Args: <conns_csv> <duration_s> <workers>

set -euo pipefail

CONNS_CSV="${1:?conns_csv required}"
DURATION="${2:-5}"
WORKERS="${3:-16}"
THREADS=64
PORT=8080
LUA=/tmp/static-rotate.lua
STATIC_DIR=/tmp/static
OUT_DIR="$HOME/sprint35_cellg_results"
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

_start_framework() {
    local fw="$1"
    local image
    local -a extra_env=()
    case "$fw" in
        bb-before)
            image=httparena-bb-before
            extra_env=( -e WEB_WORKERS="$WORKERS" -e BB_HTTPARENA_PORTS=http -e BB_NO_COMPRESSION="${BB_NO_COMPRESSION:-0}" )
            ;;
        bb-after)
            image=httparena-bb-after
            extra_env=( -e WEB_WORKERS="$WORKERS" -e BB_HTTPARENA_PORTS=http -e BB_NO_COMPRESSION="${BB_NO_COMPRESSION:-0}" )
            ;;
        fastapi)
            image=httparena-fastapi
            extra_env=( -e WEB_CONCURRENCY="$WORKERS" -e WEB_WORKERS="$WORKERS" )
            ;;
        *)
            echo "FAIL: unknown framework $fw" >&2
            return 1
            ;;
    esac

    sudo docker rm -f bench-fw 2>/dev/null || true
    sudo docker run -d --name bench-fw --network host \
        --ulimit nofile=65536:65536 \
        --security-opt seccomp=unconfined \
        "${extra_env[@]}" \
        -v "${STATIC_DIR}:/data/static:ro" \
        -v /tmp/dataset.json:/data/dataset.json:ro \
        "$image" >/dev/null

    _wait_for_port "http://localhost:${PORT}/pipeline"
}

_stop_framework() {
    sudo docker rm -f bench-fw 2>/dev/null || true
}

_run_wrk() {
    local fw="$1" prof="$2" conns="$3"
    local tag="${fw}_${prof}_c${conns}"
    local out="${OUT_DIR}/${tag}.log"

    case "$prof" in
        baseline)
            echo "  wrk -t${THREADS} -c${conns} /pipeline ($fw)" | tee -a "$out"
            wrk -t"${THREADS}" -c"${conns}" -d"${DURATION}s" \
                "http://localhost:${PORT}/pipeline" 2>&1 | tee -a "$out"
            ;;
        static)
            echo "  wrk -t${THREADS} -c${conns} /static-rotate ($fw)" | tee -a "$out"
            wrk -t"${THREADS}" -c"${conns}" -d"${DURATION}s" \
                -s "$LUA" "http://localhost:${PORT}" 2>&1 | tee -a "$out"
            ;;
        *)
            echo "FAIL: unknown profile $prof" >&2
            return 1
            ;;
    esac
    echo "  [done] ${tag}" | tee -a "$out"
}

IFS=',' read -ra CONN_LIST <<< "${CONNS_CSV}"

for fw in bb-before bb-after fastapi; do
    echo "=== framework: $fw ==="
    _start_framework "$fw"
    for prof in baseline static; do
        for c in "${CONN_LIST[@]}"; do
            _run_wrk "$fw" "$prof" "$c"
        done
    done
    _stop_framework
done

echo "=== done — artefacts under ${OUT_DIR} ==="
ls -la "${OUT_DIR}" | head -20
