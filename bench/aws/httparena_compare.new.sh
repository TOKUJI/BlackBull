#!/usr/bin/env bash

# bench/aws/httparena_compare.sh — EC2 HttpArena cross-check
set -euo pipefail

# ---------------------------------------------------------------------------
# Instance / global settings
# ---------------------------------------------------------------------------

: "${INSTANCE_TYPE:=c7i.2xlarge}"
export INSTANCE_TYPE

source "$(dirname "$0")/config.sh"
_bench_aws_check_env

export TOPO=single

PROFILES="${PROFILES:-baseline json json-tls static}"
FRAMEWORKS="${FRAMEWORKS:-blackbull fastapi}"

KEEP_INSTANCE="${KEEP_INSTANCE:-0}"
# Run HttpArena's validate.sh once at the top before benchmarking.
# 0 = run validate first (default; ~30 s × 1 catches a build break before
# the sweep burns minutes); 1 = skip entirely.
SKIP_VALIDATE="${SKIP_VALIDATE:-0}"

TS="$(date -u +%Y%m%d-%H%M%SZ)"
SPRINT_TAG="${SPRINT_TAG:-unnamedsprint}"

LOCAL_DEST="$REPO_ROOT/bench/results/httparena/${SPRINT_TAG}-${TS}"
mkdir -p "$LOCAL_DEST"

# ---------------------------------------------------------------------------
# Web server tuning
# ---------------------------------------------------------------------------

: "${WEB_WORKERS:=}"
: "${WEB_NOFILE:=65536}"

# ---------------------------------------------------------------------------
# wrk tuning
# ---------------------------------------------------------------------------

: "${LOADGEN_CPUS:=}"
: "${LOADGEN_NOFILE:=65536}"

# ---------------------------------------------------------------------------
# BlackBull options
# ---------------------------------------------------------------------------

: "${LOCAL_BB_WHEEL:=0}"
: "${BB_ACCESS_LOG:=0}"
: "${BLACKBULL_VERSION:=$(grep -E '^version' "$REPO_ROOT/pyproject.toml" \
    | sed -E 's/.*"([^"]+)".*/\1/') }"

# ---------------------------------------------------------------------------
# CPU affinity validation & normalization.  ``LOADGEN_CPUS`` is forwarded
# to runner.sh, which exports it as ``GCANNON_CPUS`` — the env var
# HttpArena's benchmark.sh natively reads for both DOCKER_FLAGS
# (--cpuset-cpus) and the native `taskset -c` wrk invocation.
# ---------------------------------------------------------------------------

if [ -n "${LOADGEN_CPUS:-}" ]; then
    if ! [[ "${LOADGEN_CPUS}" =~ ^[0-9]+$ || "${LOADGEN_CPUS}" =~ ^[0-9]+-[0-9,-]+$ ]]; then
        echo "ERROR: invalid LOADGEN_CPUS='${LOADGEN_CPUS}' (must be N, N-M, or taskset cpuset string)" >&2
        exit 1
    fi
fi

echo "    [debug] LOADGEN_CPUS=${LOADGEN_CPUS:-unset}" >&2

# ---------------------------------------------------------------------------
# Logging header
# ---------------------------------------------------------------------------

echo "=== httparena_compare.sh ==="
echo "destination:   $LOCAL_DEST"
echo "instance:      $INSTANCE_TYPE"
echo "profiles:      $PROFILES"
echo "frameworks:    $FRAMEWORKS"
echo "--- tuning ---"
echo "WEB_WORKERS:   ${WEB_WORKERS:-<auto>}"
echo "WEB_NOFILE:    $WEB_NOFILE"
echo "LOADGEN_CPUS:      $LOADGEN_CPUS"
echo "LOADGEN_NOFILE:    $LOADGEN_NOFILE"
echo "BB_ACCESS_LOG: $BB_ACCESS_LOG"
echo

# ---------------------------------------------------------------------------
# Step 0: resolve BlackBull version / wheel
# ---------------------------------------------------------------------------

if [ "$LOCAL_BB_WHEEL" = "1" ]; then
    echo ">>> LOCAL wheel mode enabled"
    if ! python3 -c 'import build' 2>/dev/null; then
        echo "ERROR: python build module missing (pip install build)" >&2
        exit 1
    fi

    (cd "$REPO_ROOT" && python3 -m build --wheel --outdir dist/ >/dev/null)

    LOCAL_WHEEL="$(ls -t "$REPO_ROOT"/dist/blackbull-*.whl | head -1 || true)"
    if [ -z "$LOCAL_WHEEL" ]; then
        echo "ERROR: wheel build failed" >&2
        exit 1
    fi

    LOCAL_WHEEL_NAME="$(basename "$LOCAL_WHEEL")"
    INSTALL_CMD="pip install --no-cache-dir /tmp/${LOCAL_WHEEL_NAME}[compression]"
    COPY_WHEEL="COPY ${LOCAL_WHEEL_NAME} /tmp/"
    _LOGGING_INI_COPY='COPY logging_access.ini /app/'

    echo "wheel: $LOCAL_WHEEL_NAME"

else
    INSTALL_CMD="pip install --no-cache-dir 'blackbull[compression]==${BLACKBULL_VERSION}'"
    COPY_WHEEL=""
    _LOGGING_INI_COPY=''
    echo ">>> PyPI mode: blackbull==$BLACKBULL_VERSION"
fi

_collect_logs() {
    set +e

    echo ">>> collecting logs ..." >&2

    rsync -e "ssh ${SSH_OPTS[*]}" -az \
        "$SERVER_REMOTE:~/results/" \
        "$LOCAL_DEST/results/"

    rsync -e "ssh ${SSH_OPTS[*]}" -az \
        "$SERVER_REMOTE:~/HttpArena/" \
        "$LOCAL_DEST/httparena-tree/"

    mkdir -p "$LOCAL_DEST/_snapshot"
    cp -a "$LOCAL_DEST/results" "$LOCAL_DEST/_snapshot/" 2>/dev/null

    set -e
}

# ---------------------------------------------------------------------------
# Step 1: provision EC2
# ---------------------------------------------------------------------------

echo ">>> provisioning instance ..."
bash "$(dirname "$0")/up.sh"

_teardown() {
    local _exit_code=$?

    # Always collect logs regardless of outcome.
    _collect_logs || true

    if [ "$_exit_code" -ne 0 ]; then
        echo "_teardown: script exited with code $_exit_code — forcing teardown"
        bash "$(dirname "$0")/down.sh" || true
        return "$_exit_code"
    fi

    if [ "$KEEP_INSTANCE" = "1" ]; then
        echo "KEEP_INSTANCE=1 (skipping teardown on success)"
        return 0
    fi

    bash "$(dirname "$0")/down.sh" || true
    return 0
}
trap _teardown EXIT

_bench_aws_load_state

SERVER_REMOTE="$SSH_USER@$SERVER_PUBLIC_IP"
echo "instance: $SERVER_PUBLIC_IP"

# ---------------------------------------------------------------------------
# Step 1.5: Preparation on server: create results dir
# ---------------------------------------------------------------------------
ssh "${SSH_OPTS[@]}" "$SERVER_REMOTE" '
set -euo pipefail
mkdir -p ~/results
mkdir -p ~/src
'

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
# Step 3: clone HttpArena
# ---------------------------------------------------------------------------

ssh "${SSH_OPTS[@]}" "$SERVER_REMOTE" '
set -euo pipefail
cd ~
rm -rf HttpArena
git clone --depth 1 https://github.com/MDA2AV/HttpArena.git
'

# ---------------------------------------------------------------------------
# Step 3.5: patch HttpArena scripts to honour Sprint-34 env knobs.
# Implementation lives in bench/httparena/patch_httparena.py — uploaded
# to the EC2 instance and invoked with ~/HttpArena as the argv.  Keeping
# the logic in a local file (rather than inline-heredoc'd through ssh)
# is the [[feedback-remote-scripts-via-upload]] pattern.
#
# Touches three files in the fresh HttpArena clone:
#   scripts/lib/common.sh    — honour pre-set HARD_NOFILE (Cell D).
#   scripts/lib/framework.sh — inject -e WEB_WORKERS into framework
#                              docker run args (Cell B).
#   scripts/benchmark.sh     — env-drive --ulimit nofile in
#                              LOADGEN_DOCKER=true mode (Cell A docker).
# Each substitution asserts exactly-one match; missing match aborts.
# ---------------------------------------------------------------------------
echo ">>> patching HttpArena scripts to honour Sprint-34 env knobs ..."
rsync -e "ssh ${SSH_OPTS[*]}" -az \
    "$REPO_ROOT/bench/httparena/patch_httparena.py" \
    "$SERVER_REMOTE:~/src/"
ssh "${SSH_OPTS[@]}" "$SERVER_REMOTE" 'python3 ~/src/patch_httparena.py ~/HttpArena'

# ---------------------------------------------------------------------------
# Step 4: framework staging (blackbull)
# ---------------------------------------------------------------------------

ssh "${SSH_OPTS[@]}" "$SERVER_REMOTE" "mkdir -p HttpArena/frameworks/blackbull"

# Upload framework files.  In LOCAL_BB_WHEEL=1 mode the wheel is also
# uploaded so the Dockerfile can COPY+install it from the build context.
# When BB_ACCESS_LOG=1, logging_access.ini is also uploaded: app.py loads it
# via logging.config.fileConfig() to configure the blackbull.access logger
# declaratively (standard Python logging config, not inline handler setup).
_BB_RSYNC_FILES=(
    "$REPO_ROOT/bench/httparena/app.py"
    "$REPO_ROOT/bench/httparena/launcher.py"
    "$REPO_ROOT/bench/httparena/meta.json"
    "$REPO_ROOT/bench/httparena/logging_access.ini"
)
rsync -e "ssh ${SSH_OPTS[*]}" -az \
    "${_BB_RSYNC_FILES[@]}" \
    "$SERVER_REMOTE:HttpArena/frameworks/blackbull/"

if [ "$LOCAL_BB_WHEEL" = "1" ]; then
    echo "    uploading wheel $LOCAL_WHEEL_NAME ..."
    rsync -e "ssh ${SSH_OPTS[*]}" -az \
        "$LOCAL_WHEEL" \
        "$SERVER_REMOTE:HttpArena/frameworks/blackbull/"
fi

# Generate the Dockerfile on the remote instance.

_write_blackbull_dockerfile() {
ssh "${SSH_OPTS[@]}" "$SERVER_REMOTE" "cat > HttpArena/frameworks/blackbull/Dockerfile" <<EOF
FROM python:3.13-slim
WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \\
    PYTHONUNBUFFERED=1 \\
    PIP_NO_CACHE_DIR=1 \\
    PIP_DISABLE_PIP_VERSION_CHECK=1

${COPY_WHEEL}
RUN ${INSTALL_CMD}

VOLUME /results
COPY app.py launcher.py /app/
${_LOGGING_INI_COPY}
EXPOSE 8080 8081 8443
CMD ["python", "launcher.py"]
EOF
}

_write_blackbull_dockerfile

# Flip meta.json enabled=true on the remote copy.
ssh "${SSH_OPTS[@]}" "$SERVER_REMOTE" \
    'sed -i "s/\"enabled\": false/\"enabled\": true/" HttpArena/frameworks/blackbull/meta.json'

# Place a build.sh alongside the Dockerfile so HttpArena's validate.sh
# calls it instead of `docker build --no-cache`.  Our build.sh omits
# --no-cache so Docker can reuse the pip layer from Step 5's pre-build.
ssh "${SSH_OPTS[@]}" "$SERVER_REMOTE" \
    'printf "#!/usr/bin/env bash\nset -euo pipefail\nSCRIPT_DIR=\"\$(cd \"\$(dirname \"\$0\")\" && pwd)\"\nFRAMEWORK=\"\$(basename \"\$SCRIPT_DIR\")\"\nIMAGE_NAME=\"httparena-\${FRAMEWORK}\"\ndocker build -t \"\$IMAGE_NAME\" \"\$SCRIPT_DIR\"\n" \
        > HttpArena/frameworks/blackbull/build.sh \
     && chmod +x HttpArena/frameworks/blackbull/build.sh'

echo "    staged."



# ---------------------------------------------------------------------------
# Step 5: build images
# ---------------------------------------------------------------------------

echo ">>> building images ..."
for fw in $FRAMEWORKS; do
    ssh "${SSH_OPTS[@]}" "$SERVER_REMOTE" "
        set -euo pipefail
        cd HttpArena
        IMAGE_NAME=httparena-${fw}
        docker build -t \$IMAGE_NAME frameworks/${fw}
    " || true
done

# ---------------------------------------------------------------------------
# Step 6 — run HttpArena benchmark
# ---------------------------------------------------------------------------

BENCH_RUNNER=(
    "$REPO_ROOT/bench/httparena/runner.sh"
    "$REPO_ROOT/bench/httparena/monitor.sh"
    "$REPO_ROOT/bench/httparena/postprocess.sh"
)

rsync -e "ssh ${SSH_OPTS[*]}" -az --chmod=F+x --delete \
    "${BENCH_RUNNER[@]}" \
    "$SERVER_REMOTE:~/src/"

# ---------------------------------------------------------------------------
# Optional: one-shot validate.sh before the benchmark sweep (catches a
# build break before the sweep burns minutes).  SKIP_VALIDATE=1 skips.
# ---------------------------------------------------------------------------
if [ "$SKIP_VALIDATE" != "1" ]; then
    echo ">>> HttpArena validate (one-shot, before sweep) ..."
    for fw in $FRAMEWORKS; do
        echo "  - validate $fw"
        ssh "${SSH_OPTS[@]}" "$SERVER_REMOTE" "
            cd HttpArena
            sudo -E ./scripts/validate.sh $fw 2>&1 | tee ~/results/validate-${fw}.log
        " || echo "    (validate non-zero for $fw — kept going; see log)"
    done
fi

# ---------------------------------------------------------------------------
# Step 6 — run HttpArena benchmark via runner.sh.
#
# Knob values reach runner.sh both as positional args (back-compat) and
# as env vars (runner.sh re-exports them for sudo -E into benchmark.sh).
# ---------------------------------------------------------------------------
echo ">>> HttpArena benchmark ..."

# echo "    [debug] LOADGEN_CPUS=${LOADGEN_CPUS:-unset} → GCANNON_CPUS=${_GCANNON_CPUS:-auto}" >&2
echo "    [debug] LOADGEN_CPUS=${LOADGEN_CPUS:-unset} → GCANNON_CPUS=${_GCANNON_CPUS:-auto}" >&2

for fw in $FRAMEWORKS; do
for prof in $PROFILES; do
    echo "  - $fw / $prof"

    ssh "${SSH_OPTS[@]}" "$SERVER_REMOTE" "
        export WEB_WORKERS='${WEB_WORKERS}' WEB_NOFILE='${WEB_NOFILE}'
        export LOADGEN_CPUS='${LOADGEN_CPUS}' LOADGEN_NOFILE='${LOADGEN_NOFILE}'
        export BB_HTTPARENA_PORTS='${BB_HTTPARENA_PORTS:-}'
        bash \$HOME/src/runner.sh \"$fw\" \"$prof\" \"$WEB_WORKERS\" \"$WEB_NOFILE\" \"$LOADGEN_CPUS\" \"$LOADGEN_NOFILE\"
        "
done
done


# ---------------------------------------------------------------------------
# Step 9: collect ALL results (no filtering, no interpretation)
# ---------------------------------------------------------------------------

echo ">>> collecting results (raw dump) ..."

# 1. benchmark / wrk / env logs all together
rsync -e "ssh ${SSH_OPTS[*]}" -az \
    "$SERVER_REMOTE:~/results/" \
    "$LOCAL_DEST/results/" || true

# 2. full HttpArena tree (for reproducibility / debugging)
rsync -e "ssh ${SSH_OPTS[*]}" -az \
    "$SERVER_REMOTE:~/HttpArena/" \
    "$LOCAL_DEST/httparena-tree/" || true

# 3. optional: explicit snapshot of key artifacts (redundant but safe)
mkdir -p "$LOCAL_DEST/_snapshot"
cp -a "$LOCAL_DEST/results" "$LOCAL_DEST/_snapshot/" 2>/dev/null || true

# ---------------------------------------------------------------------------
# done
# ---------------------------------------------------------------------------

echo "=== complete ==="
echo "results: $LOCAL_DEST"