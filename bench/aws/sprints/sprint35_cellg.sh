#!/usr/bin/env bash
# bench/aws/sprint35_cellg.sh — Cell G A/B for Sprint 35.
#
# Runs three frameworks on ONE EC2 instance:
#   blackbull-before — local wheel built from a master-HEAD baseline
#   blackbull-after  — local wheel built from the candidate branch
#   fastapi          — HttpArena's stock FastAPI image (reference)
#
# Single-instance keeps before vs after free of instance-to-instance
# noise.  FastAPI runs alongside as the apples-to-apples control —
# the BB/FA ratio on the static profile is the regression test for
# any candidate fix.
#
# Cost: c7i.8xlarge ~$1.45/hr × ~25 min ≈ $0.60.
#
# Required env knobs:
#   BB_WHEEL_BEFORE  absolute path to the baseline wheel
#   BB_WHEEL_AFTER   absolute path to the candidate wheel
#
# Optional env knobs:
#   WORKERS    BB / FastAPI worker count (default 16)
#   CONNS      comma-list of wrk c values (default "64,256,512,1024")
#   DURATION_S wrk per-cell duration (default 5)
#   KEEP_INSTANCE  1 to leave the instance running on exit (default 0)

set -euo pipefail

: "${BB_WHEEL_BEFORE:?BB_WHEEL_BEFORE=<path> required (baseline wheel)}"
: "${BB_WHEEL_AFTER:?BB_WHEEL_AFTER=<path> required (candidate wheel)}"
[ -f "$BB_WHEEL_BEFORE" ] || { echo "BB_WHEEL_BEFORE not found: $BB_WHEEL_BEFORE" >&2; exit 1; }
[ -f "$BB_WHEEL_AFTER"  ] || { echo "BB_WHEEL_AFTER not found:  $BB_WHEEL_AFTER" >&2; exit 1; }

: "${INSTANCE_TYPE:=c7i.8xlarge}"
export INSTANCE_TYPE

# shellcheck source=config.sh
source "$(dirname "$0")/config.sh"
_bench_aws_check_env
export TOPO=single

WORKERS="${WORKERS:-16}"
CONNS="${CONNS:-64,256,512,1024}"
DURATION_S="${DURATION_S:-5}"
KEEP_INSTANCE="${KEEP_INSTANCE:-0}"

WHEEL_BEFORE_NAME="$(basename "$BB_WHEEL_BEFORE")"
WHEEL_AFTER_NAME="$(basename "$BB_WHEEL_AFTER")"

TS="$(date -u +%Y%m%d-%H%M%SZ)"
LOCAL_DEST="$REPO_ROOT/bench/results/aws/sprint35-cellg-${TS}"
mkdir -p "$LOCAL_DEST"

echo "=== sprint35_cellg.sh ==="
echo "  destination:   $LOCAL_DEST"
echo "  instance type: $INSTANCE_TYPE"
echo "  workers:       $WORKERS"
echo "  conns:         $CONNS"
echo "  duration:      ${DURATION_S}s"
echo "  wheel BEFORE:  $BB_WHEEL_BEFORE"
echo "  wheel AFTER:   $BB_WHEEL_AFTER"
echo

echo ">>> provisioning instance ..."
bash "$(dirname "$0")/up.sh"

_teardown() {
    local rc=$?
    [ "$KEEP_INSTANCE" = "1" ] && return "$rc"
    bash "$(dirname "$0")/down.sh" || true
    return "$rc"
}
trap _teardown EXIT

_bench_aws_load_state

REMOTE="$SSH_USER@$SERVER_PUBLIC_IP"
echo "    instance: $SERVER_PUBLIC_IP"

# Wait for ssh.
for i in $(seq 1 30); do
    if ssh "${SSH_OPTS[@]}" -o ConnectTimeout=5 "$REMOTE" 'echo ready' >/dev/null 2>&1; then
        break
    fi
    sleep 5
done

# ---------------------------------------------------------------------------
# Step 1 — install toolchain.
# ---------------------------------------------------------------------------
echo ">>> installing toolchain ..."
ssh "${SSH_OPTS[@]}" "$REMOTE" '
    set -euo pipefail
    sudo apt-get update -qq
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
        wrk curl docker.io git rsync
    sudo systemctl enable --now docker
'

# ---------------------------------------------------------------------------
# Step 2 — clone HttpArena for dataset + lua + fastapi Dockerfile.
# ---------------------------------------------------------------------------
echo ">>> staging HttpArena ..."
ssh "${SSH_OPTS[@]}" "$REMOTE" '
    set -euo pipefail
    rm -rf ~/HttpArena
    git clone --depth 1 https://github.com/MDA2AV/HttpArena.git ~/HttpArena
    cp ~/HttpArena/data/dataset.json /tmp/dataset.json
    cp ~/HttpArena/requests/static-rotate.lua /tmp/static-rotate.lua
    sudo rm -rf /tmp/static
    cp -r ~/HttpArena/data/static /tmp/static
'

# ---------------------------------------------------------------------------
# Step 3 — upload BB wheels + app.py / launcher.py + remote driver.
# ---------------------------------------------------------------------------
echo ">>> uploading wheels + driver ..."
ssh "${SSH_OPTS[@]}" "$REMOTE" '
    mkdir -p ~/bb-before ~/bb-after
'
rsync -e "ssh ${SSH_OPTS[*]}" -az \
    "$BB_WHEEL_BEFORE" \
    "$REMOTE:~/bb-before/${WHEEL_BEFORE_NAME}"
rsync -e "ssh ${SSH_OPTS[*]}" -az \
    "$BB_WHEEL_AFTER" \
    "$REMOTE:~/bb-after/${WHEEL_AFTER_NAME}"

rsync -e "ssh ${SSH_OPTS[*]}" -az \
    "$REPO_ROOT/bench/httparena/app.py" \
    "$REPO_ROOT/bench/httparena/launcher.py" \
    "$REPO_ROOT/bench/httparena/logging_access.ini" \
    "$REMOTE:~/bb-before/"
rsync -e "ssh ${SSH_OPTS[*]}" -az \
    "$REPO_ROOT/bench/httparena/app.py" \
    "$REPO_ROOT/bench/httparena/launcher.py" \
    "$REPO_ROOT/bench/httparena/logging_access.ini" \
    "$REMOTE:~/bb-after/"

rsync -e "ssh ${SSH_OPTS[*]}" -az \
    "$REPO_ROOT/bench/httparena/sprint35_cellg_remote.sh" \
    "$REMOTE:~/"
ssh "${SSH_OPTS[@]}" "$REMOTE" 'chmod +x ~/sprint35_cellg_remote.sh'

# ---------------------------------------------------------------------------
# Step 4 — build BB images from the uploaded wheels.
# ---------------------------------------------------------------------------
echo ">>> writing Dockerfiles + building BB-before / BB-after images ..."
ssh "${SSH_OPTS[@]}" "$REMOTE" "cat > ~/bb-before/Dockerfile" <<EOF
FROM python:3.13-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \\
    PYTHONUNBUFFERED=1 \\
    PIP_NO_CACHE_DIR=1 \\
    PIP_DISABLE_PIP_VERSION_CHECK=1
COPY ${WHEEL_BEFORE_NAME} /tmp/${WHEEL_BEFORE_NAME}
RUN pip install --no-cache-dir "/tmp/${WHEEL_BEFORE_NAME}[compression]"
COPY app.py launcher.py /app/
EXPOSE 8080
CMD ["python", "launcher.py"]
EOF

ssh "${SSH_OPTS[@]}" "$REMOTE" "cat > ~/bb-after/Dockerfile" <<EOF
FROM python:3.13-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \\
    PYTHONUNBUFFERED=1 \\
    PIP_NO_CACHE_DIR=1 \\
    PIP_DISABLE_PIP_VERSION_CHECK=1
COPY ${WHEEL_AFTER_NAME} /tmp/${WHEEL_AFTER_NAME}
RUN pip install --no-cache-dir "/tmp/${WHEEL_AFTER_NAME}[compression]"
COPY app.py launcher.py /app/
EXPOSE 8080
CMD ["python", "launcher.py"]
EOF

ssh "${SSH_OPTS[@]}" "$REMOTE" '
    set -euo pipefail
    cd ~/bb-before && sudo docker build -t httparena-bb-before . 2>&1 | tail -3
    cd ~/bb-after  && sudo docker build -t httparena-bb-after  . 2>&1 | tail -3
'

# ---------------------------------------------------------------------------
# Step 5 — build FastAPI image from HttpArena's framework Dockerfile.
# ---------------------------------------------------------------------------
echo ">>> building FastAPI image ..."
ssh "${SSH_OPTS[@]}" "$REMOTE" '
    set -euo pipefail
    cd ~/HttpArena
    sudo docker build -t httparena-fastapi frameworks/fastapi 2>&1 | tail -3
'

# ---------------------------------------------------------------------------
# Step 6 — sweep all three frameworks.
# ---------------------------------------------------------------------------
echo ">>> running 3-way sweep (bb-before / bb-after / fastapi) ..."
ssh "${SSH_OPTS[@]}" "$REMOTE" \
    "bash ~/sprint35_cellg_remote.sh '$CONNS' $DURATION_S $WORKERS" \
    | tee "$LOCAL_DEST/remote.log"

# ---------------------------------------------------------------------------
# Step 7 — collect results.
# ---------------------------------------------------------------------------
echo ">>> collecting results ..."
rsync -e "ssh ${SSH_OPTS[*]}" -az \
    "$REMOTE:~/sprint35_cellg_results/" \
    "$LOCAL_DEST/results/"

# ---------------------------------------------------------------------------
# Step 8 — summary table.
# ---------------------------------------------------------------------------
echo
echo "=== summary (c7i.8xlarge, $WORKERS workers, conns=$CONNS) ==="
printf "  %-36s  %-12s  %-12s  %-14s  %s\n" "tag" "rps" "avg_lat" "tx/sec" "errs"
for log in "$LOCAL_DEST/results"/*.log; do
    [ -f "$log" ] || continue
    tag="$(basename "$log" .log)"
    rps=$(grep -oP 'Requests/sec:\s+\K[\d.]+' "$log" | head -1 || echo "—")
    lat=$(grep -oP 'Latency\s+\K[\d.]+\w+' "$log" | head -1 || echo "—")
    tx=$(grep -oP 'Transfer/sec:\s+\K.*' "$log" | head -1 || echo "—")
    errs=$(grep -oP 'Socket errors:\s+\K.*' "$log" | head -1 || echo "0")
    printf "  %-36s  rps=%-12s  avg_lat=%-12s  tx=%-14s  errs=%s\n" \
        "$tag" "$rps" "$lat" "$tx" "$errs"
done

echo
echo "=== complete ==="
echo "results: $LOCAL_DEST"
