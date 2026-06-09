#!/usr/bin/env bash
set -euo pipefail

FW="${1:-unknown}"
PROF="${2:-unknown}"

OUT_DIR="${HOME}/results/loadgen"
mkdir -p "${OUT_DIR}"

LOG="${OUT_DIR}/${FW}-${PROF}_postprocess.log"
RAW="${OUT_DIR}/${FW}-${PROF}_fd.tsv"

echo "[postprocess] called fw=${FW} prof=${PROF}" >> "${LOG}"
echo "[postprocess] timestamp=$(date -Is)" >> "${LOG}"


cat "${RAW}" | jq -R '
split("\t") |
{
  ts: .[0],
  fw: .[1],
  prof: .[2],
  pid: (.[3] | tonumber),
  cmd: .[4],
  fd_count: (.[5] | split(":")[0] | tonumber),
  socket_count: (.[5] | split(":")[1] | tonumber)
}
'