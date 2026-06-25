#!/usr/bin/env bash
# bench/aws/sprint34_compare_bb_fastapi.sh — BB vs FastAPI on
# HttpArena's baseline + static profiles, under conditions chosen to
# keep wrk well away from its own saturation point (low c values).
#
# Answers the question: when both frameworks are driven below the
# load generator's ceiling on the same machine with the same wrk
# shape, who serves how much?
#
# Cost: c7i.8xlarge ~$1.45/hr × ~20 min ≈ $0.50.
#
# Usage:
#   bash bench/aws/sprint34_compare_bb_fastapi.sh
#
# Env knobs:
#   WORKERS    BB / FastAPI worker count (default 16)
#   CONNS      comma-list of wrk c values (default "64,256,1024")
#   DURATION_S wrk per-cell duration (default 5)

set -euo pipefail

: "${INSTANCE_TYPE:=c7i.8xlarge}"
export INSTANCE_TYPE

# shellcheck source=config.sh
source "$(dirname "$0")/config.sh"
_bench_aws_check_env
export TOPO=single

WORKERS="${WORKERS:-16}"
CONNS="${CONNS:-64,256,1024}"
DURATION_S="${DURATION_S:-5}"
KEEP_INSTANCE="${KEEP_INSTANCE:-0}"

TS="$(date -u +%Y%m%d-%H%M%SZ)"
LOCAL_DEST="$REPO_ROOT/bench/results/aws/sprint34-compare-bb-fastapi-${TS}"
mkdir -p "$LOCAL_DEST"

echo "=== sprint34_compare_bb_fastapi.sh ==="
echo "  destination:   $LOCAL_DEST"
echo "  instance type: $INSTANCE_TYPE"
echo "  workers:       $WORKERS"
echo "  conns:         $CONNS"
echo "  duration:      ${DURATION_S}s"
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
# Step 1 — install Docker + wrk + git on the instance.
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
# Step 2 — clone HttpArena: needs the static dataset, lua script, AND
# the fastapi framework's Dockerfile (we use it verbatim).
# ---------------------------------------------------------------------------
echo ">>> staging HttpArena (for dataset + fastapi image) ..."
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
# Step 3 — upload BlackBull bench app + remote driver.
# ---------------------------------------------------------------------------
echo ">>> uploading probe + driver scripts ..."
rsync -e "ssh ${SSH_OPTS[*]}" -az \
    "$REPO_ROOT/bench/httparena/sprint34_compare_remote.sh" \
    "$REMOTE:~/"
ssh "${SSH_OPTS[@]}" "$REMOTE" 'chmod +x ~/sprint34_compare_remote.sh'

# ---------------------------------------------------------------------------
# Step 4 — build BlackBull image (PyPI 0.31.0 wheel + our app.py).
# Same image httparena_compare.new.sh builds.
# ---------------------------------------------------------------------------
echo ">>> building BlackBull image ..."
ssh "${SSH_OPTS[@]}" "$REMOTE" 'mkdir -p ~/blackbull-image'
rsync -e "ssh ${SSH_OPTS[*]}" -az \
    "$REPO_ROOT/bench/httparena/app.py" \
    "$REPO_ROOT/bench/httparena/launcher.py" \
    "$REPO_ROOT/bench/httparena/logging_access.ini" \
    "$REMOTE:~/blackbull-image/"

ssh "${SSH_OPTS[@]}" "$REMOTE" "cat > ~/blackbull-image/Dockerfile" <<'EOF'
FROM python:3.13-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
RUN pip install --no-cache-dir 'blackbull[compression]==0.31.0'
COPY app.py launcher.py /app/
EXPOSE 8080
CMD ["python", "launcher.py"]
EOF

ssh "${SSH_OPTS[@]}" "$REMOTE" '
    set -euo pipefail
    cd ~/blackbull-image
    sudo docker build -t httparena-blackbull . 2>&1 | tail -3
'

# ---------------------------------------------------------------------------
# Step 5 — build FastAPI image straight from HttpArena's framework dir.
# Flip meta.json enabled=true the way httparena_compare.new.sh does for
# blackbull, then let docker build do its thing.
# ---------------------------------------------------------------------------
echo ">>> building FastAPI image (HttpArena's framework Dockerfile) ..."
ssh "${SSH_OPTS[@]}" "$REMOTE" '
    set -euo pipefail
    cd ~/HttpArena
    sudo docker build -t httparena-fastapi frameworks/fastapi 2>&1 | tail -3
'

# ---------------------------------------------------------------------------
# Step 6 — sweep.
# ---------------------------------------------------------------------------
echo ">>> running BB vs FastAPI sweep ..."
ssh "${SSH_OPTS[@]}" "$REMOTE" \
    "bash ~/sprint34_compare_remote.sh '$CONNS' $DURATION_S $WORKERS" \
    | tee "$LOCAL_DEST/remote.log"

# ---------------------------------------------------------------------------
# Step 7 — collect.
# ---------------------------------------------------------------------------
echo ">>> collecting results ..."
rsync -e "ssh ${SSH_OPTS[*]}" -az \
    "$REMOTE:~/sprint34_compare_results/" \
    "$LOCAL_DEST/results/"

# ---------------------------------------------------------------------------
# Step 8 — summary.
# ---------------------------------------------------------------------------
echo
echo "=== summary (apples-to-apples on c7i.8xlarge, $WORKERS workers) ==="
printf "  %-32s  %-10s  %-10s  %-12s  %s\n" "tag" "rps" "avg_lat" "tx/sec" "errs"
for log in "$LOCAL_DEST/results"/*.log; do
    [ -f "$log" ] || continue
    tag="$(basename "$log" .log)"
    rps=$(grep -oP 'Requests/sec:\s+\K[\d.]+' "$log" | head -1 || echo "—")
    lat=$(grep -oP 'Latency\s+\K[\d.]+\w+' "$log" | head -1 || echo "—")
    tx=$(grep -oP 'Transfer/sec:\s+\K.*' "$log" | head -1 || echo "—")
    errs=$(grep -oP 'Socket errors:\s+\K.*' "$log" | head -1 || echo "0")
    printf "  %-32s  rps=%-10s  avg_lat=%-10s  tx=%-12s  errs=%s\n" \
        "$tag" "$rps" "$lat" "$tx" "$errs"
done

echo
echo "=== complete ==="
echo "results: $LOCAL_DEST"
