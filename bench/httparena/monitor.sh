#!/usr/bin/env bash
set -euo pipefail

FW="${1:-unknown}"
PROF="${2:-unknown}"
EVENT_PID="${3:-unknown}"
BENCHMARK_PID="${4:-unknown}"

OUT_DIR="${HOME}/results/loadgen"
mkdir -p "${OUT_DIR}"

LOG="${OUT_DIR}/${FW}-${PROF}_monitor.log"
RAW="${OUT_DIR}/${FW}-${PROF}_fd.tsv"
INSPECT="${OUT_DIR}/${FW}-${PROF}_docker_inspect.jsonl"
INSPECT_SEEN_FILE="${OUT_DIR}/${FW}-${PROF}_docker_inspect.seen"
: > "${INSPECT_SEEN_FILE}"

echo "[monitor] started fw=${FW} prof=${PROF}" >> "${LOG}"

while true; do
    echo "[monitor] tick fw=${FW} prof=${PROF} ts=$(date -Is)" >> "${LOG}"

    ts=$(date -Is)


    echo "$(date -Is) docker_ps=$(sudo docker ps -a --format '{{.ID}}:{{.Status}}' | tr '\n' '|')" >> ${LOG}

    # Sprint 34: capture `docker inspect` for each container the first
    # time it appears in `docker ps`.  HttpArena spawns containers with
    # `--rm`, so by the time runner.sh's post-benchmark inspect pass
    # runs the containers are already reaped.  Capturing per-tick from
    # the live `ps` set means we record HostConfig.Ulimits and
    # HostConfig.CpusetCpus while the container is still alive — the
    # only on-host proof that the patched scripts' --ulimit / --cpuset
    # flags actually reached `docker run`.
    while read -r cid; do
        [ -n "${cid}" ] || continue
        if ! grep -qxF "${cid}" "${INSPECT_SEEN_FILE}" 2>/dev/null; then
            echo "${cid}" >> "${INSPECT_SEEN_FILE}"
            sudo docker inspect "${cid}" 2>/dev/null \
                | jq -c --arg ts "${ts}" --arg fw "${FW}" --arg prof "${PROF}" \
                    '.[] | {ts: $ts, fw: $fw, prof: $prof, id: .Id, name: .Name, image: .Config.Image, ulimits: .HostConfig.Ulimits, cpuset_cpus: .HostConfig.CpusetCpus, network_mode: .HostConfig.NetworkMode}' \
                >> "${INSPECT}" || true
        fi
    done < <(sudo docker ps -q 2>/dev/null)

    # Sprint 34: feed the per-PID loop from `ps`.  Pre-Sprint-34 this
    # while-read had no stdin source so `_fd.tsv` only ever recorded the
    # outer docker_ps tick — the per-PID FD/socket counts that were the
    # point of the monitor never landed.  ``sudo`` so /proc rows for
    # docker-owned containers are readable (container processes show
    # up under the host PID namespace, owned by root).
    sudo ps -eo pid,comm --no-headers | while read -r pid cmd; do

        [ -d "/proc/${pid}/fd" ] || continue

        fd_count=$(sudo find "/proc/${pid}/fd" -mindepth 1 -maxdepth 1 2>/dev/null | wc -l)

        socket_count=$(sudo find "/proc/${pid}/fd" -mindepth 1 -maxdepth 1 \
            -lname 'socket:*' 2>/dev/null | wc -l)

        printf "%s\t%s\t%s\t%s\t%s\t%s\n" \
            "${ts}" "${FW}" "${PROF}" "${pid}" "${cmd}" "${fd_count}:${socket_count}"

    done >> "${RAW}"

    sleep 5
done