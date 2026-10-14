# bench/cloud/jobs/httparena.sh — HttpArena's own validate + benchmark on EC2
# (`just httparena-bench`).
#
# Prepares the instance itself (Docker, gcannon, wrk, h2load, ghz, a fresh
# MDA2AV/HttpArena clone), vendors bench/httparena/ as one framework directory
# per BlackBull variant, then runs bench/httparena/run_httparena.sh detached.
# AMD by default: no SMT, so HttpArena's half-split cpusets isolate the server
# from the load generator; an Intel type puts both on sibling threads.
#
# Env (defaults):
#   PROFILES (required)  space-separated HttpArena profiles
#   FRAMEWORKS(=blackbull fastapi)  also blackbull-uvloop, blackbull-asgiscope,
#       blackbull-base (LOCAL_BB_WHEEL=1 + BASE_BB_WHEEL_PATH), sanic, aiohttp.
#       Put variants side by side to measure them on one instance in one session.
#   INSTANCE_TYPE(=c7a.2xlarge)  BLACKBULL_VERSION(=pyproject's)  PYTHON_IMAGE(=python:3.13-slim)
#   LOCAL_BB_WHEEL(=0)  1 = build and install a wheel of the working tree (BB_WHEEL_PATH: a built one)
#   BB_UVLOOP(=0) BB_ACCESS_LOG(=0) BB_PHASE_TRACE
#   SKIP_VALIDATE(=0)  1 = the minimal ready_check.sh instead of validate.sh, for a
#       wheel already validated with bench/httparena/validate_local.sh
#   STAGE_PEERS(=1)  0 = upstream's sanic / aiohttp entries instead of this repo's;
#       ours implement our endpoints on their framework, which is endpoint parity,
#       not evidence about those projects — say which one a writeup used
#   WEB_WORKERS(=framework default) WEB_NOFILE(=65536) WRK_CPUS WRK_NOFILE(=65536)
#   DATABASE_MAX_CONN  Postgres connection pool for the whole cluster
#   WATCHDOG_RECOVER(=1)  restart a hung Docker daemon between polls

: "${INSTANCE_TYPE:=c7a.2xlarge}"
TOPO=single

HTTPARENA_KNOBS=(PROFILES FRAMEWORKS INSTANCE_TYPE BLACKBULL_VERSION PYTHON_IMAGE LOCAL_BB_WHEEL
                 BB_UVLOOP BB_ACCESS_LOG BB_PHASE_TRACE SKIP_VALIDATE STAGE_PEERS WEB_WORKERS
                 WEB_NOFILE WRK_CPUS WRK_NOFILE DATABASE_MAX_CONN)

job_preflight() {
    [ -n "${PROFILES:-}" ] || { echo "httparena: set PROFILES (e.g. 'baseline json static')" >&2; return 1; }
    : "${FRAMEWORKS:=blackbull fastapi}" "${PYTHON_IMAGE:=python:3.13-slim}" "${LOCAL_BB_WHEEL:=0}"
    : "${BB_UVLOOP:=0}" "${BB_ACCESS_LOG:=0}" "${SKIP_VALIDATE:=0}" "${STAGE_PEERS:=1}"
    : "${WEB_NOFILE:=65536}" "${WRK_NOFILE:=65536}"
    : "${BLACKBULL_VERSION:=$(sed -n 's/^version *= *"\(.*\)"/\1/p' pyproject.toml)}"
    case " $FRAMEWORKS " in *" blackbull-base "*)
        if [ "$LOCAL_BB_WHEEL" != 1 ] || [ ! -f "${BASE_BB_WHEEL_PATH:-}" ]; then
            echo "httparena: blackbull-base needs LOCAL_BB_WHEEL=1 and BASE_BB_WHEEL_PATH=<wheel>" >&2
            return 1
        fi ;;
    esac
    if [[ "$INSTANCE_TYPE" =~ [0-9]i ]]; then
        echo "WARNING: $INSTANCE_TYPE is Intel with SMT: the server and load-generator cpusets share physical cores." >&2
    fi
    JOB_WAIT_SECONDS=10800
    JOB_DEPLOY_GIT=0
    JOB_INSTALL=0
    JOB_SETTINGS=()
    local knob
    for knob in "${HTTPARENA_KNOBS[@]}"; do JOB_SETTINGS+=("${knob,,}=${!knob:-}"); done
}

_ssh() { ssh -n "${SSH_OPTS[@]}" "$HA_HOST" "$@"; }
_rsync() { rsync -e "ssh ${SSH_OPTS[*]}" -az "$@"; }

_install_tooling() {
    echo ">>> Docker + HttpArena load tooling ..."
    _ssh 'set -euo pipefail
        sudo apt-get update -qq
        sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq docker.io git jq curl \
            ca-certificates build-essential pkg-config wrk nghttp2-client >/dev/null
        sudo systemctl enable --now docker >/dev/null
        sudo usermod -aG docker ubuntu
        mkdir -p ~/build' || return 1
    # gcannon needs liburing >= 2.9 (built from source) and kernel 6.1+ io_uring.
    _ssh 'set -euo pipefail; cd ~/build
        if ! pkg-config --atleast-version=2.9 liburing 2>/dev/null; then
            rm -rf liburing
            git clone --quiet --depth 1 --branch liburing-2.9 https://github.com/axboe/liburing.git
            (cd liburing && ./configure --prefix=/usr >/dev/null && make -s -j"$(nproc)" -C src \
                && sudo make -s install -C src >/dev/null && sudo ldconfig)
        fi
        if ! command -v gcannon >/dev/null; then
            rm -rf gcannon
            git clone --quiet --depth 1 https://github.com/MDA2AV/gcannon.git
            (cd gcannon && make -s && sudo cp gcannon /usr/local/bin/)
        fi' || return 1
    # ghz backs every gRPC profile's readiness probe as well as its load: without
    # it framework.sh aborts and those profiles silently produce no numbers.
    _ssh 'set -euo pipefail; cd ~/build
        if ! command -v ghz >/dev/null; then
            curl -fsSL -o ghz.tar.gz \
                https://github.com/bojand/ghz/releases/download/v0.120.0/ghz-linux-x86_64.tar.gz
            mkdir -p ghz-dist && tar -xzf ghz.tar.gz -C ghz-dist
            sudo install -m 0755 ghz-dist/ghz /usr/local/bin/ghz
        fi
        for t in gcannon wrk h2load ghz; do command -v $t >/dev/null || { echo "missing $t" >&2; exit 1; }; done'
}

_clone_httparena() {
    echo ">>> cloning MDA2AV/HttpArena ..."
    _ssh 'set -euo pipefail; cd ~; sudo rm -rf HttpArena
        git clone --depth 1 https://github.com/MDA2AV/HttpArena.git' || return 1
    # Upstream pins Redis and the load generator to CPUs a small instance lacks.
    scp "${SSH_OPTS[@]}" bench/httparena/patch_cpuset.sh "$HA_HOST:build/patch_cpuset.sh" >/dev/null \
        && _ssh 'bash ~/build/patch_cpuset.sh' || return 1
    # Upstream divides a 256-connection Postgres pool by each entry's worker
    # count; a small instance would get a far larger per-worker pool than the
    # published runs, changing the regime under test.
    if [ -n "${DATABASE_MAX_CONN:-}" ]; then
        _ssh "sed -i 's/DATABASE_MAX_CONN=256/DATABASE_MAX_CONN=${DATABASE_MAX_CONN}/g' \
            HttpArena/scripts/lib/framework.sh" || return 1
    fi
}

# One framework directory per BlackBull variant.  The variants differ only in
# the last ENV layer, so they share every expensive image layer.
_stage_blackbull() {
    local fw="$1" uvloop="$2" scope="$3" wheel="${4:-${LOCAL_WHEEL:-}}"
    local dir="HttpArena/frameworks/$fw" install
    echo ">>> staging $fw (BB_UVLOOP=$uvloop BB_FORCE_ASGI_SCOPE=${scope:-unset})"
    _ssh "mkdir -p $dir" || return 1
    _rsync --delete bench/httparena/{app.py,launcher.py,meta.json,db.py,grpc_bench.py} "$HA_HOST:$dir/" || return 1
    # Upstream's stale .dockerignore keeps db.py / grpc_bench.py out of the context.
    _ssh "rm -f $dir/.dockerignore" || return 1
    if [ "$LOCAL_BB_WHEEL" = 1 ]; then
        _rsync "$wheel" "$HA_HOST:$dir/" || return 1
        install="COPY $(basename "$wheel") /wheels/
RUN pip install --no-cache-dir \"/wheels/$(basename "$wheel")[compression,speed]\" asyncpg redis"
    else
        install="RUN pip install --no-cache-dir 'blackbull[compression,speed]==$BLACKBULL_VERSION' asyncpg redis"
    fi
    _ssh "cat > $dir/Dockerfile" <<EOF || return 1
# Generated by bench/cloud/jobs/httparena.sh.
FROM $PYTHON_IMAGE
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
$install
VOLUME /results
COPY app.py launcher.py db.py grpc_bench.py /app/
ENV BB_UVLOOP=$uvloop
${scope:+ENV BB_FORCE_ASGI_SCOPE=$scope}
EXPOSE 8080 8081 8443
CMD ["python", "launcher.py"]
EOF
    # Enable the entry under its own name, and give validate.sh a build.sh
    # without --no-cache so it reuses the pre-built layers.
    _ssh "sed -i 's/\"enabled\": false/\"enabled\": true/; s/\"display_name\": \"blackbull\"/\"display_name\": \"$fw\"/' $dir/meta.json
        printf '#!/usr/bin/env bash\nset -euo pipefail\ncd \"\$(dirname \"\$0\")\"\ndocker build -t \"httparena-\$(basename \"\$PWD\")\" .\n' > $dir/build.sh
        chmod +x $dir/build.sh"
}

_stage_peer() {  # sanic | aiohttp: this repo's implementation of our endpoints
    local fw="$1" dir="HttpArena/frameworks/$1"
    echo ">>> staging $fw (this repo's implementation, STAGE_PEERS=1)"
    _ssh "mkdir -p $dir" || return 1
    _rsync --delete "bench/httparena/$fw/"{app.py,launcher.py,meta.json,requirements.txt,Dockerfile,build.sh} \
        "$HA_HOST:$dir/" || return 1
    _ssh "chmod +x $dir/build.sh; sed -i 's/\"enabled\": false/\"enabled\": true/' $dir/meta.json"
}

# A mislabelled arm produces plausible numbers, so ask each image which loop
# and scope mode it really runs, through BlackBull's own settings, and stop.
_verify_images() {
    local fw want probe
    for fw in $FRAMEWORKS; do
        case "$fw" in
            blackbull|blackbull-uvloop|blackbull-base|blackbull-asgiscope) ;;
            *) continue ;;
        esac
        want="$BB_UVLOOP"; [ "$fw" = blackbull-uvloop ] && want=1
        probe="$(_ssh "sudo docker run --rm --entrypoint python httparena-$fw -c '
import asyncio, os
from blackbull.env import apply_event_loop_policy, get_settings, reset_settings_cache
reset_settings_cache(); apply_event_loop_policy(); loop = asyncio.new_event_loop()
print(type(loop).__module__, get_settings().force_asgi_scope)'" 2>&1 | tail -1)"
        echo "    $fw: $probe"
        case "$want:$probe" in 1:uvloop*|0:asyncio*) ;; *)
            echo "httparena: $fw did not get BB_UVLOOP=$want ($probe)" >&2; return 1 ;;
        esac
        if [ "$fw" = blackbull-asgiscope ] && [[ "$probe" != *True ]]; then
            echo "httparena: $fw did not get BB_FORCE_ASGI_SCOPE=1 ($probe)" >&2; return 1
        fi
    done
}

job_setup() {
    HA_HOST="$1"
    if [ "$LOCAL_BB_WHEEL" = 1 ]; then
        LOCAL_WHEEL="${BB_WHEEL_PATH:-}"
        if [ ! -f "$LOCAL_WHEEL" ]; then
            echo ">>> building a wheel of the working tree ..."
            python3 -m build --wheel --outdir "$BENCH_TMP" . > /dev/null || return 1
            LOCAL_WHEEL="$(ls "$BENCH_TMP"/blackbull-*.whl | head -1)"
        fi
        sha256sum "$LOCAL_WHEEL" > "$LOCAL_RUN/wheel.sha256"
        [ -z "${BASE_BB_WHEEL_PATH:-}" ] || sha256sum "$BASE_BB_WHEEL_PATH" >> "$LOCAL_RUN/wheel.sha256"
    fi
    _install_tooling && _clone_httparena || return 1
    local fw
    for fw in $FRAMEWORKS; do
        case "$fw" in
            blackbull) _stage_blackbull blackbull "$BB_UVLOOP" "" ;;
            blackbull-uvloop) _stage_blackbull blackbull-uvloop 1 "" ;;
            blackbull-asgiscope) _stage_blackbull blackbull-asgiscope "$BB_UVLOOP" 1 ;;
            blackbull-base) _stage_blackbull blackbull-base "$BB_UVLOOP" "" "$BASE_BB_WHEEL_PATH" ;;
            sanic|aiohttp) [ "$STAGE_PEERS" = 1 ] && _stage_peer "$fw" ;;
            *) true ;;
        esac || return 1
    done
    # Build before the shim replaces docker, so validate.sh's 300 s budget is
    # not spent compiling brotli / zstandard.
    for fw in $FRAMEWORKS; do
        _ssh "cd HttpArena && if [ -x frameworks/$fw/build.sh ]; then sudo frameworks/$fw/build.sh; \
              else sudo docker build -t httparena-$fw frameworks/$fw; fi" \
            || echo "    (pre-build of $fw failed; validate.sh will retry)"
    done
    _verify_images || return 1
    scp "${SSH_OPTS[@]}" bench/httparena/install_docker_shim.sh "$HA_HOST:build/install_docker_shim.sh" > /dev/null \
        && _ssh "bash ~/build/install_docker_shim.sh '${WEB_WORKERS:-}' '$WEB_NOFILE' '${WRK_CPUS:-}' \
            '$WRK_NOFILE' '$BB_ACCESS_LOG' '${BB_PHASE_TRACE:-}' '${BB_LOG_BATCH_SIZE:-}' \
            '${BB_LOG_BATCH_TIMEOUT_MS:-}' '${PYTHONTRACEMALLOC:-}'" || return 1
    _ssh "mkdir -p '$REMOTE_REPO/bench/httparena'" \
        && scp "${SSH_OPTS[@]}" bench/httparena/run_httparena.sh bench/httparena/ready_check.sh \
            "$HA_HOST:$REMOTE_REPO/bench/httparena/" > /dev/null
}

job_runner() {
    printf 'cd %q\n' "$REMOTE_REPO"
    printf 'BENCH_RUN_DIR=%q bash bench/httparena/run_httparena.sh %q %q %q %q\n' "$REMOTE_RUN" \
        "${FRAMEWORKS// /,}" "${PROFILES// /,}" "$SKIP_VALIDATE" "${WRK_CPUS:-}"
    echo 'rc=$?'
    # HttpArena's own result files, for compare_table.py.
    printf 'rsync -a --include="*/" --include="*.json" --include="*.tsv" --include="*.csv" --include="*.md" --include="*.log" --exclude="*" ~/HttpArena/ %q/httparena-tree/\n' "$REMOTE_RUN"
    echo 'exit $rc'
}

# A hung Docker daemon emits nothing for the whole validate gate, so a frozen
# log looks like slow progress.  Restarting it is recovery, not interference:
# nothing can be measured while it cannot answer, and HttpArena restarts it
# between profiles itself.
job_on_poll() {
    [ "${WATCHDOG_RECOVER:-1}" = 1 ] || return 0
    if ! timeout 15 ssh -n "${SSH_OPTS[@]}" "$1" 'timeout 5 sudo docker ps >/dev/null 2>&1' 2>/dev/null; then
        echo "  [watchdog] docker did not answer — restarting it (DOCKER_RECOVERED marks this run)"
        echo "DOCKER_RECOVERED $(date -u +%FT%TZ)" >> "$LOCAL_RUN/watchdog.log"
        timeout 60 ssh -n "${SSH_OPTS[@]}" "$1" 'sudo systemctl restart docker' > /dev/null 2>&1 || true
    fi
}

job_collect() {
    python3 bench/httparena/compare_table.py "$LOCAL_RUN" \
        || echo "    (comparison table skipped)"
    compgen -G "$LOCAL_RUN/logs/benchmark-*.log" > /dev/null
}
