#!/usr/bin/env bash
# bench/aws/sprint33_static_perf.sh — Sprint 33 static-perf measurement.
#
# Three-way HttpArena comparison on a c7i.8xlarge:
#
#   1. blackbull           — local wheel (post-perf-static-cache-hot-path)
#   2. blackbull-uvloop    — same wheel + uvloop installed + BB_UVLOOP=1
#   3. fastapi             — HttpArena upstream reference (PyPI)
#
# Same provisioning shape as httparena_compare.sh, but installs
# BlackBull from a locally-built wheel so we measure the patch under
# evaluation rather than the last PyPI release.
#
# Usage:
#   bash bench/aws/sprint33_static_perf.sh
#
# Env knobs:
#   PROFILES        space-separated profiles (default: baseline json json-tls static)
#   KEEP_INSTANCE   set to 1 to leave the instance running on exit

set -euo pipefail

: "${INSTANCE_TYPE:=c7i.8xlarge}"
export INSTANCE_TYPE

# shellcheck source=config.sh
source "$(dirname "$0")/config.sh"
_bench_aws_check_env

export TOPO=single

PROFILES="${PROFILES:-baseline json json-tls static}"
FRAMEWORKS="blackbull blackbull-uvloop fastapi"
KEEP_INSTANCE="${KEEP_INSTANCE:-0}"
SKIP_VALIDATE="${SKIP_VALIDATE:-0}"

TS="$(date -u +%Y%m%d-%H%M%SZ)"
SPRINT_TAG="${SPRINT_TAG:-sprint33-static-perf}"
LOCAL_DEST="$REPO_ROOT/bench/results/httparena/${SPRINT_TAG}-${TS}"
mkdir -p "$LOCAL_DEST"

echo "=== bench/aws/sprint33_static_perf.sh ==="
echo "  destination:   $LOCAL_DEST"
echo "  instance type: $INSTANCE_TYPE"
echo "  profiles:      $PROFILES"
echo "  frameworks:    $FRAMEWORKS"
echo

# ---------------------------------------------------------------------------
# Step 0 — build a fresh wheel from the current branch so the container
# installs the patch under test, not the last PyPI release.
# ---------------------------------------------------------------------------
echo ">>> building wheel from current branch ..."
cd "$REPO_ROOT"
rm -f dist/blackbull-*.whl 2>/dev/null || true
python -m build --wheel >/dev/null
WHEEL_PATH="$(ls -1 "$REPO_ROOT"/dist/blackbull-*-py3-none-any.whl | head -1)"
WHEEL_NAME="$(basename "$WHEEL_PATH")"
echo "    wheel: $WHEEL_NAME"

# ---------------------------------------------------------------------------
# Step 1 — provision EC2.
# ---------------------------------------------------------------------------
echo ">>> bench/aws/up.sh ..."
bash "$(dirname "$0")/up.sh"

_teardown() {
    local rc=$?
    if [ "$KEEP_INSTANCE" = "1" ]; then
        echo "KEEP_INSTANCE=1 — leaving EC2 alive; remember to run 'bash bench/aws/down.sh'"
        return $rc
    fi
    echo ">>> bench/aws/down.sh (trap EXIT) ..."
    bash "$(dirname "$0")/down.sh" || true
    return $rc
}
trap _teardown EXIT

_bench_aws_load_state

SERVER_REMOTE="$SSH_USER@$SERVER_PUBLIC_IP"
echo "    instance: $SERVER_PUBLIC_IP"

# ---------------------------------------------------------------------------
# Step 2 — Docker + HttpArena load-generator tooling.
# ---------------------------------------------------------------------------
echo ">>> installing Docker + HttpArena load tooling on the instance ..."
ssh "${SSH_OPTS[@]}" "$SERVER_REMOTE" '
    set -euo pipefail
    sudo apt-get update -qq
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
        docker.io git jq curl ca-certificates \
        build-essential pkg-config \
        wrk nghttp2-client >/dev/null
    sudo systemctl enable --now docker >/dev/null
    sudo usermod -aG docker ubuntu

    if ! pkg-config --atleast-version=2.9 liburing 2>/dev/null; then
        echo "  building liburing 2.9 ..."
        cd /tmp
        rm -rf liburing
        git clone --quiet --depth 1 --branch liburing-2.9 \
            https://github.com/axboe/liburing.git
        cd liburing
        ./configure --prefix=/usr >/dev/null
        make -s -j"$(nproc)" -C src
        sudo make -s install -C src >/dev/null
        sudo ldconfig
        cd ..
    fi

    if ! command -v gcannon >/dev/null; then
        echo "  building gcannon ..."
        cd /tmp
        rm -rf gcannon
        git clone --quiet --depth 1 https://github.com/MDA2AV/gcannon.git
        cd gcannon
        make -s
        sudo cp gcannon /usr/local/bin/
        cd ..
    fi

    command -v gcannon >/dev/null || { echo "FATAL: gcannon not on PATH" >&2; exit 1; }
    command -v wrk     >/dev/null || { echo "FATAL: wrk not on PATH" >&2; exit 1; }
    command -v h2load  >/dev/null || { echo "FATAL: h2load not on PATH" >&2; exit 1; }
'

# ---------------------------------------------------------------------------
# Step 3 — clone HttpArena fresh.
# ---------------------------------------------------------------------------
echo ">>> cloning MDA2AV/HttpArena on the instance ..."
ssh "${SSH_OPTS[@]}" "$SERVER_REMOTE" '
    set -euo pipefail
    cd ~
    rm -rf HttpArena
    git clone --depth 1 https://github.com/MDA2AV/HttpArena.git
'

# ---------------------------------------------------------------------------
# Step 4 — stage `blackbull` and `blackbull-uvloop` frameworks.  Both use
# the same locally-built wheel + the same app.py / launcher.py.  Only
# the Dockerfile differs (uvloop install + BB_UVLOOP=1 env on the
# uvloop variant).
# ---------------------------------------------------------------------------
echo ">>> staging blackbull + blackbull-uvloop framework dirs ..."
for fw in blackbull blackbull-uvloop; do
    ssh "${SSH_OPTS[@]}" "$SERVER_REMOTE" "mkdir -p HttpArena/frameworks/$fw"
    rsync -e "ssh ${SSH_OPTS[*]}" -az \
        "$WHEEL_PATH" \
        "$REPO_ROOT/bench/httparena/app.py" \
        "$REPO_ROOT/bench/httparena/launcher.py" \
        "$REPO_ROOT/bench/httparena/meta.json" \
        "$SERVER_REMOTE:HttpArena/frameworks/$fw/"
done

# Plain blackbull — install the wheel, no uvloop.
ssh "${SSH_OPTS[@]}" "$SERVER_REMOTE" "cat > HttpArena/frameworks/blackbull/Dockerfile" <<EOF
# Auto-generated by bench/aws/sprint33_static_perf.sh.
# Installs BlackBull from the wheel uploaded alongside this Dockerfile.
FROM python:3.13-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \\
    PYTHONUNBUFFERED=1 \\
    PIP_NO_CACHE_DIR=1 \\
    PIP_DISABLE_PIP_VERSION_CHECK=1
COPY $WHEEL_NAME /tmp/
RUN pip install --no-cache-dir "/tmp/${WHEEL_NAME}[compression]"
COPY app.py launcher.py /app/
EXPOSE 8080 8081 8443
CMD ["python", "launcher.py"]
EOF

# blackbull-uvloop — install the wheel + uvloop, BB_UVLOOP=1 at runtime.
ssh "${SSH_OPTS[@]}" "$SERVER_REMOTE" "cat > HttpArena/frameworks/blackbull-uvloop/Dockerfile" <<EOF
# Auto-generated by bench/aws/sprint33_static_perf.sh.
FROM python:3.13-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \\
    PYTHONUNBUFFERED=1 \\
    PIP_NO_CACHE_DIR=1 \\
    PIP_DISABLE_PIP_VERSION_CHECK=1 \\
    BB_UVLOOP=1
COPY $WHEEL_NAME /tmp/
RUN pip install --no-cache-dir "/tmp/${WHEEL_NAME}[compression]" uvloop
COPY app.py launcher.py /app/
EXPOSE 8080 8081 8443
CMD ["python", "launcher.py"]
EOF

# Patch meta.json on both, including a distinct display_name on the
# uvloop variant.  Flip enabled=true so the harness picks each up.
ssh "${SSH_OPTS[@]}" "$SERVER_REMOTE" '
    sed -i "s/\"enabled\": false/\"enabled\": true/" \
        HttpArena/frameworks/blackbull/meta.json
    cp HttpArena/frameworks/blackbull/meta.json \
       HttpArena/frameworks/blackbull-uvloop/meta.json
    sed -i "s/\"display_name\": \"blackbull\"/\"display_name\": \"blackbull-uvloop\"/" \
        HttpArena/frameworks/blackbull-uvloop/meta.json
'

echo "    staged."

# ---------------------------------------------------------------------------
# Step 5 — validate + benchmark for each (fw × profile).
# ---------------------------------------------------------------------------
ssh "${SSH_OPTS[@]}" "$SERVER_REMOTE" 'mkdir -p results'

if [ "$SKIP_VALIDATE" != "1" ]; then
    echo ">>> HttpArena validate ..."
    for fw in $FRAMEWORKS; do
        echo "  - $fw"
        ssh "${SSH_OPTS[@]}" "$SERVER_REMOTE" "
            set -euo pipefail
            cd HttpArena
            sudo ./scripts/validate.sh $fw 2>&1 | tee ~/results/validate-${fw}.log
        " || echo "    (validate non-zero for $fw — kept going; see log)"
    done
fi

echo ">>> HttpArena benchmark ..."
for fw in $FRAMEWORKS; do
    for prof in $PROFILES; do
        echo "  - $fw / $prof"
        ssh "${SSH_OPTS[@]}" "$SERVER_REMOTE" "
            set -euo pipefail
            cd HttpArena
            sudo ./scripts/benchmark.sh $fw $prof 2>&1 \
                | tee ~/results/benchmark-${fw}-${prof}.log
        " || echo "    (benchmark non-zero for $fw / $prof — kept going)"
    done
done

# ---------------------------------------------------------------------------
# Step 6 — pull artefacts back.
# ---------------------------------------------------------------------------
echo ">>> pulling artefacts back to $LOCAL_DEST ..."
rsync -e "ssh ${SSH_OPTS[*]}" -az \
    "$SERVER_REMOTE:results/" "$LOCAL_DEST/logs/"

cat > "$LOCAL_DEST/provenance.md" <<EOF
# HttpArena EC2 cross-check — Sprint 33 static-perf

- Timestamp:  $TS
- Sprint tag: $SPRINT_TAG
- Instance:   $INSTANCE_TYPE in $REGION
- Public IP:  $SERVER_PUBLIC_IP
- BlackBull:  $WHEEL_NAME (built from $(cd "$REPO_ROOT" && git rev-parse --short HEAD) on branch $(cd "$REPO_ROOT" && git rev-parse --abbrev-ref HEAD))
- Profiles:   $PROFILES
- Frameworks: $FRAMEWORKS
EOF

echo
echo "=== complete ==="
echo "Artefacts at: $LOCAL_DEST"
