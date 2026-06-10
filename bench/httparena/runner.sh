#!/usr/bin/env bash
set -euo pipefail

FW="${1:-unknown}"
PROF="${2:-unknown}"
WEB_WORKERS="${3:-}"
WEB_NOFILE="${4:-65536}"
LOADGEN_CPUS="${5:-}"
LOADGEN_NOFILE="${6:-65536}"

# Sprint 34: native-mode wrk inherits the parent shell's RLIMIT_NOFILE
# (HttpArena defaults LOADGEN_DOCKER=false so wrk runs via
# `taskset -c $GCANNON_CPUS wrk ...` directly).  Set the shell limit to
# LOADGEN_NOFILE so the wrk-FD-cap hypothesis (Sprint 33) is directly
# controllable from the harness.  Framework container's ulimit is set
# separately via the HARD_NOFILE override below.
ulimit -n "${LOADGEN_NOFILE}"

# Push WEB_NOFILE and WEB_WORKERS into benchmark.sh / framework.sh via
# env.  HARD_NOFILE is read by common.sh from `ulimit -Hn`; pre-export
# overrides the read (the sed-patch applied by httparena_compare.new.sh
# adds an "honor pre-set HARD_NOFILE" branch to common.sh).  WEB_WORKERS
# is consumed by bench/httparena/launcher.py inside the container, so
# we surface it via `-e WEB_WORKERS=...` on the framework `docker run`
# (added to args[] in the matching framework.sh sed-patch).
export HARD_NOFILE="${WEB_NOFILE}"
export WEB_WORKERS WEB_NOFILE LOADGEN_NOFILE
# Sprint 34 Cell B: forwarded to launcher.py via -e in framework.sh
# (patched by patch_httparena.py).  Comma-list of listeners to spawn;
# default = all three (back-compat).  For static-profile sweeps we
# pass only "http" so :8081/:8443 don't fork idle worker pools.
export BB_HTTPARENA_PORTS="${BB_HTTPARENA_PORTS:-}"

# LOADGEN_CPUS maps onto HttpArena's GCANNON_CPUS env var, which
# benchmark.sh natively respects in both DOCKER_FLAGS (--cpuset-cpus)
# and the native `taskset -c $GCANNON_CPUS` wrk invocation.
if [ -n "${LOADGEN_CPUS}" ]; then
    export GCANNON_CPUS="${LOADGEN_CPUS}"
fi

OUT_DIR="${HOME}/results/loadgen"
mkdir -p "${OUT_DIR}"

RUN_LOG="${OUT_DIR}/${FW}-${PROF}_runner.log"
cd HttpArena

echo "[runner] start fw=${FW} prof=${PROF}" | tee -a "${RUN_LOG}"
{
    echo "[runner] LOADGEN_NOFILE=${LOADGEN_NOFILE} (shell ulimit -n -> $(ulimit -n))"
    echo "[runner] WEB_NOFILE=${WEB_NOFILE} (HARD_NOFILE override for framework container)"
    echo "[runner] WEB_WORKERS=${WEB_WORKERS:-<unset>}"
    echo "[runner] LOADGEN_CPUS=${LOADGEN_CPUS:-<unset>} -> GCANNON_CPUS=${GCANNON_CPUS:-<unset>}"
} | tee -a "${RUN_LOG}"


# ---------------------------------------
# 1 benchmark開始前に event を起動
# ---------------------------------------
sudo docker events --since 0 \
    > "${OUT_DIR}/${FW}-${PROF}_docker_events.log" &
EVENT_PID=$!

echo "[runner] event_pid=${EVENT_PID}" | tee -a "${RUN_LOG}"

# ---------------------------------------
# 2 benchmark起動
# ---------------------------------------
{
    echo "ulimit_n=$(ulimit -n)"
    # echo "GCANNON_CPUS=${GCANNON_CPUS:-unset}"
    echo "LOADGEN_CPUS=${LOADGEN_CPUS:-unset}"
} >> "${OUT_DIR}/${FW}-${PROF}_env.log"

# sudo -E preserves the HARD_NOFILE / GCANNON_CPUS / WEB_WORKERS /
# WEB_NOFILE / LOADGEN_NOFILE env we exported above.  HttpArena's
# common.sh + framework.sh + benchmark.sh (post-sed-patch) read these.
sudo -E ./scripts/benchmark.sh "${FW}" "${PROF}" --save \
    > "${OUT_DIR}/${FW}-${PROF}_benchmark_out.log" \
    2> "${OUT_DIR}/${FW}-${PROF}_benchmark_err.log" &
BENCHMARK_PID=$!

echo "[runner] benchmark_pid=${BENCHMARK_PID}" | tee -a "${RUN_LOG}"

# ---------------------------------------
# 3 monitor起動
# ---------------------------------------
~/src/monitor.sh "${FW}" "${PROF}" \
    "${EVENT_PID}" \
    "${BENCHMARK_PID}" &
MONITOR_PID=$!

echo "[runner] monitor_pid=${MONITOR_PID}" | tee -a "${RUN_LOG}"

# ---------------------------------------
# 4 benchmark完了待ち
# ---------------------------------------
wait "${BENCHMARK_PID}"
RC=$?

echo "[runner] benchmark finished rc=${RC}" | tee -a "${RUN_LOG}"

# ---------------------------------------
# 5 即時回収フェーズ
# ---------------------------------------

sudo docker ps -a --filter 'ancestor=wrk' --format '{{.ID}} {{.Image}}' \
    > "${OUT_DIR}/${FW}-${PROF}_docker_ps.log" || true

CIDS=$(sudo docker ps -a -q 2>/dev/null || true)

for cid in ${CIDS}; do
    echo "[runner] inspect ${cid}" | tee -a "${RUN_LOG}"

    sudo docker inspect "${cid}" \
        >> "${OUT_DIR}/${FW}-${PROF}_docker_inspect.json" || true

    sudo docker logs "${cid}" \
        >> "${OUT_DIR}/${FW}-${PROF}_docker_logs.log" 2>&1 || true
done

# ---------------------------------------
# 6 monitor停止（benchmark後に停止制御）
# ---------------------------------------
kill "${MONITOR_PID}" 2>/dev/null || true
wait "${MONITOR_PID}" 2>/dev/null || true

echo "[runner] monitor stopped" | tee -a "${RUN_LOG}"

# ---------------------------------------
# 7 docker events停止
# ---------------------------------------
kill "${EVENT_PID}" 2>/dev/null || true

echo "[runner] event stopped" | tee -a "${RUN_LOG}"

# ---------------------------------------
# 8 postprocess
# ---------------------------------------
~/src/postprocess.sh "${FW}" "${PROF}"

echo "[runner] done rc=${RC}" | tee -a "${RUN_LOG}"

exit "${RC}"