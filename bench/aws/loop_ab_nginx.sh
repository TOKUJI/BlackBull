#!/usr/bin/env bash
# bench/aws/loop_ab_nginx.sh — what uvloop is worth on CPython, behind nginx.
#
# Topology (the one you'd actually deploy):
#
#   wrk -c$CONNS -> nginx :8443 (TLS terminated, upstream keep-alive pool)
#                -> BlackBull :8444 (cleartext HTTP/1.1)
#
# nginx is the constant: one binary, one config, started once, never
# restarted between arms.  The only thing that changes is the upstream's
# event loop.
#
#   arm A = BB_UVLOOP=1   (libuv)
#   arm B = BB_UVLOOP=0   (stock asyncio)
#
# Both arms run on ONE instance in ONE session, and the arms are
# INTERLEAVED across $CYCLES repetitions (A B A B A B), not run once each
# back to back.  Two reasons, both learned the hard way:
#
#   - A single window per arm reports no spread, so a small delta cannot be
#     told apart from one arm drawing a luckier window.
#   - Running all of A then all of B aliases the arm against anything that
#     drifts monotonically during the run (page cache, thermal, a noisy
#     neighbour).  Interleaving decorrelates the arm from wall-clock.
#
# Never compare an arm here against a number from a different invocation:
# the machine, the AMI, the kernel and the build all move between runs.
# The delta this script prints is the only supported reading.
#
# Usage:
#   INSTANCE_TYPE=m7a.2xlarge bash bench/aws/loop_ab_nginx.sh
#
# Env knobs:
#   INSTANCE_TYPE  EC2 type for the single host        (default m7a.2xlarge)
#   CYCLES         interleaved A/B repetitions         (default 3)
#   PROFILES       routes to measure, space-separated  (default "ping json")
#   WEB_WORKERS    BlackBull worker processes          (default 3)
#   NGINX_WORKERS  nginx worker_processes              (default 2)
#   CONNS/THREADS  wrk connections / threads           (default 256 / 4)
#   WARMUP         per-arm-activation warmup seconds   (default 15)
#   DURATION       per-measurement seconds             (default 30)
#   TAG            result-directory prefix             (default loop-ab-nginx)
#   SKIP_PROVISION reuse a running instance from .state
set -euo pipefail

: "${INSTANCE_TYPE:=m7a.2xlarge}"
export INSTANCE_TYPE

# shellcheck source=config.sh
source "$(dirname "$0")/config.sh"
_bench_aws_check_env

export TOPO=single

CYCLES="${CYCLES:-3}"
PROFILES="${PROFILES:-ping json}"
WEB_WORKERS="${WEB_WORKERS:-3}"
NGINX_WORKERS="${NGINX_WORKERS:-2}"
CONNS="${CONNS:-256}"
THREADS="${THREADS:-4}"
WARMUP="${WARMUP:-15}"
DURATION="${DURATION:-30}"
TAG="${TAG:-loop-ab-nginx}"
KEEP_INSTANCE="${KEEP_INSTANCE:-0}"
SKIP_PROVISION="${SKIP_PROVISION:-0}"

UPSTREAM_PORT=8444
LISTEN_PORT=8443

TS="$(date -u +%Y%m%d-%H%M%SZ)"
LOCAL_DEST="$REPO_ROOT/bench/results/loop-ab-nginx/${TAG}-${INSTANCE_TYPE}-${TS}"
mkdir -p "$LOCAL_DEST"

exec > >(tee -a "$LOCAL_DEST/driver.log") 2>&1

echo "=== bench/aws/loop_ab_nginx.sh ==="
echo "  destination:   $LOCAL_DEST"
echo "  instance type: $INSTANCE_TYPE"
echo "  profiles:      $PROFILES"
echo "  cycles:        $CYCLES (arms interleaved A B A B ...)"
echo "  workers:       BlackBull $WEB_WORKERS / nginx $NGINX_WORKERS"
echo "  load:          wrk -t$THREADS -c$CONNS, ${WARMUP}s warmup + ${DURATION}s measure"
echo "  local HEAD:    $(git -C "$REPO_ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)"
echo

# ---------------------------------------------------------------------------
# Step 1 — provision, with a teardown trap and an on-box backstop.
# ---------------------------------------------------------------------------
if [ "$SKIP_PROVISION" != "1" ]; then
    echo ">>> bench/aws/up.sh ..."
    bash "$(dirname "$0")/up.sh"
fi

_teardown() {
    local rc=$?
    if [ "$KEEP_INSTANCE" = "1" ]; then
        echo "KEEP_INSTANCE=1 — leaving EC2 alive; run 'bash bench/aws/down.sh'"
        return $rc
    fi
    echo ">>> bench/aws/down.sh (trap EXIT) ..."
    bash "$(dirname "$0")/down.sh" || echo "WARNING: down.sh failed — check the console"
    return $rc
}
trap _teardown EXIT

_bench_aws_load_state
SERVER_REMOTE="$SSH_USER@${SERVER_PUBLIC_IP:-${PUBLIC_IP:-}}"
REMOTE_REPO="/home/$SSH_USER/BlackBull"

# The EXIT trap dies with this shell.  Instances launch with
# shutdown-behavior=terminate, so an on-box timer is the backstop that
# survives a dropped SSH session or a killed driver.
echo ">>> arming on-box self-terminate backstop (+90 min) ..."
ssh "${SSH_OPTS[@]}" "$SERVER_REMOTE" "sudo shutdown -h +90" || true

# ---------------------------------------------------------------------------
# Step 2 — deploy + toolchain (bench/install.sh brings nginx and wrk).
# ---------------------------------------------------------------------------
echo ">>> bench/aws/install.sh ..."
bash "$(dirname "$0")/install.sh"

# ---------------------------------------------------------------------------
# Step 3 — run the interleaved A/B on the box.
#
# The whole measurement is one remote script so that arm switching, the
# loop-identity check and the keep-alive check all happen inside a single
# shell on the box — no per-step SSH round trips inside a timed window.
# ---------------------------------------------------------------------------
echo ">>> running $CYCLES interleaved A/B cycles ..."

# shellcheck disable=SC2087  # the heredoc is deliberately expanded locally
ssh "${SSH_OPTS[@]}" "$SERVER_REMOTE" "bash -s" <<REMOTE
set -uo pipefail
cd "$REMOTE_REPO"
source .venv/bin/activate

OUT=\$HOME/loop-ab-out
rm -rf "\$OUT"; mkdir -p "\$OUT"

echo "=== box ==="
{ lscpu; echo; uname -a; echo; nginx -v 2>&1; wrk --version 2>&1 | head -1;
  python -V; python -c 'import uvloop; print("uvloop", uvloop.__version__)'; } \
  >"\$OUT/provenance.txt" 2>&1
grep -E '^(Model name|Thread|Core|Socket|CPU\(s\))' "\$OUT/provenance.txt" || true

command -v fuser >/dev/null || sudo apt-get install -y -qq psmisc >/dev/null 2>&1

# --- nginx: the constant ---------------------------------------------------
# nginx_proxy.conf carries "daemon off;" because run_peer.sh foregrounds it.
# Here nginx must outlive the shell that starts it and stay identical across
# arms, so it is daemonised in the config rather than with -g (nginx rejects
# -g for a directive the config already sets).
sed -e "s|__BB_CERT__|$REMOTE_REPO/tests/cert.pem|" \
    -e "s|__BB_KEY__|$REMOTE_REPO/tests/key.pem|" \
    -e "s|__BB_UPSTREAM_PORT__|$UPSTREAM_PORT|" \
    -e "s|__BB_LISTEN_PORT__|$LISTEN_PORT|" \
    -e "s|^worker_processes .*|worker_processes $NGINX_WORKERS;|" \
    -e "s|^daemon off;|daemon on;|" \
    bench/peers/nginx_proxy.conf >"\$OUT/nginx.conf"

sudo fuser -k $LISTEN_PORT/tcp 2>/dev/null || true
sleep 1
sudo nginx -c "\$OUT/nginx.conf" 2>"\$OUT/nginx-start.err" || {
    echo "FATAL: nginx failed to start"; cat "\$OUT/nginx-start.err"; exit 1; }
echo "nginx up on :$LISTEN_PORT (worker_processes $NGINX_WORKERS)"

UP_PGID=""

stop_arm() {
    # setsid makes the upstream a process-group leader, so one signal to
    # -PGID takes the master and every forked worker.  Killing the master
    # alone would orphan the workers and leave :$UPSTREAM_PORT bound.
    [ -n "\$UP_PGID" ] && kill -TERM -"\$UP_PGID" 2>/dev/null
    UP_PGID=""
    for i in \$(seq 1 40); do
        sudo fuser -s $UPSTREAM_PORT/tcp 2>/dev/null || return 0
        sleep 0.25
    done
    sudo fuser -k $UPSTREAM_PORT/tcp 2>/dev/null || true
    sleep 1
}

start_arm() {   # \$1 = arm letter, \$2 = BB_UVLOOP value
    local arm="\$1" uv="\$2" i
    stop_arm
    # BB_WORKERS as well as --workers: the CLI flag reaches app.run(), but
    # /config reports the *settings* value, so without the env var the
    # provenance line would claim 1 worker while 3 were serving.
    BB_UVLOOP="\$uv" BB_WORKERS=$WEB_WORKERS setsid nohup python bench/app.py \
        --no-tls --port $UPSTREAM_PORT --workers $WEB_WORKERS \
        >>"\$OUT/upstream-\$arm.log" 2>&1 &
    UP_PGID=\$!
    # -f is load-bearing: without it curl exits 0 on nginx's own 502, so a
    # dead upstream reads as "ready" and the arm gets benchmarked against an
    # error page.  Probe the upstream directly first so a failure names the
    # layer that broke rather than just the proxy in front of it.
    for i in \$(seq 1 60); do
        curl -sf --max-time 1 "http://127.0.0.1:$UPSTREAM_PORT/ping" >/dev/null 2>&1 && break
        sleep 0.5
        if [ "\$i" = 60 ]; then
            echo "FATAL: arm \$arm upstream never bound :$UPSTREAM_PORT"
            tail -20 "\$OUT/upstream-\$arm.log"; return 1
        fi
    done
    for i in \$(seq 1 20); do
        curl -sfk --max-time 1 "https://127.0.0.1:$LISTEN_PORT/ping" >/dev/null 2>&1 && return 0
        sleep 0.5
    done
    echo "FATAL: arm \$arm upstream is up but nginx will not proxy to it"
    tail -20 /tmp/bench-nginx-proxy-error.log 2>/dev/null; return 1
}

# The failure this guards against is silent: when BB_UVLOOP=1 but uvloop
# cannot be imported, the server logs a warning and keeps the stock loop.
# The run then completes and produces a full, plausible set of numbers that
# read as "uvloop bought nothing".  /config echoes what the process actually
# installed, so ask it rather than trusting the env var we set.
check_loop() {  # \$1 = arm letter, \$2 = expected true|false
    local got
    got=\$(curl -sfk --max-time 5 "https://127.0.0.1:$LISTEN_PORT/config") || {
        echo "FATAL: arm \$1 — /config did not answer"; return 1; }
    echo "  /config: \$got"
    case "\$got" in
        *'"uvloop": '\$2*) : ;;
        *) echo "FATAL: arm \$1 wanted uvloop=\$2, /config disagrees"; return 1 ;;
    esac
    case "\$got" in
        *'"workers": $WEB_WORKERS'*) : ;;
        *) echo "FATAL: arm \$1 wanted $WEB_WORKERS workers, /config disagrees"; return 1 ;;
    esac
}

# nginx must reuse pooled upstream connections; if it does not, every
# request pays a TCP handshake and the run measures connection setup.
# TcpActiveOpens counts outbound connects box-wide — on an otherwise idle
# box that is nginx dialling the upstream.  Report it per request.
active_opens() { nstat -asz TcpActiveOpens 2>/dev/null | awk '/TcpActiveOpens/{print \$2}'; }

measure() {     # \$1 = arm, \$2 = cycle, \$3 = profile
    local arm="\$1" cyc="\$2" prof="\$3"
    local f="\$OUT/wrk-\$arm-\$prof-c\$cyc.txt"
    local before after reqs
    before=\$(active_opens)
    wrk -t$THREADS -c$CONNS -d${DURATION}s --latency \
        "https://127.0.0.1:$LISTEN_PORT/\$prof" >"\$f" 2>&1
    after=\$(active_opens)
    reqs=\$(awk '/requests in/{print \$1}' "\$f")
    echo "reconnects=\$((after - before)) requests=\${reqs:-0}" >>"\$f"
    printf '  %s/%s cycle %s: %s req/s (reconnects/req %s)\n' \
        "\$arm" "\$prof" "\$cyc" \
        "\$(awk '/Requests\/sec/{print \$2}' "\$f")" \
        "\$(awk -v a=\$((after - before)) -v r="\${reqs:-1}" 'BEGIN{printf "%.4f", a/r}')"
}

for cyc in \$(seq 1 $CYCLES); do
    for spec in "A 1 true" "B 0 false"; do
        set -- \$spec
        arm=\$1; uv=\$2; want=\$3
        echo "--- cycle \$cyc / arm \$arm (BB_UVLOOP=\$uv) ---"
        start_arm "\$arm" "\$uv" || exit 1
        check_loop "\$arm" "\$want" || exit 1
        wrk -t$THREADS -c$CONNS -d${WARMUP}s \
            "https://127.0.0.1:$LISTEN_PORT/ping" >/dev/null 2>&1
        for prof in $PROFILES; do
            measure "\$arm" "\$cyc" "\$prof"
        done
    done
done

stop_arm
sudo nginx -c "\$OUT/nginx.conf" -s quit 2>/dev/null || sudo fuser -k $LISTEN_PORT/tcp 2>/dev/null || true
echo "AB_DONE"
REMOTE
rc=$?
echo "remote A/B exit=$rc"
[ "$rc" -eq 0 ] || exit "$rc"

# ---------------------------------------------------------------------------
# Step 4 — pull the raw wrk output back and summarise.
# ---------------------------------------------------------------------------
echo ">>> fetching results ..."
rsync -e "ssh ${SSH_OPTS[*]}" -az "$SERVER_REMOTE:loop-ab-out/" "$LOCAL_DEST/raw/"

python3 "$REPO_ROOT/bench/aws/loop_ab_summary.py" "$LOCAL_DEST" \
    --instance "$INSTANCE_TYPE" --workers "$WEB_WORKERS" --conns "$CONNS"

echo "done; teardown via trap"
