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
#
#   --- Web server tuning (applied to ALL framework containers) ---
#   WEB_WORKERS   number of worker processes per web-server container
#                 (default: number of vCPUs on the instance, i.e. nproc)
#                 Passed as WEB_WORKERS env var into each container and also
#                 read by bench/httparena/launcher.py to set worker count.
#   WEB_NOFILE    maximum open file descriptors for each web-server container
#                 (default: 65536)
#                 Logged at startup so the value is visible in benchmark logs.
#
#   --- wrk load-generator tuning ---
#   WRK_CPUS      CPU affinity for the wrk load generator.
#                 Pass a plain integer (e.g. "24") to use cores 0–N-1, or a
#                 taskset range string (e.g. "16-31") to pin to specific CPUs.
#                 Forwarded as GCANNON_CPUS to HttpArena's benchmark.sh, which
#                 passes it to `taskset -c "$GCANNON_CPUS" wrk …`.
#                 (default: HttpArena auto-detects — second half of vCPUs)
#   WRK_NOFILE    maximum open file descriptors for the wrk container
#                 (default: 65536)
#                 Logged at startup so the value is visible in wrk logs.
#
#   wrk stdout/stderr are saved to separate files under the result directory
#   (logs/wrk-<framework>-<profile>.{log,err}) in addition to the normal log.
#
#   --- BlackBull local-wheel mode ---
#   LOCAL_BB_WHEEL  set to 1 to benchmark an unpublished local build.
#                 Runs `python -m build --wheel` locally, uploads the resulting
#                 .whl to EC2, and generates a Dockerfile that COPY+installs the
#                 local wheel instead of pulling from PyPI.
#                 Requires the 'build' package: pip install build
#
#   --- BlackBull access log ---
#   BB_ACCESS_LOG   set to 1 to enable per-request access logging inside the
#                 BlackBull container.  launcher.py attaches a StreamHandler to
#                 blackbull.access that writes each request to stderr with an
#                 "[ACCESS]" prefix.  benchmark.sh's save_result() captures
#                 docker logs (stdout+stderr) to
#                 HttpArena/site/static/logs/<profile>/<conns>/blackbull.log
#                 before the container is removed.  After each benchmark.sh run
#                 httparena_compare.sh greps "[ACCESS]" lines from those files
#                 and saves them as results/bb-access-blackbull-<profile>.log,
#                 which is rsync'd back to the local result directory in Step 9.

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

: "${PROFILES:?must be set explicitly, space-separated (e.g. 'baseline baseline-h2 echo-ws json json-comp json-tls limited-conn pipelined static static-h2 upload')}"
FRAMEWORKS="${FRAMEWORKS:-blackbull fastapi}"
KEEP_INSTANCE="${KEEP_INSTANCE:-0}"
SKIP_VALIDATE="${SKIP_VALIDATE:-0}"

# --- Web server tuning defaults -------------------------------------------
# WEB_WORKERS: empty string means "let each framework decide at runtime"
# (i.e. the launcher uses nproc / sched_getaffinity inside the container).
# Set to an integer to pin all frameworks to the same worker count.
: "${WEB_WORKERS:=}"
# WEB_NOFILE: ulimit -n value applied to every web-server container.
: "${WEB_NOFILE:=65536}"

# --- wrk load-generator tuning defaults -----------------------------------
# WRK_CPUS: CPU affinity for the wrk native binary (taskset GCANNON_CPUS).
# Pass a plain integer ("24" → cores 0-23) or a range string ("16-31").
# Empty means HttpArena auto-detects (second half of available vCPUs).
: "${WRK_CPUS:=}"
# WRK_NOFILE: ulimit -n value applied to the wrk container.
: "${WRK_NOFILE:=65536}"

TS="$(date -u +%Y%m%d-%H%M%SZ)"
SPRINT_TAG="${SPRINT_TAG:-sprint29}"
LOCAL_DEST="$REPO_ROOT/bench/results/httparena/${SPRINT_TAG}-${TS}"
mkdir -p "$LOCAL_DEST"

# Self-document the run: capture the entire driver console — provisioning,
# image builds, the watchdog heartbeats, and the streamed validate/benchmark
# output — into the result directory itself, so the orchestration + remote
# health trail lives alongside the artefacts instead of in an ad-hoc external
# tee.  (The caller may still pipe to its own log; this is independent.)
exec > >(tee -a "$LOCAL_DEST/driver.log") 2>&1

echo "=== bench/aws/httparena_compare.sh ==="
echo "  destination:   $LOCAL_DEST"
echo "  instance type: $INSTANCE_TYPE"
echo "  profiles:      $PROFILES"
echo "  frameworks:    $FRAMEWORKS"
echo "  --- web server ---"
echo "  WEB_WORKERS:   ${WEB_WORKERS:-<framework default (nproc)>}"
echo "  WEB_NOFILE:    $WEB_NOFILE"
echo "  --- wrk ---"
echo "  WRK_CPUS:      ${WRK_CPUS:-<no limit>}"
echo "  WRK_NOFILE:    $WRK_NOFILE"
echo "  --- blackbull ---"
echo "  LOCAL_BB_WHEEL: ${LOCAL_BB_WHEEL:-0}"
echo "  BB_ACCESS_LOG:  ${BB_ACCESS_LOG:-0}"
echo

# ---------------------------------------------------------------------------
# Step 0 — resolve the BlackBull version / wheel to install on the EC2.
#
# Normal path (LOCAL_BB_WHEEL unset / 0):
#   Install blackbull[compression]==<version> from PyPI inside the image.
#
# Local-wheel path (LOCAL_BB_WHEEL=1):
#   Build a wheel from the local source tree with `python -m build --wheel`,
#   upload it to EC2 alongside the framework files, and generate a
#   Dockerfile.dev-style Dockerfile that COPY+pip-installs the wheel
#   instead of pulling from PyPI.  Use this to benchmark unpublished changes.
# ---------------------------------------------------------------------------
LOCAL_BB_WHEEL="${LOCAL_BB_WHEEL:-0}"
# BB_ACCESS_LOG: empty / "0" means access logging disabled (default).
# Set to "1" to enable per-request logging inside the BlackBull container.
BB_ACCESS_LOG="${BB_ACCESS_LOG:-0}"
# BB_PHASE_TRACE: set to enable phase-trace logging (default: empty / disabled).
: "${BB_PHASE_TRACE:=}"
BLACKBULL_VERSION="${BLACKBULL_VERSION:-$(grep -E '^version' "$REPO_ROOT/pyproject.toml" | sed -E 's/.*"([^"]+)".*/\1/')}"

if [ "$LOCAL_BB_WHEEL" = "1" ]; then
    # BB_WHEEL_PATH: use a pre-built wheel instead of building from source.
    # Set this to compare a wheel built from a different commit without
    # touching the working tree.  The wheel must be a valid blackbull-*.whl.
    if [ -n "${BB_WHEEL_PATH:-}" ] && [ -f "$BB_WHEEL_PATH" ]; then
        echo ">>> LOCAL_BB_WHEEL=1 — using pre-built wheel: $BB_WHEEL_PATH"
        LOCAL_WHEEL="$BB_WHEEL_PATH"
        LOCAL_WHEEL_NAME="$(basename "$LOCAL_WHEEL")"
    else
        echo ">>> LOCAL_BB_WHEEL=1 — building wheel from local source ..."
        # Require the 'build' frontend; fail early with a helpful message.
        if ! python3 -c 'import build' 2>/dev/null; then
            echo "ERROR: 'build' package not found; install it with:" >&2
            echo "  pip install build" >&2
            exit 1
        fi
        # Build the wheel into dist/ (--wheel skips the sdist).
        (
            cd "$REPO_ROOT"
            python3 -m build --wheel --outdir dist/ >/dev/null
        )
        # Resolve the exact wheel filename just built.
        LOCAL_WHEEL="$(ls -t "$REPO_ROOT/dist/blackbull-"*.whl 2>/dev/null | head -1)"
        if [ -z "$LOCAL_WHEEL" ]; then
            echo "ERROR: no blackbull-*.whl found under $REPO_ROOT/dist/ after build" >&2
            exit 1
        fi
        LOCAL_WHEEL_NAME="$(basename "$LOCAL_WHEEL")"
        echo "    wheel: $LOCAL_WHEEL_NAME"
    fi
    echo ">>> BlackBull version: $BLACKBULL_VERSION (from LOCAL wheel)"
else
    echo ">>> BlackBull version: $BLACKBULL_VERSION (from PyPI)"
fi

# ---------------------------------------------------------------------------
# Step 1 — provision EC2 (and arm a teardown trap so we don't leak the
# instance on error or Ctrl-C).
# SKIP_PROVISION=1: re-use an already-running instance (requires valid
# .state file from a previous run with KEEP_INSTANCE=1).
# ---------------------------------------------------------------------------
SKIP_PROVISION="${SKIP_PROVISION:-0}"
if [ "$SKIP_PROVISION" != "1" ]; then
    echo ">>> bench/aws/up.sh ..."
    bash "$(dirname "$0")/up.sh"
fi

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
#
# Each sub-step echoes progress so the orchestrator can tell whether
# the SSH pipe is alive or hung.  The apt-get + source-build phases
# can saturate the CPU and make SSH unresponsive for minutes — the
# local echo markers bracketing each phase are the heartbeat.
# ---------------------------------------------------------------------------
echo ">>> installing Docker + HttpArena load tooling on the instance ..."
echo "    [1/4] apt-get update + install packages ..."
ssh "${SSH_OPTS[@]}" "$SERVER_REMOTE" '
    set -euo pipefail
    sudo apt-get update -qq
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
        docker.io git jq curl ca-certificates \
        build-essential pkg-config \
        wrk nghttp2-client >/dev/null
    sudo systemctl enable --now docker >/dev/null
    sudo usermod -aG docker ubuntu
    echo "    apt-get done."
'
echo "    [1/4] packages installed."

echo "    [2/4] liburing 2.9 (source build) ..."
ssh "${SSH_OPTS[@]}" "$SERVER_REMOTE" '
    set -euo pipefail
    if pkg-config --atleast-version=2.9 liburing 2>/dev/null; then
        echo "    liburing already >= 2.9, skipping."
        exit 0
    fi
    cd /tmp
    rm -rf liburing
    git clone --quiet --depth 1 --branch liburing-2.9 \
        https://github.com/axboe/liburing.git
    cd liburing
    ./configure --prefix=/usr >/dev/null
    make -s -j"$(nproc)" -C src
    sudo make -s install -C src >/dev/null
    sudo ldconfig
    echo "    liburing 2.9 built."
'
echo "    [2/4] liburing done."

echo "    [3/4] gcannon (source build, io_uring loadgen) ..."
ssh "${SSH_OPTS[@]}" "$SERVER_REMOTE" '
    set -euo pipefail
    if command -v gcannon >/dev/null; then
        echo "    gcannon already installed, skipping."
        exit 0
    fi
    cd /tmp
    rm -rf gcannon
    git clone --quiet --depth 1 https://github.com/MDA2AV/gcannon.git
    cd gcannon
    make -s
    sudo cp gcannon /usr/local/bin/
    echo "    gcannon built."
'
echo "    [3/4] gcannon done."

echo "    [4/4] verify load tools ..."
ssh "${SSH_OPTS[@]}" "$SERVER_REMOTE" '
    set -euo pipefail
    command -v gcannon >/dev/null || { echo "FATAL: gcannon not on PATH" >&2; exit 1; }
    command -v wrk     >/dev/null || { echo "FATAL: wrk not on PATH" >&2; exit 1; }
    command -v h2load  >/dev/null || { echo "FATAL: h2load not on PATH" >&2; exit 1; }
    echo "    all load tools verified."
'
echo "    [4/4] toolchain ready."

# ---------------------------------------------------------------------------
# Step 3 — clone HttpArena fresh on the instance.
# ---------------------------------------------------------------------------
echo ">>> cloning MDA2AV/HttpArena on the instance ..."
ssh "${SSH_OPTS[@]}" "$SERVER_REMOTE" '
    set -euo pipefail
    cd ~
    sudo rm -rf HttpArena
    git clone --depth 1 https://github.com/MDA2AV/HttpArena.git
'

# Patch HttpArena cpusets for small instances.  Upstream redis.sh / benchmark.sh
# pin the Redis sidecar + gcannon load tool to CPUs 0,64 / 32-63,96-127, which
# do not exist on an 8-vCPU c7i.2xlarge and make the crud profile fail.
# patch_cpuset.sh rewrites them to valid CPUs (0,2 and 7).  Idempotent.
echo ">>> applying cpuset patch for small instances ..."
scp "${SSH_OPTS[@]}" "$REPO_ROOT/bench/httparena/patch_cpuset.sh" \
    "$SERVER_REMOTE:~/patch_cpuset.sh"
ssh "${SSH_OPTS[@]}" "$SERVER_REMOTE" 'bash ~/patch_cpuset.sh'

# ---------------------------------------------------------------------------
# Step 4 — vendor bench/httparena/ as the `blackbull` framework.  Rewrite
# the Dockerfile to install from PyPI.  Flip meta.json enabled=true so
# HttpArena's harness picks it up.
# ---------------------------------------------------------------------------
echo ">>> staging blackbull framework dir on the instance ..."
ssh "${SSH_OPTS[@]}" "$SERVER_REMOTE" 'mkdir -p HttpArena/frameworks/blackbull'

# Upload framework files.  In LOCAL_BB_WHEEL=1 mode the wheel is also
# uploaded so the Dockerfile can COPY+install it from the build context.
# When BB_ACCESS_LOG=1, logging_access.ini is also uploaded: app.py loads it
# via logging.config.fileConfig() to configure the blackbull.access logger
# declaratively (standard Python logging config, not inline handler setup).
_BB_RSYNC_FILES=(
    "$REPO_ROOT/bench/httparena/app.py"
    "$REPO_ROOT/bench/httparena/launcher.py"
    "$REPO_ROOT/bench/httparena/meta.json"
    "$REPO_ROOT/bench/httparena/db.py"
    "$REPO_ROOT/bench/httparena/grpc_bench.py"
)
if [ "${BB_ACCESS_LOG:-0}" != "0" ]; then
    _BB_RSYNC_FILES+=("$REPO_ROOT/bench/httparena/logging_access.ini")
fi
rsync -e "ssh ${SSH_OPTS[*]}" -az --delete \
    "${_BB_RSYNC_FILES[@]}" \
    "$SERVER_REMOTE:HttpArena/frameworks/blackbull/"

# The upstream HttpArena repo vendors a stale frameworks/blackbull/.dockerignore
# that whitelists only requirements.txt/app.py/launcher.py and excludes
# everything else (`**`), so db.py / grpc_bench.py never enter the Docker build
# context and `COPY ... db.py` fails the build.  We don't rsync .dockerignore
# (so --delete won't remove it), and our rsync already controls exactly which
# files land in the dir — so just delete the stale ignore file.  Print the
# resulting build context so the driver log records what the build will see.
ssh "${SSH_OPTS[@]}" "$SERVER_REMOTE" \
    'rm -f HttpArena/frameworks/blackbull/.dockerignore
     echo "    build context:"; ls -1 HttpArena/frameworks/blackbull/'

if [ "$LOCAL_BB_WHEEL" = "1" ]; then
    echo "    uploading wheel $LOCAL_WHEEL_NAME ..."
    rsync -e "ssh ${SSH_OPTS[*]}" -az \
        "$LOCAL_WHEEL" \
        "$SERVER_REMOTE:HttpArena/frameworks/blackbull/"
fi

# Build the COPY line for logging_access.ini if BB_ACCESS_LOG=1.
# When present, app.py loads it via logging.config.fileConfig() at startup.
_LOGGING_INI_COPY=''
if [ "${BB_ACCESS_LOG:-0}" != "0" ]; then
    _LOGGING_INI_COPY='COPY logging_access.ini /app/'
fi

# Generate the Dockerfile on the remote instance.
if [ "$LOCAL_BB_WHEEL" = "1" ]; then
    # Dockerfile.dev style: COPY the local wheel, install from it.
    ssh "${SSH_OPTS[@]}" "$SERVER_REMOTE" "cat > HttpArena/frameworks/blackbull/Dockerfile" <<EOF
# Auto-generated by bench/aws/httparena_compare.sh (LOCAL_BB_WHEEL=1).
# Installs BlackBull from a locally-built wheel instead of PyPI.
FROM python:3.13-slim
WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \\
    PYTHONUNBUFFERED=1 \\
    PIP_NO_CACHE_DIR=1 \\
    PIP_DISABLE_PIP_VERSION_CHECK=1

COPY ${LOCAL_WHEEL_NAME} /tmp/
# asyncpg + redis back the async-db / crud profiles (Postgres + Redis sidecars).
RUN cd /tmp && pip install --no-cache-dir "/tmp/${LOCAL_WHEEL_NAME}[compression]" asyncpg redis
VOLUME /results

COPY app.py launcher.py db.py grpc_bench.py /app/
${_LOGGING_INI_COPY}
EXPOSE 8080 8081 8443
CMD ["python", "launcher.py"]
EOF
else
    ssh "${SSH_OPTS[@]}" "$SERVER_REMOTE" "cat > HttpArena/frameworks/blackbull/Dockerfile" <<EOF
# Auto-generated by bench/aws/httparena_compare.sh.
# Installs BlackBull from PyPI (no source tree on the instance).
FROM python:3.13-slim
WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \\
    PYTHONUNBUFFERED=1 \\
    PIP_NO_CACHE_DIR=1 \\
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN pip install --no-cache-dir 'blackbull[compression]==${BLACKBULL_VERSION}' asyncpg redis
VOLUME /results

COPY app.py launcher.py db.py grpc_bench.py /app/
${_LOGGING_INI_COPY}
EXPOSE 8080 8081 8443
CMD ["python", "launcher.py"]
EOF
fi

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
# Step 5 — pre-build framework images BEFORE installing the docker shim.
#
# Rationale:
#   validate.sh has a 300-second overall watchdog timer.  On a cold
#   instance, `pip install blackbull[compression]` (brotli + zstandard
#   compilation) takes 2-4 minutes, which leaves no headroom.  By
#   building the image here — before the shim replaces /usr/bin/docker —
#   we use the real docker binary directly (no shim complexity), and the
#   pip layer is cached.  validate.sh's subsequent `docker build` then
#   reuses that layer and completes in under 5 seconds.
#
# IMPORTANT: this step must run BEFORE Step 6 (shim install) so that
#   the docker calls here go straight to the real binary.
# ---------------------------------------------------------------------------
echo ">>> pre-building framework images on the instance ..."
for fw in $FRAMEWORKS; do
    echo "  - $fw"
    ssh "${SSH_OPTS[@]}" "$SERVER_REMOTE" "
        set -euo pipefail
        cd HttpArena
        IMAGE_NAME=\"httparena-${fw}\"
        if [ -x frameworks/${fw}/build.sh ]; then
            sudo frameworks/${fw}/build.sh
        else
            sudo docker build -t \"\$IMAGE_NAME\" frameworks/${fw}
        fi
        echo \"    image \$IMAGE_NAME ready.\"
    " || echo "    (pre-build non-zero for $fw — kept going)"
done

# ---------------------------------------------------------------------------
# Step 6 — install the docker-bench shim AFTER pre-building images.
#
# The shim wraps every subsequent `docker run` call to inject tuning
# (ulimit, WEB_WORKERS, WRK_CPUS, etc.).  The shim script is uploaded
# as a plain file and executed on the instance — no SSH heredoc.
# ---------------------------------------------------------------------------
echo ">>> installing docker-bench shim on the instance ..."

# Upload the shim installer script.
scp "${SSH_OPTS[@]}" \
    "$REPO_ROOT/bench/httparena/install_docker_shim.sh" \
    "$SERVER_REMOTE:~/install_docker_shim.sh"

# Execute it with tuning values as arguments.
ssh "${SSH_OPTS[@]}" "$SERVER_REMOTE" \
    "bash ~/install_docker_shim.sh '${WEB_WORKERS}' '${WEB_NOFILE}' '${WRK_CPUS}' '${WRK_NOFILE}' '${BB_ACCESS_LOG}' '${BB_PHASE_TRACE}'"
echo "    shim installed."

# ---------------------------------------------------------------------------
# Step 7 — run HttpArena's official validate + benchmark scripts via
# an uploaded plain script (no SSH heredoc).  Output captured under
# ~/results/ on the instance and rsync'd back at the end.
# ---------------------------------------------------------------------------
echo ">>> uploading run_httparena.sh ..."
scp "${SSH_OPTS[@]}" \
    "$REPO_ROOT/bench/httparena/run_httparena.sh" \
    "$SERVER_REMOTE:~/run_httparena.sh"

# Convert space-separated lists to comma-separated for the script arg.
_FW_CSV=$(echo "$FRAMEWORKS" | tr ' ' ',')
_PROF_CSV=$(echo "$PROFILES" | tr ' ' ',')

echo ">>> HttpArena validate + benchmark ..."

# --- remote-state watchdog ---------------------------------------------------
# The validate+benchmark SSH below blocks for ~50 min.  A wedged docker daemon
# (e.g. an instance reused after a killed run) emits NOTHING for the full 600s
# validate gate, so a frozen log line is indistinguishable from slow progress.
# This sidecar probes the server independently every WATCHDOG_INTERVAL seconds
# and prints a one-line heartbeat (docker responsiveness, live containers,
# :8080 listen state, loadavg) into the same streamed output.  It is strictly
# observational — it never touches the benchmark commands, profiles, or
# connection counts.  Every probe is `timeout`-wrapped so it can never itself
# hang the run, and it is killed the moment the run SSH returns.  The probe is
# 4 cheap syscalls every 30 s on a 32-vCPU box — far below the measurement
# noise band.  Set WATCHDOG_INTERVAL=0 to disable.
_REMOTE_PROBE='set +e; if names=$(timeout 5 sudo docker ps --format "{{.Names}}" 2>/dev/null); then c=$(printf "%s" "$names" | paste -sd, -); [ -n "$c" ] || c="(none)"; else c=HUNG; fi; p=$(ss -ltn 2>/dev/null | grep -qE ":8080|:8443" && echo up || echo down); printf "docker=%s port8080=%s load=%s\n" "$c" "$p" "$(cut -d" " -f1 /proc/loadavg)"'
_watchdog() {
    local interval="${WATCHDOG_INTERVAL:-30}" strikes=0 probe
    [ "$interval" -gt 0 ] 2>/dev/null || return 0
    while true; do
        sleep "$interval"
        if probe=$(timeout 12 ssh "${SSH_OPTS[@]}" "$SERVER_REMOTE" "$_REMOTE_PROBE" 2>/dev/null) \
           && [ -n "$probe" ] && [[ "$probe" != *docker=HUNG* ]]; then
            strikes=0
            echo "  [watchdog $(date +%H:%M:%S)] $probe"
        else
            strikes=$((strikes + 1))
            echo "  [watchdog $(date +%H:%M:%S)] ⚠ remote unresponsive (strike ${strikes}) — ${probe:-probe ssh timed out}"
            if [ "$strikes" -ge "${WATCHDOG_MAX_STRIKES:-4}" ]; then
                echo "  [watchdog] ✗ docker/instance wedged for ${strikes} consecutive probes — abort manually (Ctrl-C) and re-provision; do not trust this run."
                strikes=0
            fi
        fi
    done
}
_watchdog & _WATCHDOG_PID=$!

ssh "${SSH_OPTS[@]}" "$SERVER_REMOTE" \
    "bash ~/run_httparena.sh '${_FW_CSV}' '${_PROF_CSV}' '${SKIP_VALIDATE}' '${WRK_CPUS}'" \
    || echo "  (run_httparena.sh exited non-zero — kept going)"

kill "$_WATCHDOG_PID" 2>/dev/null || true
wait "$_WATCHDOG_PID" 2>/dev/null || true

# ---------------------------------------------------------------------------
# Step 8 — remove the shim and restore the real docker binary.
# ---------------------------------------------------------------------------
echo ">>> restoring real docker binary on the instance ..."
ssh "${SSH_OPTS[@]}" "$SERVER_REMOTE" '
    set -euo pipefail
    DOCKER_ON_PATH="$(command -v docker)"
    DOCKER_REAL="${DOCKER_ON_PATH}.real"
    if [ -f "$DOCKER_REAL" ]; then
        sudo install -o root -g root -m 0755 "$DOCKER_REAL" "$DOCKER_ON_PATH"
        sudo rm -f "$DOCKER_REAL"
        echo "    docker binary restored from $DOCKER_REAL."
    else
        echo "    $DOCKER_REAL not found — docker binary left as-is."
    fi
' || echo "  (could not restore docker binary — instance may have issues)"

# ---------------------------------------------------------------------------
# Step 9 — pull all logs + any HttpArena-generated result artefacts back.
# ---------------------------------------------------------------------------
echo ">>> pulling artefacts back to $LOCAL_DEST ..."
rsync -e "ssh ${SSH_OPTS[*]}" -az \
    "$SERVER_REMOTE:results/" "$LOCAL_DEST/logs/"

# HttpArena may emit per-run JSON / TSV under a known dir; grab the lot
# regardless of where it landed (best-effort).
rsync -e "ssh ${SSH_OPTS[*]}" -az --include='*/' --include='*.json' \
    --include='*.tsv' --include='*.csv' --include='*.md' --include='*.log' --exclude='*' \
    "$SERVER_REMOTE:HttpArena/" "$LOCAL_DEST/httparena-tree/" || true

# Record provenance.
cat > "$LOCAL_DEST/provenance.md" <<EOF
# HttpArena EC2 cross-check

- Timestamp:  $TS
- Sprint tag: $SPRINT_TAG
- Instance:   $INSTANCE_TYPE in $REGION
- Public IP:  $SERVER_PUBLIC_IP
- BlackBull:  blackbull==$BLACKBULL_VERSION ($([ "$LOCAL_BB_WHEEL" = "1" ] && echo "local wheel: $LOCAL_WHEEL_NAME" || echo "from PyPI"); repo commit $(cd "$REPO_ROOT" && git rev-parse --short HEAD))
- Profiles:   $PROFILES
- Frameworks: $FRAMEWORKS

## Tuning

| Knob | Value |
|------|-------|
| WEB_WORKERS | ${WEB_WORKERS:-<framework default (nproc)>} |
| WEB_NOFILE  | $WEB_NOFILE |
| WRK_CPUS    | ${WRK_CPUS:-<no limit>} |
| WRK_NOFILE  | $WRK_NOFILE |
EOF

echo
echo "=== complete ==="
echo "Artefacts at: $LOCAL_DEST"
echo "  Validate logs:  $LOCAL_DEST/logs/validate-*.log"
echo "  Benchmark logs: $LOCAL_DEST/logs/benchmark-*-*.log"
echo "  wrk logs:       $LOCAL_DEST/logs/wrk-*-*.log"
[ "${BB_ACCESS_LOG:-0}" != "0" ] && \
    echo "  Access logs:    $LOCAL_DEST/logs/bb-access-*-*.log"
echo "  Provenance:     $LOCAL_DEST/provenance.md"
echo
echo "Instance will be torn down by the EXIT trap."
