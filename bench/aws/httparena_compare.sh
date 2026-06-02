#!/usr/bin/env bash
# bench/aws/httparena_compare.sh — EC2 HttpArena cross-check.
#
# Provisions one EC2 instance, installs Docker + HttpArena's load
# tooling (gcannon, wrk, h2load), clones MDA2AV/HttpArena, vendors
# bench/httparena/ as the `blackbull` framework, installs BlackBull
# from PyPI in the container, runs HttpArena's official
# scripts/validate.sh and scripts/benchmark.sh, pulls results back,
# and tears the instance down.
#
# Cost estimate: c7i.2xlarge at ~$0.36/hr × ~30 min = ~$0.18.
# Override INSTANCE_TYPE to c7i.xlarge (~$0.18/hr) for ~$0.09.
#
# Usage:
#   bash bench/aws/httparena_compare.sh
#
# Env knobs:
#   PROFILES   space-separated HttpArena profile names
#              (default: "baseline json json-tls static")
#   FRAMEWORKS space-separated framework names to run
#              (default: "blackbull fastapi")
#   BLACKBULL_VERSION  PyPI version pin (default: pyproject.toml's version)
#   SPRINT_TAG  prefix on the result directory (default: sprint29)
#   SKIP_VALIDATE   set to 1 to skip the 49-point correctness check
#   KEEP_INSTANCE   set to 1 to leave the EC2 instance running on exit
#                   (for debugging — REMEMBER to `bash bench/aws/down.sh`)

set -euo pipefail

# Pick a roomier instance than `config.sh`'s 4-vCPU default — HttpArena
# colocates loadgen + framework in the same VM, so 8 vCPUs gives enough
# headroom that the loadgen isn't competing with the framework for CPU.
# Set BEFORE sourcing config.sh so config.sh's `: "${INSTANCE_TYPE:=...}"`
# default no-ops (env-set value wins).  Override with the env var.
: "${INSTANCE_TYPE:=c7i.2xlarge}"
export INSTANCE_TYPE

# shellcheck source=config.sh
source "$(dirname "$0")/config.sh"
_bench_aws_check_env

# Force single-host topology (HttpArena runs everything in containers
# on one host with --network host).
export TOPO=single

PROFILES="${PROFILES:-baseline json json-tls static}"
FRAMEWORKS="${FRAMEWORKS:-blackbull fastapi}"
KEEP_INSTANCE="${KEEP_INSTANCE:-0}"
SKIP_VALIDATE="${SKIP_VALIDATE:-0}"

TS="$(date -u +%Y%m%d-%H%M%SZ)"
SPRINT_TAG="${SPRINT_TAG:-sprint29}"
LOCAL_DEST="$REPO_ROOT/bench/results/httparena/${SPRINT_TAG}-${TS}"
mkdir -p "$LOCAL_DEST"

echo "=== bench/aws/httparena_compare.sh ==="
echo "  destination:   $LOCAL_DEST"
echo "  instance type: $INSTANCE_TYPE"
echo "  profiles:      $PROFILES"
echo "  frameworks:    $FRAMEWORKS"
echo

# ---------------------------------------------------------------------------
# Step 0 — resolve the BlackBull version we want installed on the EC2
# instance.  0.28.0 onward is on PyPI, so we install from there instead
# of building + uploading a wheel.  This matches the install path real
# users follow.  Override BLACKBULL_VERSION to test a different release.
# ---------------------------------------------------------------------------
BLACKBULL_VERSION="${BLACKBULL_VERSION:-$(grep -E '^version' "$REPO_ROOT/pyproject.toml" | sed -E 's/.*"([^"]+)".*/\1/')}"
echo ">>> BlackBull version: $BLACKBULL_VERSION (from PyPI)"

# ---------------------------------------------------------------------------
# Step 1 — provision EC2 (and arm a teardown trap so we don't leak the
# instance on error or Ctrl-C).
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
# Step 2 — install Docker + HttpArena load-generator tooling (gcannon,
# wrk, h2load).  Sprint 28 Task 4: the Task 3 first pass skipped these
# and HttpArena's benchmark.sh reported 0 req/s for every run.
#
# liburing 2.9 + gcannon build from source (HttpArena's docs as of
# 2026-05-31 — no pre-built gcannon binary distribution).  wrk and
# h2load come from apt (nghttp2-client provides h2load on Ubuntu).
# Kernel 6.1+ with io_uring is a gcannon precondition; the c7i.xlarge
# Ubuntu 24.04 AMI ships kernel 6.8+, so the precondition is met.
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

    # liburing 2.9 (gcannon dep) — build from source, install to /usr.
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

    # gcannon (HttpArena io_uring load generator).
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

    # Verify the three load tools are now resolvable.
    command -v gcannon >/dev/null || { echo "FATAL: gcannon not on PATH" >&2; exit 1; }
    command -v wrk     >/dev/null || { echo "FATAL: wrk not on PATH" >&2; exit 1; }
    command -v h2load  >/dev/null || { echo "FATAL: h2load not on PATH" >&2; exit 1; }
'

# ---------------------------------------------------------------------------
# Step 3 — clone HttpArena fresh on the instance.
# ---------------------------------------------------------------------------
echo ">>> cloning MDA2AV/HttpArena on the instance ..."
ssh "${SSH_OPTS[@]}" "$SERVER_REMOTE" '
    set -euo pipefail
    cd ~
    rm -rf HttpArena
    git clone --depth 1 https://github.com/MDA2AV/HttpArena.git
'

# ---------------------------------------------------------------------------
# Step 4 — vendor bench/httparena/ as the `blackbull` framework.  Rewrite
# the Dockerfile to install from the wheel we built in step 0 (no need
# for the BlackBull source tree on the instance).  Flip meta.json
# enabled=true so HttpArena's harness picks it up.
# ---------------------------------------------------------------------------
echo ">>> staging blackbull framework dir on the instance ..."
ssh "${SSH_OPTS[@]}" "$SERVER_REMOTE" 'mkdir -p HttpArena/frameworks/blackbull'

# Upload framework files only — no wheel.  BlackBull is installed
# from PyPI inside the container build.
rsync -e "ssh ${SSH_OPTS[*]}" -az --delete \
    "$REPO_ROOT/bench/httparena/app.py" \
    "$REPO_ROOT/bench/httparena/launcher.py" \
    "$REPO_ROOT/bench/httparena/meta.json" \
    "$SERVER_REMOTE:HttpArena/frameworks/blackbull/"

# Generate a Dockerfile that installs BlackBull from PyPI.  Same
# install path real adopters follow; reproducible by anyone with the
# version string.
ssh "${SSH_OPTS[@]}" "$SERVER_REMOTE" "cat > HttpArena/frameworks/blackbull/Dockerfile" <<EOF
# Auto-generated by bench/aws/httparena_compare.sh.
# Installs BlackBull from PyPI (no source tree on the instance).
FROM python:3.13-slim
WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \\
    PYTHONUNBUFFERED=1 \\
    PIP_NO_CACHE_DIR=1 \\
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN pip install --no-cache-dir 'blackbull[compression]==${BLACKBULL_VERSION}'

COPY app.py launcher.py /app/

EXPOSE 8080 8081 8443
CMD ["python", "launcher.py"]
EOF

# Flip meta.json enabled=true on the remote copy.
ssh "${SSH_OPTS[@]}" "$SERVER_REMOTE" \
    'sed -i "s/\"enabled\": false/\"enabled\": true/" HttpArena/frameworks/blackbull/meta.json'

echo "    staged."

# ---------------------------------------------------------------------------
# Step 5 — run HttpArena's official validate + benchmark scripts for
# each (framework × profile) combination.  Output is captured under
# ~/results/ on the instance and rsync'd back at the end.
# ---------------------------------------------------------------------------
ssh "${SSH_OPTS[@]}" "$SERVER_REMOTE" 'mkdir -p results'

if [ "$SKIP_VALIDATE" != "1" ]; then
    echo ">>> HttpArena validate (correctness check) ..."
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
# Step 6 — pull all logs + any HttpArena-generated result artefacts back.
# ---------------------------------------------------------------------------
echo ">>> pulling artefacts back to $LOCAL_DEST ..."
rsync -e "ssh ${SSH_OPTS[*]}" -az \
    "$SERVER_REMOTE:results/" "$LOCAL_DEST/logs/"

# HttpArena may emit per-run JSON / TSV under a known dir; grab the lot
# regardless of where it landed (best-effort).
rsync -e "ssh ${SSH_OPTS[*]}" -az --include='*/' --include='*.json' \
    --include='*.tsv' --include='*.csv' --include='*.md' --exclude='*' \
    "$SERVER_REMOTE:HttpArena/" "$LOCAL_DEST/httparena-tree/" || true

# Record provenance.
cat > "$LOCAL_DEST/provenance.md" <<EOF
# HttpArena EC2 cross-check

- Timestamp:  $TS
- Sprint tag: $SPRINT_TAG
- Instance:   $INSTANCE_TYPE in $REGION
- Public IP:  $SERVER_PUBLIC_IP
- BlackBull:  blackbull==$BLACKBULL_VERSION (from PyPI; repo commit $(cd "$REPO_ROOT" && git rev-parse --short HEAD))
- Profiles:   $PROFILES
- Frameworks: $FRAMEWORKS
EOF

echo
echo "=== complete ==="
echo "Artefacts at: $LOCAL_DEST"
echo "  Validate logs:  $LOCAL_DEST/logs/validate-*.log"
echo "  Benchmark logs: $LOCAL_DEST/logs/benchmark-*-*.log"
echo "  Provenance:     $LOCAL_DEST/provenance.md"
echo
echo "Instance will be torn down by the EXIT trap."
