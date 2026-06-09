#!/usr/bin/env bash
# bench/httparena/sprint34_remote.sh — runs on EC2 instance.
#
# Drives a native-vs-Docker A/B for the static-rotate.lua workload at
# a sequence of connection counts.  Each mode + each c value is a
# fresh server boot (clean page cache between cells would be nicer
# but is not enforced — between-cell behaviour is captured in the
# per-cell logs).
#
# Args: <conns_csv> <duration_s> <workers> <threads>
# e.g.  bash sprint34_remote.sh 64,1024,4096,6800 5 16 64
#
# Layout assumed on the instance (orchestrator stages these):
#   ~/sprint34_probe_app.py   (uploaded)
#   ~/venv/                   (python venv with blackbull[compression]==0.31.0)
#   /tmp/static/              (HttpArena's data/static cloned)
#   /tmp/dataset.json         (HttpArena's data/dataset.json)
#   /tmp/static-rotate.lua    (HttpArena's requests/static-rotate.lua)
#   httparena-blackbull       (docker image, built locally on the instance)

set -euo pipefail

CONNS_CSV="${1:?conns_csv required, eg 64,1024,4096,6800}"
DURATION="${2:-5}"
WORKERS="${3:-16}"
THREADS="${4:-64}"

PORT=8080
LUA=/tmp/static-rotate.lua
STATIC_DIR=/tmp/static
OUT_DIR="$HOME/sprint34_results"
mkdir -p "$OUT_DIR"

# Lift the shell ulimit; wrk auto-bumps anyway but be explicit so the
# log matches what we observed in Cell A.
ulimit -n 65536 || true

_wait_for_port() {
    local port="$1" attempts=40
    for _ in $(seq 1 "$attempts"); do
        if curl -fsS -m 1 "http://localhost:${port}/static/reset.css" -o /dev/null 2>/dev/null; then
            return 0
        fi
        sleep 0.5
    done
    echo "FAIL: server on :${port} did not become ready" >&2
    return 1
}

_run_wrk() {
    local mode="$1" conns="$2"
    local tag="${mode}_w${WORKERS}_t${THREADS}_c${conns}"
    local out="${OUT_DIR}/${tag}.log"

    echo "  wrk -t${THREADS} -c${conns} -d${DURATION}s ..." | tee -a "${out}"
    # No taskset: wrk runs unpinned so it can use all cores
    # (the framework also runs unpinned in both native and docker).
    wrk -t"${THREADS}" -c"${conns}" -d"${DURATION}s" \
        -s "${LUA}" "http://localhost:${PORT}" 2>&1 | tee -a "${out}"
    echo "  [done] ${tag}" | tee -a "${out}"
}

_start_native() {
    echo "[native] launching probe_app (workers=${WORKERS})"
    source "$HOME/venv/bin/activate"
    STATIC_DIR="${STATIC_DIR}" WEB_WORKERS="${WORKERS}" PORT="${PORT}" \
        python3 "$HOME/sprint34_probe_app.py" \
        >"${OUT_DIR}/native_server.log" 2>&1 &
    NATIVE_PID=$!
    _wait_for_port "${PORT}"
}

_stop_native() {
    if [ -n "${NATIVE_PID:-}" ]; then
        # The launcher process spawns N workers; killing the launcher
        # leaves orphans, so reach for the process group instead.
        sudo kill -TERM -- -"${NATIVE_PID}" 2>/dev/null || true
        # Best-effort cleanup of any stragglers bound to :8080.
        sudo fuser -k -TERM "${PORT}/tcp" 2>/dev/null || true
        sleep 1
        sudo fuser -k -KILL "${PORT}/tcp" 2>/dev/null || true
        wait "${NATIVE_PID}" 2>/dev/null || true
        NATIVE_PID=""
    fi
}

_start_docker() {
    echo "[docker] launching httparena-blackbull (workers=${WORKERS})"
    sudo docker rm -f bb-probe 2>/dev/null || true
    sudo docker run -d --name bb-probe --network host \
        --ulimit nofile=65536:65536 \
        --security-opt seccomp=unconfined \
        -e WEB_WORKERS="${WORKERS}" \
        -e BB_HTTPARENA_PORTS=http \
        -v "${STATIC_DIR}:/data/static:ro" \
        -v /tmp/dataset.json:/data/dataset.json:ro \
        httparena-blackbull >/dev/null
    _wait_for_port "${PORT}"
}

_stop_docker() {
    sudo docker rm -f bb-probe 2>/dev/null || true
}

# Native pass
echo "=== sprint34 native pass ==="
_start_native
IFS=',' read -ra CONN_LIST <<< "${CONNS_CSV}"
for c in "${CONN_LIST[@]}"; do
    _run_wrk native "$c"
done
_stop_native

# Docker pass
echo "=== sprint34 docker pass ==="
_start_docker
for c in "${CONN_LIST[@]}"; do
    _run_wrk docker "$c"
done
_stop_docker

echo "=== done — results under ${OUT_DIR} ==="
ls -la "${OUT_DIR}"
