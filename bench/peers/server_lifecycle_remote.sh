#!/usr/bin/env bash
# bench/peers/server_lifecycle_remote.sh — Sprint 20.
#
# Server-side launch / kill helper invoked over SSH by compare_servers.sh
# when BENCH_REMOTE_LIFECYCLE=1.  Runs on the server instance.  The peer
# launch branches live in run_peer.sh; this script only wraps lifecycle
# (backgrounding, log redirection, port-clean kill) around them.
#
# Subcommands:
#   start <stack> <port> <cert> <key> <logfile_rel>
#       nohup'd `bash run_peer.sh ...` with stdout/stderr → $REMOTE_REPO/$logfile_rel.
#       BIND_HOST is read from env (defaults to 127.0.0.1 if missing, but
#       compare_servers.sh always sets it).
#   kill_existing
#       Mirror of compare_servers.sh's kill_existing_local — by-port fuser
#       + by-name pkill belt, then wait for $BASE_PORT to be free.

set -u

usage() {
    echo "Usage: $0 start <stack> <port> <cert> <key> <logfile_rel>" >&2
    echo "       $0 kill_existing      (reads BASE_PORT from env)" >&2
    return 2
}

cmd="${1:-}"
shift || true

case "$cmd" in
    start)
        if [ $# -lt 5 ]; then usage; exit 2; fi
        stack="$1"; port="$2"; cert="$3"; key="$4"; logfile_rel="$5"
        mkdir -p "$(dirname "$logfile_rel")"

        # Propagate the per-stack log knob that compare_servers.sh sets for
        # granian's direct-FileHandler path.  Other stacks ignore it.
        granian_env=""
        if [ -n "${GRANIAN_LOG_TARGET:-}" ]; then
            granian_env="GRANIAN_LOG_TARGET=$GRANIAN_LOG_TARGET"
        fi

        # BIND_HOST is the seam introduced for split topology.  Default to
        # 0.0.0.0 in this script even if the env var is missing, because by
        # construction nobody invokes server_lifecycle_remote.sh in the
        # single-host path — being permissive here just protects against an
        # orchestrator misconfiguration.
        bind_host="${BIND_HOST:-0.0.0.0}"

        # Activate the project venv so run_peer.sh can find the peer
        # binaries (blackbull / uvicorn / hypercorn / granian / daphne).
        # The local orchestrator path is invoked from within an
        # already-activated venv by run.sh; the remote path needs to do it
        # itself because the SSH session inherits the login shell's clean
        # environment.
        if [ -f .venv/bin/activate ]; then
            # shellcheck disable=SC1091
            source .venv/bin/activate
        fi

        # Optional CPU pinning for Sprint 21 Phase B (w=2→w=4 scaling
        # diagnosis).  When BB_BENCH_TASKSET is set, prefix the launch
        # with `taskset -c $BB_BENCH_TASKSET` so the worker pool inherits
        # the CPU mask.  Defaults to no pinning, matching prior behaviour.
        taskset_prefix=()
        if [ -n "${BB_BENCH_TASKSET:-}" ]; then
            taskset_prefix=(taskset -c "$BB_BENCH_TASKSET")
        fi

        # Detach so the SSH session can return immediately; orchestrator's
        # wait_ready does the actual readiness probe.  Redirect stdin from
        # /dev/null per Sprint 10/11 caution: pipe stdin to a non-reader
        # is a known deadlock shape.
        nohup env $granian_env BIND_HOST="$bind_host" PATH="$PATH" \
            "${taskset_prefix[@]}" \
            bash bench/peers/run_peer.sh "$stack" "$port" "$cert" "$key" \
            </dev/null >"$logfile_rel" 2>&1 &
        echo $! > /tmp/bench_server.pid
        disown
        ;;

    kill_existing)
        BASE_PORT="${BASE_PORT:-8443}"
        if command -v fuser >/dev/null 2>&1; then
            fuser -k -9 -n tcp "$BASE_PORT"  2>/dev/null || true
            fuser -k -9 -n tcp "$((BASE_PORT+1))" 2>/dev/null || true
        fi
        pkill -9 -f "bench/app.py"         2>/dev/null || true
        pkill -9 -f "bench.peers.asgi_app" 2>/dev/null || true
        pkill -9 -f "hypercorn"            2>/dev/null || true
        pkill -9 -f "uvicorn"              2>/dev/null || true
        pkill -9 -f "granian"              2>/dev/null || true
        pkill -9 -f "daphne"               2>/dev/null || true
        pkill -9 -f "nginx.*bench/peers"   2>/dev/null || true
        for _ in $(seq 1 20); do
            if ! ss -tln 2>/dev/null | grep -q ":$BASE_PORT "; then
                exit 0
            fi
            sleep 0.5
        done
        echo "server_lifecycle_remote.sh: port $BASE_PORT still bound after kill" >&2
        ss -tlnp 2>/dev/null | grep ":$BASE_PORT " >&2 || true
        exit 0
        ;;

    *)
        usage
        exit 2
        ;;
esac
