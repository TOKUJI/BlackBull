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
#              (default: "blackbull fastapi";
#               supported: blackbull, blackbull-uvloop, blackbull-asgiscope,
#               fastapi, sanic, aiohttp)
#   BLACKBULL_VERSION  PyPI version pin (default: pyproject.toml's version)
#   SPRINT_TAG  prefix on the result directory (default: sprint29)
#   SKIP_VALIDATE   set to 1 to skip the full 49-point correctness check and
#                   run the minimal ready_check.sh instead (correct when the
#                   wheel was already validated locally with
#                   bench/httparena/validate_local.sh — same wheel, same commit).
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
# Supported frameworks: blackbull, blackbull-uvloop, blackbull-asgiscope, fastapi, sanic, aiohttp
KEEP_INSTANCE="${KEEP_INSTANCE:-0}"
SKIP_VALIDATE="${SKIP_VALIDATE:-0}"
# STAGE_PEERS=0 leaves upstream HttpArena's own `sanic` / `aiohttp` entries in
# place instead of overwriting them with this repo's.  Default 1 preserves the
# existing behaviour.
#
# Why this knob exists.  The staged entries are *our* applications written
# against someone else's framework — `bench/httparena/sanic/app.py` says so in
# its first line ("BlackBull-equivalent") — and their meta.json declares more
# profiles than upstream does (sanic: 14 vs 6).  That is fine for "compare the
# same endpoints", and it is **not** evidence about Sanic or aiohttp as
# projects.  A peer-positioning run wants STAGE_PEERS=0; an endpoint-parity run
# wants the default.  Whichever is used has to be stated in the writeup, which
# is why this is a named knob and not a code edit.
STAGE_PEERS="${STAGE_PEERS:-1}"
# BB_UVLOOP: baked into the `blackbull` image ENV. Default 0 (pure-Python event
# loop) so the identity measurement is the default.
#
# To measure what uvloop is worth, do NOT flip this and compare against an
# earlier run — put `blackbull blackbull-uvloop` in $FRAMEWORKS instead.  The
# `blackbull-uvloop` variant is the same wheel and the same app with
# BB_UVLOOP=1, so both loops are measured on one instance in one session
# against the same peers, and the delta is not confounded by the machine, the
# generator, or the day.
BB_UVLOOP="${BB_UVLOOP:-0}"
# PYTHON_IMAGE: Docker base image for the BlackBull container.
# Default python:3.13-slim.  Set to python:3.15-rc-slim to compare
# Python versions on the same instance.
: "${PYTHON_IMAGE:=python:3.13-slim}"

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
echo "  BB_UVLOOP:      $BB_UVLOOP"
echo "  PYTHON_IMAGE:   $PYTHON_IMAGE"
echo "  BB_ACCESS_LOG:  ${BB_ACCESS_LOG:-0}"
echo

# ---------------------------------------------------------------------------
# Architecture guard: Intel instances ship with SMT enabled, and Linux x86
# enumerates sibling threads interleaved (vCPU N and vCPU N+C share physical
# core C).  HttpArena's half-split cpuset strategy (server 0..V/2-1, load-gen
# V/2..V-1) therefore places one sibling from each core in each cpuset —
# server and load-gen contend on every physical core instead of being isolated.
# Disable SMT at launch (ThreadsPerCore=1) or use AMD (m7a) / Graviton (m7g).
if [[ "$INSTANCE_TYPE" =~ [0-9]i ]]; then
    echo "=== WARNING: Intel instance ($INSTANCE_TYPE) — SMT sibling interleaving ==="
    echo "  The cpuset half-split (0..V/2-1 vs V/2..V-1) places server and"
    echo "  load-gen on sibling threads of the SAME physical cores — not isolated."
    echo "  Benchmark throughput and CPU% will be distorted by SMT contention."
    echo "  Recommended: use AMD m7a/c7a or Graviton m7g/c7g, or set"
    echo "  ThreadsPerCore=1 to disable SMT on Intel instances."
    echo
fi

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

# Safety net: schedule a forced poweroff on the EC2 instance so that an
# orphaned instance (terminal loss, local shutdown, network partition) is
# terminated by AWS within this window.  `shutdown -h` triggers a systemd
# poweroff; the instance's --instance-initiated-shutdown-behavior=terminate
# (set by up.sh) ensures the poweroff results in termination, not just stop.
# 180 minutes covers the worst-case BB+FastAPI run (~90 min) with a 2× margin.
SAFETY_SHUTDOWN_MINUTES="${SAFETY_SHUTDOWN_MINUTES:-180}"
if [ "$STAGE_PEERS" = "1" ]; then
    echo ">>> STAGE_PEERS=1 — sanic/aiohttp will run THIS REPO's implementations"
else
    echo ">>> STAGE_PEERS=0 — sanic/aiohttp will run UPSTREAM's implementations"
fi
echo ">>> setting EC2 safety shutdown timer: ${SAFETY_SHUTDOWN_MINUTES} min ..."
ssh "${SSH_OPTS[@]}" "$SERVER_REMOTE" \
    "sudo shutdown -h +${SAFETY_SHUTDOWN_MINUTES} </dev/null >/dev/null 2>&1" || true

# ---------------------------------------------------------------------------
# Step 2 — install Docker + HttpArena load-generator tooling (gcannon,
# wrk, h2load, ghz).  An earlier pass skipped these
# and HttpArena's benchmark.sh reported 0 req/s for every run.
#
# liburing 2.9 + gcannon build from source (HttpArena's docs as of
# 2026-05-31 — no pre-built gcannon binary distribution).  wrk and
# h2load come from apt (nghttp2-client provides h2load on Ubuntu).  ghz
# (real gRPC load generator) is required by HttpArena's gRPC/stream
# profiles AND — since upstream added the `_wait_grpc` readiness probe —
# by *every* grpc profile's server-readiness check; a missing ghz makes
# `_wait_grpc` fail and framework.sh fall through to an unbound-var abort
# (`probe_url: unbound variable`), silently producing no gRPC numbers.
# Installed from the upstream prebuilt release binary (no Go toolchain).
# Kernel 6.1+ with io_uring is a gcannon precondition; the c7i.xlarge
# Ubuntu 24.04 AMI ships kernel 6.8+, so the precondition is met.
#
# Each sub-step echoes progress so the orchestrator can tell whether
# the SSH pipe is alive or hung.  The apt-get + source-build phases
# can saturate the CPU and make SSH unresponsive for minutes — the
# local echo markers bracketing each phase are the heartbeat.
# ---------------------------------------------------------------------------
echo ">>> installing Docker + HttpArena load tooling on the instance ..."
echo "    [1/5] apt-get update + install packages ..."
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
echo "    [1/5] packages installed."

echo "    [2/5] liburing 2.9 (source build) ..."
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
echo "    [2/5] liburing done."

echo "    [3/5] gcannon (source build, io_uring loadgen) ..."
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
echo "    [3/5] gcannon done."

echo "    [4/5] ghz (prebuilt gRPC load generator) ..."
ssh "${SSH_OPTS[@]}" "$SERVER_REMOTE" '
    set -euo pipefail
    if command -v ghz >/dev/null; then
        echo "    ghz already installed, skipping."
        exit 0
    fi
    # Upstream prebuilt release binary — no Go toolchain needed.  The
    # linux x86_64 tarball ships a static ghz binary + config + LICENSE.
    GHZ_VER=0.120.0
    cd /tmp
    rm -rf ghz-dist ghz.tar.gz
    curl -fsSL -o ghz.tar.gz \
        "https://github.com/bojand/ghz/releases/download/v${GHZ_VER}/ghz-linux-x86_64.tar.gz"
    mkdir -p ghz-dist
    tar -xzf ghz.tar.gz -C ghz-dist
    sudo install -m 0755 ghz-dist/ghz /usr/local/bin/ghz
    echo "    ghz ${GHZ_VER} installed."
'
echo "    [4/5] ghz done."

echo "    [5/5] verify load tools ..."
ssh "${SSH_OPTS[@]}" "$SERVER_REMOTE" '
    set -euo pipefail
    command -v gcannon >/dev/null || { echo "FATAL: gcannon not on PATH" >&2; exit 1; }
    command -v wrk     >/dev/null || { echo "FATAL: wrk not on PATH" >&2; exit 1; }
    command -v h2load  >/dev/null || { echo "FATAL: h2load not on PATH" >&2; exit 1; }
    command -v ghz     >/dev/null || { echo "FATAL: ghz not on PATH" >&2; exit 1; }
    echo "    all load tools verified."
'
echo "    [5/5] toolchain ready."

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
# DATABASE_MAX_CONN — how many Postgres connections the whole cluster may hold.
# Upstream hardcodes 256 in scripts/lib/framework.sh; each entry divides it by
# its own worker count.  On a small instance that division gives a pool far
# larger than the reference run's, which changes the regime under test: at
# 256/16 every worker has 15 slots and nothing waits, where the published
# numbers were taken at 3.  Set this to put the pool back where the comparison
# needs it; the divisor stays each entry's own.
# ---------------------------------------------------------------------------
if [ -n "${DATABASE_MAX_CONN:-}" ]; then
    echo ">>> setting DATABASE_MAX_CONN=${DATABASE_MAX_CONN} on the instance ..."
    ssh "${SSH_OPTS[@]}" "$SERVER_REMOTE" \
        "sed -i 's/DATABASE_MAX_CONN=256/DATABASE_MAX_CONN=${DATABASE_MAX_CONN}/g' \
             HttpArena/scripts/lib/framework.sh
         grep -n 'DATABASE_MAX_CONN=' HttpArena/scripts/lib/framework.sh | head -3"
fi

# ---------------------------------------------------------------------------
# Step 4 — vendor bench/httparena/ as one framework dir per BlackBull variant.
# Rewrite the Dockerfile to install from PyPI (or the local wheel).  Flip
# meta.json enabled=true so HttpArena's harness picks it up.
#
# Three variants may be requested in the same run:
#
#   blackbull           BB_UVLOOP=$BB_UVLOOP   (default 0 — the shipped default)
#   blackbull-uvloop    BB_UVLOOP=1
#   blackbull-asgiscope BB_FORCE_ASGI_SCOPE=1 (ASGI scope conversion on every
#                       request — the dual-path lane, and Sprint 99's baseline)
#
# Naming them in $FRAMEWORKS puts the variants on the *same instance in the
# same session*, alongside the same peers.  That is the only way to read a
# variant's delta as a property of BlackBull rather than of the box: the images
# differ in exactly one ENV line — same wheel, same app.py, same launcher.py —
# so nothing else can account for a gap between them.
#
# BB_UVLOOP / BB_FORCE_ASGI_SCOPE are set in their own ENV layers at the very
# end of the Dockerfile.  They are not needed at build time, and keeping them
# after the pip install lets the variants share every expensive layer: the
# second image builds in seconds.
# ---------------------------------------------------------------------------

# Upload framework files.  In LOCAL_BB_WHEEL=1 mode the wheel is also
# uploaded so the Dockerfile can COPY+install it from the build context.
# Access logging (BB_ACCESS_LOG=1) is configured via env, not a config file:
# the shim injects BB_LOG_FILE and app.py sets the access logger level, so the
# framework's async setup_async_logging (worker.py) builds the file sink.  The
# legacy logging_access.ini is no longer uploaded or loaded.
_BB_RSYNC_FILES=(
    "$REPO_ROOT/bench/httparena/app.py"
    "$REPO_ROOT/bench/httparena/launcher.py"
    "$REPO_ROOT/bench/httparena/meta.json"
    "$REPO_ROOT/bench/httparena/db.py"
    "$REPO_ROOT/bench/httparena/grpc_bench.py"
)

# Access logging is now configured via env vars (BB_LOG_FILE from the shim +
# BB_ACCESS_LOG), so no logging config file is COPYed into the image.
_LOGGING_INI_COPY=''

_stage_blackbull() {
    local fw="$1" uvloop="$2" scope="$3"
    local dir="HttpArena/frameworks/${fw}"

    echo ">>> staging ${fw} framework dir on the instance (BB_UVLOOP=${uvloop}"
    echo "        BB_FORCE_ASGI_SCOPE=${scope:-<unset>}) ..."
    ssh "${SSH_OPTS[@]}" "$SERVER_REMOTE" "mkdir -p ${dir}"

    rsync -e "ssh ${SSH_OPTS[*]}" -az --delete \
        "${_BB_RSYNC_FILES[@]}" \
        "$SERVER_REMOTE:${dir}/"

    # The upstream HttpArena repo vendors a stale
    # frameworks/blackbull/.dockerignore that whitelists only
    # requirements.txt/app.py/launcher.py and excludes everything else (`**`),
    # so db.py / grpc_bench.py never enter the Docker build context and
    # `COPY ... db.py` fails the build.  We don't rsync .dockerignore (so
    # --delete won't remove it), and our rsync already controls exactly which
    # files land in the dir — so just delete the stale ignore file.  Print the
    # resulting build context so the driver log records what the build sees.
    ssh "${SSH_OPTS[@]}" "$SERVER_REMOTE" \
        "rm -f ${dir}/.dockerignore
         echo '    build context:'; ls -1 ${dir}/"

    if [ "$LOCAL_BB_WHEEL" = "1" ]; then
        echo "    uploading wheel $LOCAL_WHEEL_NAME ..."
        rsync -e "ssh ${SSH_OPTS[*]}" -az \
            "$LOCAL_WHEEL" \
            "$SERVER_REMOTE:${dir}/"
    fi

    # Generate the Dockerfile on the remote instance.
    if [ "$LOCAL_BB_WHEEL" = "1" ]; then
        # Dockerfile.dev style: COPY the local wheel, install from it.
        ssh "${SSH_OPTS[@]}" "$SERVER_REMOTE" "cat > ${dir}/Dockerfile" <<EOF
# Auto-generated by bench/aws/httparena_compare.sh (LOCAL_BB_WHEEL=1).
# Installs BlackBull from a locally-built wheel instead of PyPI.
FROM ${PYTHON_IMAGE}
WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \\
    PYTHONUNBUFFERED=1 \\
    PIP_NO_CACHE_DIR=1 \\
    PIP_DISABLE_PIP_VERSION_CHECK=1

COPY ${LOCAL_WHEEL_NAME} /tmp/
# asyncpg + redis back the async-db / crud profiles (Postgres + Redis sidecars).
RUN cd /tmp && pip install --no-cache-dir "/tmp/${LOCAL_WHEEL_NAME}[compression,speed]" asyncpg redis
VOLUME /results

COPY app.py launcher.py db.py grpc_bench.py /app/
${_LOGGING_INI_COPY}
# Last layer, and the only one that differs between the blackbull variants —
# everything above is shared cache.
ENV BB_UVLOOP=${uvloop}
${scope:+ENV BB_FORCE_ASGI_SCOPE=${scope}}
EXPOSE 8080 8081 8443
CMD ["python", "launcher.py"]
EOF
    else
        ssh "${SSH_OPTS[@]}" "$SERVER_REMOTE" "cat > ${dir}/Dockerfile" <<EOF
# Auto-generated by bench/aws/httparena_compare.sh.
# Installs BlackBull from PyPI (no source tree on the instance).
FROM ${PYTHON_IMAGE}
WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \\
    PYTHONUNBUFFERED=1 \\
    PIP_NO_CACHE_DIR=1 \\
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN pip install --no-cache-dir 'blackbull[compression,speed]==${BLACKBULL_VERSION}' asyncpg redis
VOLUME /results

COPY app.py launcher.py db.py grpc_bench.py /app/
${_LOGGING_INI_COPY}
# Last layer, and the only one that differs between the blackbull variants —
# everything above is shared cache.
ENV BB_UVLOOP=${uvloop}
${scope:+ENV BB_FORCE_ASGI_SCOPE=${scope}}
EXPOSE 8080 8081 8443
CMD ["python", "launcher.py"]
EOF
    fi

    # Flip meta.json enabled=true, and name the variant after its own dir so
    # HttpArena's report does not show two rows both called "blackbull".
    ssh "${SSH_OPTS[@]}" "$SERVER_REMOTE" \
        "sed -i 's/\"enabled\": false/\"enabled\": true/;
                 s/\"display_name\": \"blackbull\"/\"display_name\": \"${fw}\"/' \
             ${dir}/meta.json"

    # Place a build.sh alongside the Dockerfile so HttpArena's validate.sh
    # calls it instead of `docker build --no-cache`.  Our build.sh omits
    # --no-cache so Docker can reuse the pip layer from Step 5's pre-build.
    ssh "${SSH_OPTS[@]}" "$SERVER_REMOTE" \
        "printf '#!/usr/bin/env bash\nset -euo pipefail\nSCRIPT_DIR=\"\$(cd \"\$(dirname \"\$0\")\" && pwd)\"\nFRAMEWORK=\"\$(basename \"\$SCRIPT_DIR\")\"\nIMAGE_NAME=\"httparena-\${FRAMEWORK}\"\ndocker build -t \"\$IMAGE_NAME\" \"\$SCRIPT_DIR\"\n' \
            > ${dir}/build.sh \
         && chmod +x ${dir}/build.sh"

    echo "    ${fw} staged."
}

for fw in $FRAMEWORKS; do
    case "$fw" in
        blackbull)          _stage_blackbull blackbull "$BB_UVLOOP" "" ;;
        blackbull-uvloop)   _stage_blackbull blackbull-uvloop 1 "" ;;
        blackbull-asgiscope) _stage_blackbull blackbull-asgiscope "$BB_UVLOOP" 1 ;;
    esac
done

# ---------------------------------------------------------------------------
# Step 4b — stage bench/httparena/sanic/ as the `sanic` framework.
# Same pattern as BlackBull: upload app.py, launcher.py, meta.json,
# requirements.txt, Dockerfile, and build.sh.
# Only stages if "sanic" is in $FRAMEWORKS.
# ---------------------------------------------------------------------------
if [[ " $FRAMEWORKS " == *" sanic "* && "$STAGE_PEERS" = "1" ]]; then
    echo ">>> staging sanic framework dir on the instance (STAGE_PEERS=1 — this repo's implementation, not upstream's) ..."
    ssh "${SSH_OPTS[@]}" "$SERVER_REMOTE" 'mkdir -p HttpArena/frameworks/sanic'

    _SANIC_RSYNC_FILES=(
        "$REPO_ROOT/bench/httparena/sanic/app.py"
        "$REPO_ROOT/bench/httparena/sanic/launcher.py"
        "$REPO_ROOT/bench/httparena/sanic/meta.json"
        "$REPO_ROOT/bench/httparena/sanic/requirements.txt"
        "$REPO_ROOT/bench/httparena/sanic/Dockerfile"
        "$REPO_ROOT/bench/httparena/sanic/build.sh"
    )
    rsync -e "ssh ${SSH_OPTS[*]}" -az --delete \
        "${_SANIC_RSYNC_FILES[@]}" \
        "$SERVER_REMOTE:HttpArena/frameworks/sanic/"

    ssh "${SSH_OPTS[@]}" "$SERVER_REMOTE" \
        'chmod +x HttpArena/frameworks/sanic/build.sh
         echo "    build context:"; ls -1 HttpArena/frameworks/sanic/'

    # Flip meta.json enabled=true on the remote copy.
    ssh "${SSH_OPTS[@]}" "$SERVER_REMOTE" \
        'sed -i "s/\"enabled\": false/\"enabled\": true/" HttpArena/frameworks/sanic/meta.json'

    echo "    sanic staged."
fi

# ---------------------------------------------------------------------------
# Step 4c — stage bench/httparena/aiohttp/ as the `aiohttp` framework.
# Same pattern as sanic: upload app.py, launcher.py, meta.json,
# requirements.txt, Dockerfile, and build.sh.
# Only stages if "aiohttp" is in $FRAMEWORKS.
# ---------------------------------------------------------------------------
if [[ " $FRAMEWORKS " == *" aiohttp "* && "$STAGE_PEERS" = "1" ]]; then
    echo ">>> staging aiohttp framework dir on the instance (STAGE_PEERS=1 — this repo's implementation, not upstream's) ..."
    ssh "${SSH_OPTS[@]}" "$SERVER_REMOTE" 'mkdir -p HttpArena/frameworks/aiohttp'

    _AIOHTTP_RSYNC_FILES=(
        "$REPO_ROOT/bench/httparena/aiohttp/app.py"
        "$REPO_ROOT/bench/httparena/aiohttp/launcher.py"
        "$REPO_ROOT/bench/httparena/aiohttp/meta.json"
        "$REPO_ROOT/bench/httparena/aiohttp/requirements.txt"
        "$REPO_ROOT/bench/httparena/aiohttp/Dockerfile"
        "$REPO_ROOT/bench/httparena/aiohttp/build.sh"
    )
    rsync -e "ssh ${SSH_OPTS[*]}" -az --delete \
        "${_AIOHTTP_RSYNC_FILES[@]}" \
        "$SERVER_REMOTE:HttpArena/frameworks/aiohttp/"

    ssh "${SSH_OPTS[@]}" "$SERVER_REMOTE" \
        'chmod +x HttpArena/frameworks/aiohttp/build.sh
         echo "    build context:"; ls -1 HttpArena/frameworks/aiohttp/'

    # Flip meta.json enabled=true on the remote copy.
    ssh "${SSH_OPTS[@]}" "$SERVER_REMOTE" \
        'sed -i "s/\"enabled\": false/\"enabled\": true/" HttpArena/frameworks/aiohttp/meta.json'

    echo "    aiohttp staged."
fi

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
# Step 5b — prove which event loop each BlackBull image will actually run.
#
# The dangerous failure is silent: with BB_UVLOOP=1 but no uvloop in the image,
# ``apply_event_loop_policy`` logs a warning and keeps the stock loop, and the
# arm still produces a full set of plausible numbers — which then read as
# "uvloop bought nothing".  So ask the image itself, through BlackBull's own
# policy installer, and abort the run rather than measure a mislabelled arm.
# ---------------------------------------------------------------------------
_LOOP_PROBE='
import asyncio, os
from blackbull.env import apply_event_loop_policy, reset_settings_cache
reset_settings_cache()
apply_event_loop_policy()
loop = asyncio.new_event_loop()
print("BB_UVLOOP=" + str(os.environ.get("BB_UVLOOP"))
      + " loop=" + type(loop).__module__ + "." + type(loop).__name__)
loop.close()
'
for fw in $FRAMEWORKS; do
    case "$fw" in blackbull|blackbull-uvloop) ;; *) continue ;; esac
    _want=$([ "$fw" = "blackbull-uvloop" ] && echo 1 || echo "$BB_UVLOOP")
    echo ">>> verifying event loop for $fw (expect BB_UVLOOP=${_want}) ..."
    _probe=$(ssh "${SSH_OPTS[@]}" "$SERVER_REMOTE" \
        "sudo docker run --rm --entrypoint python httparena-${fw} -c '${_LOOP_PROBE}'" \
        2>&1 | tail -1) || true
    echo "    $_probe"
    case "$_want:$_probe" in
        1:*uvloop*)   echo "    OK — uvloop active." ;;
        0:*asyncio*)  echo "    OK — stock asyncio loop." ;;
        *) echo "FATAL: $fw did not get the intended event loop (wanted BB_UVLOOP=${_want}); probe said: ${_probe}" >&2
           exit 1 ;;
    esac
done

# --- prove the asgiscope variant's BB_FORCE_ASGI_SCOPE the same way.  The
# silent-failure shape is identical: if the ENV never lands in the image, the
# arm still produces a full set of plausible numbers that read as "scope
# forcing is free" — so abort rather than measure a mislabelled arm.
_SCOPE_PROBE='
import os
from blackbull.env import get_settings, reset_settings_cache
reset_settings_cache()
s = get_settings()
print("BB_FORCE_ASGI_SCOPE=" + str(os.environ.get("BB_FORCE_ASGI_SCOPE"))
      + " setting=" + str(s.force_asgi_scope))
'
for fw in $FRAMEWORKS; do
    case "$fw" in blackbull-asgiscope) ;; *) continue ;; esac
    echo ">>> verifying ASGI scope forcing for $fw (expect BB_FORCE_ASGI_SCOPE=1) ..."
    _probe=$(ssh "${SSH_OPTS[@]}" "$SERVER_REMOTE" \
        "sudo docker run --rm --entrypoint python httparena-${fw} -c '${_SCOPE_PROBE}'" \
        2>&1 | tail -1) || true
    echo "    $_probe"
    case "$_probe" in
        *BB_FORCE_ASGI_SCOPE=1*setting=True*) echo "    OK — ASGI scope forced." ;;
        *) echo "FATAL: $fw did not get BB_FORCE_ASGI_SCOPE=1; probe said: ${_probe}" >&2
           exit 1 ;;
    esac
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
    "bash ~/install_docker_shim.sh '${WEB_WORKERS}' '${WEB_NOFILE}' '${WRK_CPUS}' '${WRK_NOFILE}' '${BB_ACCESS_LOG}' '${BB_PHASE_TRACE}' '${BB_LOG_BATCH_SIZE:-}' '${BB_LOG_BATCH_TIMEOUT_MS:-}' '${PYTHONTRACEMALLOC:-}'"
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
# ready_check.sh backs the SKIP_VALIDATE=1 path (WSL2-validated wheels):
# run_httparena.sh runs it in place of the full validate.sh.
scp "${SSH_OPTS[@]}" \
    "$REPO_ROOT/bench/httparena/ready_check.sh" \
    "$SERVER_REMOTE:~/ready_check.sh"

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
            # A hung daemon is not a perturbation risk: nothing can be
            # measured while docker cannot answer, so restarting it is
            # recovery rather than interference.  HttpArena restarts the
            # daemon itself between profiles (68 times in a 5-way run); one
            # restart failing is what cost a previous run 23 cells, because
            # this watchdog named the problem and did nothing about it.
            if [[ "$probe" == *docker=HUNG* ]] && [ "${WATCHDOG_RECOVER:-1}" = "1" ]; then
                echo "  [watchdog] docker is hung — attempting restart (this run will be marked as recovered)"
                timeout 60 ssh "${SSH_OPTS[@]}" "$SERVER_REMOTE" \
                    'sudo systemctl restart docker 2>/dev/null || sudo service docker restart 2>/dev/null' \
                    >/dev/null 2>&1
                for _i in $(seq 1 20); do
                    if timeout 10 ssh "${SSH_OPTS[@]}" "$SERVER_REMOTE" \
                        'timeout 5 sudo docker ps >/dev/null 2>&1'; then
                        echo "  [watchdog] ✓ docker answered again after restart — DOCKER_RECOVERED"
                        strikes=0
                        break
                    fi
                    sleep 3
                done
            fi
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
- BlackBull:  blackbull==$BLACKBULL_VERSION ($([ "$LOCAL_BB_WHEEL" = "1" ] && echo "local wheel: $LOCAL_WHEEL_NAME" || echo "from PyPI"))
- Harness ref: $(cd "$REPO_ROOT" && git rev-parse --short HEAD) — the commit app.py / launcher.py / meta.json were rsynced from, which is NOT necessarily where the wheel came from
- Wheel sha256: $([ -n "${LOCAL_WHEEL:-}" ] && sha256sum "$LOCAL_WHEEL" | awk '{print $1}' || echo n/a)  (n/a = PyPI install)
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

# Emit a simple per-profile framework comparison table (COMPARISON.md) from the
# result JSONs pulled above.  Best-effort: a failure here never fails the run.
echo ">>> generating comparison table ..."
python3 "$REPO_ROOT/bench/httparena/compare_table.py" "$LOCAL_DEST" \
    || echo "    (comparison table skipped — see stderr above)"

echo
echo "=== complete ==="
echo "Artefacts at: $LOCAL_DEST"
echo "  Validate logs:  $LOCAL_DEST/logs/validate-*.log"
echo "  Benchmark logs: $LOCAL_DEST/logs/benchmark-*-*.log"
echo "  wrk logs:       $LOCAL_DEST/logs/wrk-*-*.log"
[ "${BB_ACCESS_LOG:-0}" != "0" ] && \
    echo "  Access logs:    $LOCAL_DEST/logs/bb-access-*-*.log"
echo "  Comparison:     $LOCAL_DEST/COMPARISON.md"
echo "  Provenance:     $LOCAL_DEST/provenance.md"
echo
echo "Instance will be torn down by the EXIT trap."
