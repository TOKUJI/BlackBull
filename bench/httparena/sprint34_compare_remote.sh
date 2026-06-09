#!/usr/bin/env bash
# bench/httparena/sprint34_compare_remote.sh — runs on EC2 instance.
#
# Drives a framework × profile × concurrency sweep for the
# BlackBull-vs-FastAPI comparison.  Loops over conn counts that
# include values low enough to keep wrk away from its own saturation
# point, so the per-cell r/s reflects the framework, not the loadgen.
#
# Both frameworks are containerised via the HttpArena framework
# Dockerfiles (same build the public leaderboard uses), exposed on
# :8080 cleartext, with 16 workers.
#
# Args: <conns_csv> <duration_s> <workers>
# e.g.  bash sprint34_compare_remote.sh 64,256,1024 5 16
#
# Layout assumed:
#   /tmp/static/                cloned from HttpArena's data/static
#   /tmp/dataset.json           cloned from HttpArena's data/dataset.json
#   /tmp/static-rotate.lua      cloned from HttpArena's requests/
#   httparena-blackbull         docker image (built locally)
#   httparena-fastapi           docker image (built locally)

set -euo pipefail

CONNS_CSV="${1:?conns_csv required}"
DURATION="${2:-5}"
WORKERS="${3:-16}"
THREADS=64
PORT=8080
LUA=/tmp/static-rotate.lua
STATIC_DIR=/tmp/static
OUT_DIR="$HOME/sprint34_compare_results"
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
        blackbull)
            image=httparena-blackbull
            extra_env=( -e WEB_WORKERS="$WORKERS" -e BB_HTTPARENA_PORTS=http )
            ;;
        fastapi)
            image=httparena-fastapi
            # HttpArena's fastapi framework reads WEB_CONCURRENCY (gunicorn
            # convention).  Pass both so either name works.
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

    # Both frameworks must answer GET /pipeline → "ok".
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

for fw in blackbull fastapi; do
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
