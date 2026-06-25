#!/usr/bin/env bash
# bench/httparena/install_docker_shim.sh — runs on EC2 instance.
#
# Installs a wrapper around /usr/bin/docker that injects tuning
# (ulimit, WEB_WORKERS, WRK_CPUS, etc.) into every `docker run` call.
# Called by bench/aws/httparena_compare.sh via scp + exec.
#
# Args: WEB_WORKERS WEB_NOFILE WRK_CPUS WRK_NOFILE [BB_ACCESS_LOG] [BB_PHASE_TRACE]
# All args are optional (empty = no-op for that knob).

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
cat > "$SHIM_STAGING" <<'SHIM_INNER'
#!/usr/bin/env bash
# docker-bench-shim — wraps 'docker run' to inject web-server / wrk tuning.

REAL_DOCKER="__DOCKER_REAL__"
WEB_WORKERS="__WEB_WORKERS__"
WEB_NOFILE="__WEB_NOFILE__"
WRK_CPUS="__WRK_CPUS__"
WRK_NOFILE="__WRK_NOFILE__"
BB_ACCESS_LOG="__BB_ACCESS_LOG__"
BB_PHASE_TRACE="__BB_PHASE_TRACE__"

# Pass non-run subcommands (build, pull, inspect, ps, …) straight through.
if [ "${1:-}" != "run" ]; then
    exec "$REAL_DOCKER" "$@"
fi

# ---- classify this 'docker run' call ----
# Scan all arguments (including 'run' itself) for the image-name pattern.
# wrk image names contain the string "wrk" (e.g. wrk:local).
# All other 'docker run' calls are treated as web-server containers.
is_wrk=0
for arg in "$@"; do
    case "$arg" in
        *wrk*) is_wrk=1; break ;;
    esac
done

shift  # remove the 'run' token; $@ is now the rest of the original args

if [ "$is_wrk" -eq 1 ]; then
    # --- wrk load-generator container ---
    echo "[bench-shim] wrk container: WRK_NOFILE=$WRK_NOFILE WRK_CPUS=${WRK_CPUS:-<no limit>}" >&2
    extra=(--ulimit "nofile=${WRK_NOFILE}:${WRK_NOFILE}")
    [ -n "$WRK_CPUS" ] && extra+=(--cpus "$WRK_CPUS")
    exec "$REAL_DOCKER" run "${extra[@]}" "$@"
else
    # --- web-server container ---
    echo "[bench-shim] web-server container: WEB_NOFILE=$WEB_NOFILE WEB_WORKERS=${WEB_WORKERS:-<framework default>} BB_ACCESS_LOG=${BB_ACCESS_LOG:-0}" >&2
    extra=(--ulimit "nofile=${WEB_NOFILE}:${WEB_NOFILE}")
    extra+=(-v /home/ubuntu/results:/results)
    if [ -n "$WEB_WORKERS" ]; then
        extra+=(
            -e "WEB_WORKERS=${WEB_WORKERS}"
            -e "WEB_CONCURRENCY=${WEB_WORKERS}"
        )
    fi
    [ -n "$BB_ACCESS_LOG" ] && extra+=(-e "BB_ACCESS_LOG=${BB_ACCESS_LOG}")
    [ -n "$BB_PHASE_TRACE" ] && extra+=(-e "BB_PHASE_TRACE=${BB_PHASE_TRACE}")
    exec "$REAL_DOCKER" run "${extra[@]}" "$@"
fi
SHIM_INNER

# Substitute placeholder values into the shim script.
sed -i \
    -e "s|__DOCKER_REAL__|${DOCKER_REAL}|g" \
    -e "s|__WEB_WORKERS__|${_WEB_WORKERS}|g" \
    -e "s|__WEB_NOFILE__|${_WEB_NOFILE}|g" \
    -e "s|__WRK_CPUS__|${_WRK_CPUS}|g" \
    -e "s|__WRK_NOFILE__|${_WRK_NOFILE}|g" \
    -e "s|__BB_ACCESS_LOG__|${_BB_ACCESS_LOG}|g" \
    -e "s|__BB_PHASE_TRACE__|${_BB_PHASE_TRACE}|g" \
    "$SHIM_STAGING"

chmod +x "$SHIM_STAGING"

# Atomically replace /usr/bin/docker with the shim.
sudo install -o root -g root -m 0755 "$SHIM_STAGING" "$DOCKER_ON_PATH"
echo "    shim installed: $DOCKER_ON_PATH → shim (real binary saved as $DOCKER_REAL)"
