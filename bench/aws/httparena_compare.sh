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

PROFILES="${PROFILES:-baseline json json-tls static}"
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
    echo ">>> BlackBull version: $BLACKBULL_VERSION (from LOCAL wheel)"
else
    echo ">>> BlackBull version: $BLACKBULL_VERSION (from PyPI)"
fi

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
)
if [ "${BB_ACCESS_LOG:-0}" != "0" ]; then
    _BB_RSYNC_FILES+=("$REPO_ROOT/bench/httparena/logging_access.ini")
fi
rsync -e "ssh ${SSH_OPTS[*]}" -az --delete \
    "${_BB_RSYNC_FILES[@]}" \
    "$SERVER_REMOTE:HttpArena/frameworks/blackbull/"

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
RUN cd /tmp && pip install --no-cache-dir "/tmp/${LOCAL_WHEEL_NAME}[compression]"
VOLUME /results

COPY app.py launcher.py /app/
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

RUN pip install --no-cache-dir 'blackbull[compression]==${BLACKBULL_VERSION}'
VOLUME /results

COPY app.py launcher.py /app/
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
# The shim wraps every subsequent `docker run` call to inject tuning:
#
#   Web-server containers  → --ulimit nofile=WEB_NOFILE:WEB_NOFILE
#                            -e WEB_WORKERS=<value>       (when set)
#                            -e WEB_CONCURRENCY=<value>   (when set; FastAPI)
#   wrk containers         → --ulimit nofile=WRK_NOFILE:WRK_NOFILE
#                            (WRK_CPUS handled via GCANNON_CPUS/taskset in Step 7)
#
# Strategy:
#   1. Resolve the real docker binary path and save it as DOCKER_REAL
#      (e.g. /usr/bin/docker.real).
#   2. Hard-copy the real binary to that new path.
#   3. Write the shim to ~/docker-bench-shim (user-writable).
#   4. sudo install the shim over /usr/bin/docker.
#   5. The shim's REAL_DOCKER variable points to /usr/bin/docker.real
#      — NOT to /usr/bin/docker — so there is NO infinite recursion.
#
# The shim is removed in Step 9 by restoring /usr/bin/docker from the
# saved real binary.
#
# FD limits are logged to stderr on every docker run so the values
# appear in all benchmark and validate logs.
# ---------------------------------------------------------------------------
echo ">>> installing docker-bench shim on the instance ..."
# Pass the four tuning values as positional arguments to avoid
# quoting/expansion pitfalls with environment-variable injection.
ssh "${SSH_OPTS[@]}" "$SERVER_REMOTE" bash -s \
    -- "${WEB_WORKERS}" "${WEB_NOFILE}" "${WRK_CPUS}" "${WRK_NOFILE}" "${BB_ACCESS_LOG}" "${BB_PHASE_TRACE}" <<'REMOTE_SHIM'
set -euo pipefail

_WEB_WORKERS="${1:-}"
_WEB_NOFILE="${2:-}"
_WRK_CPUS="${3:-}"
_WRK_NOFILE="${4:-}"
_BB_ACCESS_LOG="${5:-}"
_BB_PHASE_TRACE="${6:-}"

# Locate the docker binary currently on PATH (not yet shimmed).
DOCKER_ON_PATH="$(command -v docker)"

# Choose a stable "real binary" path that the shim can always exec.
# We copy (not move) the binary to a fixed name so /usr/bin/docker can
# be replaced with the shim without changing anything else on the system.
DOCKER_REAL="${DOCKER_ON_PATH}.real"

sudo cp -f "$DOCKER_ON_PATH" "$DOCKER_REAL"
sudo chmod 0755 "$DOCKER_REAL"

SHIM_STAGING="$HOME/docker-bench-shim"

# Write the shim to a user-writable location, then sudo-install it.
cat > "$SHIM_STAGING" <<SHIM_INNER
#!/usr/bin/env bash
# docker-bench-shim — wraps 'docker run' to inject web-server / wrk tuning.
# Generated by bench/aws/httparena_compare.sh — do not edit by hand.
#
# REAL_DOCKER points to the copy of the original binary made at shim-
# install time.  It must NOT point to /usr/bin/docker (shim itself).

REAL_DOCKER="${DOCKER_REAL}"
WEB_WORKERS="${_WEB_WORKERS}"
WEB_NOFILE="${_WEB_NOFILE}"
WRK_CPUS="${_WRK_CPUS}"
WRK_NOFILE="${_WRK_NOFILE}"
BB_ACCESS_LOG="${_BB_ACCESS_LOG}"
BB_PHASE_TRACE="${_BB_PHASE_TRACE}"

# Pass non-run subcommands (build, pull, inspect, ps, …) straight through.
if [ "\${1:-}" != "run" ]; then
    exec "\$REAL_DOCKER" "\$@"
fi

# ---- classify this 'docker run' call ----
# Scan all arguments (including 'run' itself) for the image-name pattern.
# wrk image names contain the string "wrk" (e.g. wrk:local).
# All other 'docker run' calls are treated as web-server containers.
is_wrk=0
for arg in "\$@"; do
    case "\$arg" in
        *wrk*) is_wrk=1; break ;;
    esac
done

shift  # remove the 'run' token; \$@ is now the rest of the original args

if [ "\$is_wrk" -eq 1 ]; then
    # --- wrk load-generator container ---
    echo "[bench-shim] wrk container: WRK_NOFILE=\$WRK_NOFILE WRK_CPUS=\${WRK_CPUS:-<no limit>}" >&2
    extra=(--ulimit "nofile=\${WRK_NOFILE}:\${WRK_NOFILE}")
    [ -n "\$WRK_CPUS" ] && extra+=(--cpus "\$WRK_CPUS")
    exec "\$REAL_DOCKER" run "\${extra[@]}" "\$@"
else
    # --- web-server container ---
    echo "[bench-shim] web-server container: WEB_NOFILE=\$WEB_NOFILE WEB_WORKERS=\${WEB_WORKERS:-<framework default>} BB_ACCESS_LOG=\${BB_ACCESS_LOG:-0}" >&2
    extra=(--ulimit "nofile=\${WEB_NOFILE}:\${WEB_NOFILE}")
    extra+=(-v /home/ubuntu/results:/results)
    if [ -n "\$WEB_WORKERS" ]; then
        # WEB_WORKERS: read by BlackBull's launcher.py
        # WEB_CONCURRENCY: read by uvicorn/FastAPI (standard uvicorn env var)
        # Both are injected so the same WEB_WORKERS value works for all frameworks.
        extra+=(
            -e "WEB_WORKERS=\${WEB_WORKERS}"
            -e "WEB_CONCURRENCY=\${WEB_WORKERS}"
        )
    fi
    # BB_ACCESS_LOG: forwarded as env var so launcher.py enables the StreamHandler
    # that writes every request to stderr with a "[ACCESS]" prefix.
    # benchmark.sh's save_result() captures docker logs (including stderr) to
    # site/static/logs/<profile>/<conns>/blackbull.log before the container is
    # removed; httparena_compare.sh greps [ACCESS] lines from that file.
    [ -n "\$BB_ACCESS_LOG" ] && extra+=(-e "BB_ACCESS_LOG=\${BB_ACCESS_LOG}")
    [ -n "\$BB_PHASE_TRACE" ] && extra+=(-e "BB_PHASE_TRACE=\${BB_PHASE_TRACE}")
    exec "\$REAL_DOCKER" run "\${extra[@]}" "\$@"
fi
SHIM_INNER

chmod +x "$SHIM_STAGING"

# Atomically replace /usr/bin/docker with the shim.
sudo install -o root -g root -m 0755 "$SHIM_STAGING" "$DOCKER_ON_PATH"
echo "    shim installed: $DOCKER_ON_PATH → shim (real binary saved as $DOCKER_REAL)"
REMOTE_SHIM

# ---------------------------------------------------------------------------
# Step 7 — run HttpArena's official validate + benchmark scripts for
# each (framework × profile) combination.  Output is captured under
# ~/results/ on the instance and rsync'd back at the end.
#
# wrk stdout/stderr are captured to separate files:
#   ~/results/wrk-<fw>-<prof>.log   (stdout from docker logs)
#   ~/results/wrk-<fw>-<prof>.err   (stderr / shim annotations)
# ---------------------------------------------------------------------------
ssh "${SSH_OPTS[@]}" "$SERVER_REMOTE" 'mkdir -p results'

if [ "$SKIP_VALIDATE" != "1" ]; then
    echo ">>> HttpArena validate (correctness check) ..."
    for fw in $FRAMEWORKS; do
        echo "  - $fw"
        # VALIDATE_TIMEOUT=600 gives 10 minutes headroom.
        # The image is already cached from Step 5, so the docker build
        # inside validate.sh completes in <5 seconds from the layer cache.
        ssh "${SSH_OPTS[@]}" "$SERVER_REMOTE" "
            set -euo pipefail
            cd HttpArena
            sudo VALIDATE_TIMEOUT=600 ./scripts/validate.sh $fw 2>&1 | tee ~/results/validate-${fw}.log
        " || echo "    (validate non-zero for $fw — kept going; see log)"
    done
fi

echo ">>> HttpArena benchmark ..."
# ---------------------------------------------------------------------------
# benchmark.sh を (framework × profile) の組み合わせごとに呼ぶ。
#
# WRK_CPUS → GCANNON_CPUS:
#   HttpArena の wrk_run() が `taskset -c "$GCANNON_CPUS" wrk …` で
#   CPU ピニングを行う。`sudo` は env を落とすため `env KEY=VAL` を
#   sudo の後ろに置いてポリシーによらず注入する。
#   平整数 ("24") は "0-23" に変換、範囲文字列はそのまま渡す。
# ---------------------------------------------------------------------------

# GCANNON_CPUS を一度だけ計算する（ループ外）
if [ -n "${WRK_CPUS}" ]; then
    if [[ "${WRK_CPUS}" =~ ^[0-9]+$ ]]; then
        _GCANNON_CPUS="0-$(( WRK_CPUS - 1 ))"
    else
        _GCANNON_CPUS="${WRK_CPUS}"
    fi
    _SUDO_ENV="env GCANNON_CPUS=${_GCANNON_CPUS}"
else
    _SUDO_ENV=""
fi

for fw in $FRAMEWORKS; do
    for prof in $PROFILES; do
        echo "  - $fw / $prof"
        ssh "${SSH_OPTS[@]}" "$SERVER_REMOTE" "
            set -euo pipefail
            cd HttpArena
            # --save invokes save_result() which writes docker logs to
            # site/static/logs/<profile>/<conns>/<fw>.log before the container
            # is removed.  Without --save that file is never created, so the
            # [ACCESS] grep below finds nothing and bb-access-*.log stays empty.
            sudo ${_SUDO_ENV} ./scripts/benchmark.sh $fw $prof --save 2>&1 \
                | tee ~/results/benchmark-${fw}-${prof}.log

            # Extract shim annotations for wrk from combined output.
            grep -E '^\[bench-shim\].*wrk' \
                ~/results/benchmark-${fw}-${prof}.log \
                > ~/results/wrk-${fw}-${prof}.log 2>/dev/null || true

            # Best-effort: grab docker logs from any exited wrk containers.
            for cid in \$(sudo docker ps -a --filter 'ancestor=wrk' -q 2>/dev/null); do
                sudo docker logs \"\$cid\" \
                    >> ~/results/wrk-${fw}-${prof}.log \
                    2>> ~/results/wrk-${fw}-${prof}.err \
                    || true
            done

        " || echo "    (benchmark non-zero for $fw / $prof — kept going)"
    done
done

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
