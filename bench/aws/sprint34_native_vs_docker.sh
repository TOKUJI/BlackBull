#!/usr/bin/env bash
# bench/aws/sprint34_native_vs_docker.sh — direct A/B on one EC2 instance.
#
# Settles whether the 10× HttpArena-static gap (Sprint 33 native 36,961
# r/s vs Dockerized 3,500 r/s on c7i.8xlarge) is caused by the Docker /
# HttpArena harness layer or by something the code itself does.
#
# Same instance, same wrk, same static dataset, same wrk shape.  Only
# the server runtime differs:
#   A — native:  blackbull[compression]==0.31.0 in a venv, single
#                process with --workers=16 on :8080.  Mirrors Sprint
#                33's probe_app.py.
#   B — docker:  the same image httparena_compare.new.sh builds,
#                run with BB_HTTPARENA_PORTS=http + WEB_WORKERS=16,
#                no HttpArena scripts in the loop.
#
# Conn sweep includes c=64 (low-load latency floor — if it's still
# ~230ms in docker, the bottleneck is per-request CPU, not queueing).
#
# Cost: c7i.8xlarge ~$1.45/hr × ~20 min ≈ $0.50.
#
# Usage:
#   bash bench/aws/sprint34_native_vs_docker.sh
#
# Env knobs:
#   WORKERS    BlackBull worker count (default 16)
#   CONNS      comma-list of wrk connection counts
#              (default "64,1024,4096,6800")
#   DURATION_S wrk run length (default 5)
#   THREADS    wrk -t value (default 64)

set -euo pipefail

: "${INSTANCE_TYPE:=c7i.8xlarge}"
export INSTANCE_TYPE

# shellcheck source=config.sh
source "$(dirname "$0")/config.sh"
_bench_aws_check_env
export TOPO=single

WORKERS="${WORKERS:-16}"
CONNS="${CONNS:-64,1024,4096,6800}"
DURATION_S="${DURATION_S:-5}"
THREADS="${THREADS:-64}"
KEEP_INSTANCE="${KEEP_INSTANCE:-0}"
# Point at a locally-built wheel to install in place of PyPI's
# blackbull 0.31.0.  Used to A/B branches (eg perf-compression-passthrough)
# against the current public release.  Empty = install from PyPI.
LOCAL_BB_WHEEL="${LOCAL_BB_WHEEL:-}"
# Static-dataset selection:
#   httparena         (default) — clone MDA2AV/HttpArena and use its
#                                  real data/static.  ~15.6 KB avg
#                                  response after br-sibling negotiation.
#   sprint33-synthetic            — regenerate Sprint 33's synthetic
#                                  dataset (1024-byte ascii pattern × kb).
#                                  Trivially compressible, ~50-byte .br
#                                  siblings.  Reproduces the 36,961 r/s
#                                  number measured by Sprint 33.
STATIC_DATA="${STATIC_DATA:-httparena}"

TS="$(date -u +%Y%m%d-%H%M%SZ)"
LOCAL_DEST="$REPO_ROOT/bench/results/aws/sprint34-native-vs-docker-${TS}"
mkdir -p "$LOCAL_DEST"

echo "=== sprint34_native_vs_docker.sh ==="
echo "  destination:   $LOCAL_DEST"
echo "  instance type: $INSTANCE_TYPE"
echo "  workers:       $WORKERS"
echo "  conns:         $CONNS"
echo "  duration:      ${DURATION_S}s"
echo "  threads:       $THREADS"
echo

echo ">>> provisioning instance ..."
bash "$(dirname "$0")/up.sh"

_teardown() {
    local rc=$?
    if [ "$KEEP_INSTANCE" = "1" ]; then
        echo "KEEP_INSTANCE=1 — leaving EC2 alive; remember bash bench/aws/down.sh"
        return "$rc"
    fi
    bash "$(dirname "$0")/down.sh" || true
    return "$rc"
}
trap _teardown EXIT

_bench_aws_load_state

REMOTE="$SSH_USER@$SERVER_PUBLIC_IP"
echo "    instance: $SERVER_PUBLIC_IP"

# Wait for SSH to be up.  up.sh blocks on instance-running but the
# first few seconds the OpenSSH daemon may not yet be accepting.
for i in $(seq 1 30); do
    if ssh "${SSH_OPTS[@]}" -o ConnectTimeout=5 "$REMOTE" 'echo ready' >/dev/null 2>&1; then
        break
    fi
    sleep 5
done

# ---------------------------------------------------------------------------
# Step 1 — install python + wrk + docker on the instance, build a
# venv, install BlackBull from PyPI.  Same install path used adopters.
# ---------------------------------------------------------------------------
echo ">>> installing toolchain ..."
ssh "${SSH_OPTS[@]}" "$REMOTE" '
    set -euo pipefail
    sudo apt-get update -qq
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
        python3 python3-pip python3-venv wrk curl docker.io git rsync
    sudo systemctl enable --now docker
    sudo usermod -aG docker ubuntu 2>/dev/null || true
    python3 -m venv ~/venv
    ~/venv/bin/pip install --quiet --upgrade pip
'

if [ -n "$LOCAL_BB_WHEEL" ] && [ -f "$LOCAL_BB_WHEEL" ]; then
    WHEEL_NAME="$(basename "$LOCAL_BB_WHEEL")"
    echo ">>> installing local wheel: $WHEEL_NAME"
    rsync -e "ssh ${SSH_OPTS[*]}" -az "$LOCAL_BB_WHEEL" "$REMOTE:~/$WHEEL_NAME"
    ssh "${SSH_OPTS[@]}" "$REMOTE" "bash -s" <<REMOTE_PIP
set -euo pipefail
\$HOME/venv/bin/pip install --quiet "\$HOME/${WHEEL_NAME}[compression]"
REMOTE_PIP
else
    echo ">>> installing blackbull[compression]==0.31.0 from PyPI"
    ssh "${SSH_OPTS[@]}" "$REMOTE" \
        '$HOME/venv/bin/pip install --quiet "blackbull[compression]==0.31.0"'
fi

# ---------------------------------------------------------------------------
# Step 2 — stage HttpArena's static dataset on the instance for both
# the native run (mounted from /tmp/static) and the docker run (bind
# mount /tmp/static -> /data/static inside the container).
# ---------------------------------------------------------------------------
echo ">>> staging static dataset (mode: $STATIC_DATA) ..."
# Always clone HttpArena — we need the static-rotate.lua + dataset.json.
ssh "${SSH_OPTS[@]}" "$REMOTE" '
    set -euo pipefail
    rm -rf ~/HttpArena
    git clone --depth 1 https://github.com/MDA2AV/HttpArena.git ~/HttpArena
    cp ~/HttpArena/data/dataset.json /tmp/dataset.json
    cp ~/HttpArena/requests/static-rotate.lua /tmp/static-rotate.lua
    sudo rm -rf /tmp/static
'

case "$STATIC_DATA" in
    httparena)
        ssh "${SSH_OPTS[@]}" "$REMOTE" 'cp -r ~/HttpArena/data/static /tmp/static'
        ;;
    sprint33-synthetic)
        # Upload the generator + brotli, then run it on the remote.
        rsync -e "ssh ${SSH_OPTS[*]}" -az \
            "$REPO_ROOT/bench/httparena/sprint34_synthetic_data.py" \
            "$REMOTE:~/"
        ssh "${SSH_OPTS[@]}" "$REMOTE" '
            set -euo pipefail
            ~/venv/bin/pip install --quiet brotli
            mkdir -p /tmp/static
            ~/venv/bin/python3 ~/sprint34_synthetic_data.py /tmp/static
        '
        ;;
    *)
        echo "ERROR: unknown STATIC_DATA=$STATIC_DATA" >&2
        exit 1
        ;;
esac

# ---------------------------------------------------------------------------
# Step 3 — upload our probe app + remote driver.
# ---------------------------------------------------------------------------
echo ">>> uploading probe scripts ..."
rsync -e "ssh ${SSH_OPTS[*]}" -az \
    "$REPO_ROOT/bench/httparena/sprint34_probe_app.py" \
    "$REPO_ROOT/bench/httparena/sprint34_remote.sh" \
    "$REMOTE:~/"
ssh "${SSH_OPTS[@]}" "$REMOTE" 'chmod +x ~/sprint34_remote.sh'

# ---------------------------------------------------------------------------
# Step 4 — build the BlackBull docker image (same image
# httparena_compare.new.sh builds, but installed from PyPI inside this
# script's flow rather than via HttpArena).  WEB_WORKERS-aware
# launcher.py and the BB_HTTPARENA_PORTS knob ship along.
# ---------------------------------------------------------------------------
echo ">>> staging + building docker image ..."
ssh "${SSH_OPTS[@]}" "$REMOTE" 'mkdir -p ~/blackbull-image'
rsync -e "ssh ${SSH_OPTS[*]}" -az \
    "$REPO_ROOT/bench/httparena/app.py" \
    "$REPO_ROOT/bench/httparena/launcher.py" \
    "$REPO_ROOT/bench/httparena/logging_access.ini" \
    "$REMOTE:~/blackbull-image/"

if [ -n "$LOCAL_BB_WHEEL" ] && [ -f "$LOCAL_BB_WHEEL" ]; then
    WHEEL_NAME="$(basename "$LOCAL_BB_WHEEL")"
    rsync -e "ssh ${SSH_OPTS[*]}" -az "$LOCAL_BB_WHEEL" \
        "$REMOTE:~/blackbull-image/$WHEEL_NAME"
    ssh "${SSH_OPTS[@]}" "$REMOTE" "cat > ~/blackbull-image/Dockerfile" <<EOF
FROM python:3.13-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \\
    PYTHONUNBUFFERED=1 \\
    PIP_NO_CACHE_DIR=1 \\
    PIP_DISABLE_PIP_VERSION_CHECK=1
COPY ${WHEEL_NAME} /tmp/
RUN pip install --no-cache-dir /tmp/${WHEEL_NAME}[compression]
COPY app.py launcher.py /app/
EXPOSE 8080
CMD ["python", "launcher.py"]
EOF
else
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
fi

ssh "${SSH_OPTS[@]}" "$REMOTE" '
    set -euo pipefail
    cd ~/blackbull-image
    sudo docker build -t httparena-blackbull . 2>&1 | tail -3
'

# ---------------------------------------------------------------------------
# Step 5 — run the A/B.
# ---------------------------------------------------------------------------
echo ">>> running native + docker passes ..."
ssh "${SSH_OPTS[@]}" "$REMOTE" \
    "bash ~/sprint34_remote.sh '$CONNS' $DURATION_S $WORKERS $THREADS" \
    | tee "$LOCAL_DEST/remote.log"

# ---------------------------------------------------------------------------
# Step 6 — collect.
# ---------------------------------------------------------------------------
echo ">>> collecting results ..."
rsync -e "ssh ${SSH_OPTS[*]}" -az \
    "$REMOTE:~/sprint34_results/" \
    "$LOCAL_DEST/results/"

# ---------------------------------------------------------------------------
# Step 7 — summary.
# ---------------------------------------------------------------------------
echo
echo "=== summary ==="
for log in "$LOCAL_DEST/results"/*.log; do
    [ -f "$log" ] || continue
    tag="$(basename "$log" .log)"
    case "$tag" in
        native_server|docker_server) continue ;;
    esac
    rps=$(grep -oP 'Requests/sec:\s+\K[\d.]+' "$log" | head -1 || echo "—")
    lat=$(grep -oP 'Latency\s+\K[\d.]+\w+' "$log" | head -1 || echo "—")
    errs=$(grep -oP 'Socket errors:\s+\K.*' "$log" | head -1 || echo "—")
    printf "  %-32s  rps=%-12s  avg_lat=%-10s  errs=%s\n" \
        "$tag" "$rps" "$lat" "$errs"
done

echo
echo "=== complete ==="
echo "results: $LOCAL_DEST"
