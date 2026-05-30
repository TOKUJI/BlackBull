#!/usr/bin/env bash
# bench/aws/profile_lanes.sh — Sprint 27 Phase 1: wrk-driven per-lane
# py-spy capture on EC2 split topology.
#
# For each lane in $LANES (default "B1 B2 B3"), starts BlackBull on the
# server host under py-spy, drives the matching wrk workload from the
# loadgen host, then pulls the speedscope JSON + wrk output back.
#
# Methodology pin: re-profile on the SAME EC2 instance type used for
# the sprint cumulative measurement (c7i.xlarge server, c7i.2xlarge
# loadgen) — WSL2 profile shape diverges materially at high
# concurrency.  Sprint 21 Phase B finding.
#
# Prereqs:
#   - bench/aws/.state populated by up.sh (TOPO=split)
#   - install.sh has run (server has BlackBull + py-spy; loadgen has wrk)
#
# Usage:
#   TOPO=split bash bench/aws/up.sh
#   TOPO=split bash bench/aws/install.sh
#   bash bench/aws/profile_lanes.sh
#   TOPO=split bash bench/aws/down.sh
#
# Env knobs:
#   LANES         space-separated subset of {B1 B2 B3}  (default: all three)
#   DURATION      load-phase seconds per lane           (default: 60)
#   WARMUP        warmup seconds before measurement     (default: 15)
#   PROFILE_RATE  py-spy sampling rate in Hz            (default: 200)
#   BB_UVLOOP     event-loop posture (0 or 1)           (default: 0 — pure-Python identity)
#   BB_TLS        TLS posture (1 = HTTPS:8443; 0 = HTTP:8000, no TLS)
#                                                       (default: 1 — matches production
#                                                        wrk lanes; set 0 to isolate
#                                                        framework cost from TLS-stack
#                                                        cost in the typical
#                                                        nginx-fronted production
#                                                        topology where TLS is
#                                                        terminated by nginx).
#
# Output:
#   bench/results/aws/sprint27-phase1[-tls<n>]-<ts>/<lane>/profile.json   speedscope
#   bench/results/aws/sprint27-phase1[-tls<n>]-<ts>/<lane>/profile.log    py-spy stderr
#   bench/results/aws/sprint27-phase1[-tls<n>]-<ts>/<lane>/wrk.txt        wrk output
#
# Speedscope JSON viewable at https://www.speedscope.app — drag-and-drop.

set -euo pipefail

# shellcheck source=config.sh
source "$(dirname "$0")/config.sh"
_bench_aws_check_env
_bench_aws_load_state

if [ "${TOPO:-single}" != "split" ]; then
    echo "profile_lanes.sh requires TOPO=split (server + loadgen)" >&2
    exit 1
fi

LANES="${LANES:-B1 B2 B3}"
DURATION="${DURATION:-60}"
WARMUP="${WARMUP:-15}"
PROFILE_RATE="${PROFILE_RATE:-200}"
BB_UVLOOP="${BB_UVLOOP:-0}"
BB_TLS="${BB_TLS:-1}"

TS="$(date -u +%Y%m%d-%H%M%SZ)"
if [ "$BB_TLS" = "0" ]; then
    LOCAL_DEST="$REPO_ROOT/bench/results/aws/sprint27-phase1-tls0-$TS"
    PORT=8000
    SCHEME="http"
    SERVER_CLI_ARGS="--port $PORT --no-tls"
else
    LOCAL_DEST="$REPO_ROOT/bench/results/aws/sprint27-phase1-$TS"
    PORT=8443
    SCHEME="https"
    SERVER_CLI_ARGS="--port $PORT --cert tests/cert.pem --key tests/key.pem"
fi
mkdir -p "$LOCAL_DEST"

SERVER_REMOTE="$SSH_USER@$SERVER_PUBLIC_IP"
LOADGEN_REMOTE="$SSH_USER@$LOADGEN_PUBLIC_IP"
REMOTE_REPO="/home/$SSH_USER/BlackBull"
BASE="${SCHEME}://bench-server.internal:${PORT}"

echo "=== profile_lanes.sh ==="
echo "  destination: $LOCAL_DEST"
echo "  lanes:       $LANES"
echo "  duration:    ${DURATION}s + ${WARMUP}s warmup"
echo "  py-spy rate: ${PROFILE_RATE} Hz"
echo "  BB_UVLOOP:   $BB_UVLOOP"
echo "  BB_TLS:      $BB_TLS  ($SCHEME on port $PORT)"
echo

# Lane shapes mirror bench/wrk/run.sh exactly.
_lane_wrk_cmd() {
    local lane="$1"
    case "$lane" in
        B1) echo "wrk -t4 -c256 -d${DURATION}s ${BASE}/plaintext" ;;
        B2) echo "wrk -t4 -c1024 -d${DURATION}s -s ${REMOTE_REPO}/bench/wrk/pipeline.lua ${BASE}/plaintext -- 16" ;;
        B3) echo "wrk -t4 -c256 -d${DURATION}s ${BASE}/json" ;;
        *)  echo "unknown lane: $lane" >&2; return 1 ;;
    esac
}

_run_one_lane() {
    local lane="$1"
    local lane_dir="$LOCAL_DEST/$lane"
    mkdir -p "$lane_dir"

    # Total py-spy wall: 5s startup + WARMUP + DURATION + 10s wind-down.
    local profile_secs=$((5 + WARMUP + DURATION + 10))
    local wrk_cmd
    wrk_cmd=$(_lane_wrk_cmd "$lane")

    echo ">>> $lane: profile_secs=${profile_secs}s, wrk_cmd=$wrk_cmd"

    # 1. Kill any lingering BlackBull on the server.  Use the bracket
    #    trick so pkill -f doesn't self-match (Sprint 25 close-out
    #    lesson).
    ssh "${SSH_OPTS[@]}" "$SERVER_REMOTE" \
        "pkill -f '[b]ench/app.py' 2>/dev/null || true
         pkill -f '[p]y-spy record' 2>/dev/null || true
         sleep 1" || true

    # 2. Start the server through py-spy.  The `(...&)` subshell form is
    #    load-bearing: it backgrounds the chain INSIDE the subshell, then
    #    the subshell exits immediately — re-parenting the background
    #    process to init BEFORE the outer SSH bash exits.  Without the
    #    explicit subshell (e.g. plain `cmd & ` form), the backgrounded
    #    process remains a child of the SSH bash and dies via SIGHUP
    #    when SSH disconnects (Sprint 27 Phase 1 finding — cleartext-v1
    #    attempt without the subshell broke server startup on B1).
    #
    #    B2 (c=1024 + pipeline=16) wants `ulimit -n` raised, but applying
    #    `ulimit -n 65536 && ...` inside the `(...&)` subshell ALSO
    #    breaks server startup (cause unidentified).  The two together
    #    are incompatible; for now B2 is run without the ulimit bump
    #    (will hit EMFILE), and B2 profiling waits for a wrapper-script
    #    approach.  B1 + B3 cover the cascade-decision picture fully.
    ssh -n "${SSH_OPTS[@]}" "$SERVER_REMOTE" \
        "cd $REMOTE_REPO && source .venv/bin/activate &&
         (BB_WORKERS=1 BB_UVLOOP=$BB_UVLOOP BB_ACCESS_LOG=0 \
          nohup py-spy record \
             --duration $profile_secs \
             --rate $PROFILE_RATE \
             --format speedscope \
             --output /tmp/profile_${lane}.json \
             -- python bench/app.py $SERVER_CLI_ARGS \
             > /tmp/profile_${lane}.log 2>&1 &)" || {
        echo "  ssh-start failed for $lane" >&2
        return 1
    }

    # 3. Wait for the server to be ready (probe from loadgen via private DNS).
    local ready=0
    for i in $(seq 1 30); do
        if ssh "${SSH_OPTS[@]}" "$LOADGEN_REMOTE" \
            "curl -sk --max-time 2 ${BASE}/ping >/dev/null 2>&1"; then
            ready=1
            echo "  server ready (${i}s)"
            break
        fi
        sleep 1
    done
    if [ "$ready" = "0" ]; then
        echo "  ERROR: server not ready after 30s for $lane" >&2
        ssh "${SSH_OPTS[@]}" "$SERVER_REMOTE" \
            "cat /tmp/profile_${lane}.log 2>/dev/null | tail -20" >&2 || true
        return 1
    fi

    # 4. Warmup — discarded.  Settles wrk loadgen state + accept queue
    #    BEFORE the measured wrk pass, so the first lane in a run
    #    doesn't pay the cold-cache + connection-storm tax that lane-N
    #    runs don't see (Sprint 27 Phase 1 finding: B3-first throughput
    #    was ~21 % lower than B3-last on the same code).
    echo "  warmup (${WARMUP}s) ..."
    ssh "${SSH_OPTS[@]}" "$LOADGEN_REMOTE" \
        "wrk -t4 -c64 -d${WARMUP}s ${BASE}/plaintext > /dev/null 2>&1 || true"

    # 5. Measurement wrk — this is what py-spy is recording.
    echo "  load phase (${DURATION}s) ..."
    ssh "${SSH_OPTS[@]}" "$LOADGEN_REMOTE" \
        "$wrk_cmd > /tmp/wrk_${lane}.txt 2>&1 || true"

    # 6. Wait for py-spy to finish writing its JSON (its --duration
    #    timer is what gates this; the server exits when py-spy SIGTERMs it).
    echo "  waiting for py-spy to finalise ..."
    local waited=0
    while ssh "${SSH_OPTS[@]}" "$SERVER_REMOTE" \
            "pgrep -f '[p]y-spy record' >/dev/null"; do
        sleep 2
        waited=$((waited + 2))
        if [ "$waited" -gt 120 ]; then
            echo "  WARN: py-spy still running after 120s; forcing stop" >&2
            ssh "${SSH_OPTS[@]}" "$SERVER_REMOTE" \
                "pkill -f '[p]y-spy record' 2>/dev/null || true"
            sleep 3
            break
        fi
    done

    # 7. Belt + braces: stop any straggler bench/app.py.
    ssh "${SSH_OPTS[@]}" "$SERVER_REMOTE" \
        "pkill -f '[b]ench/app.py' 2>/dev/null || true" || true

    # 8. Pull artefacts.
    scp "${SSH_OPTS[@]}" "$SERVER_REMOTE:/tmp/profile_${lane}.json" \
        "$lane_dir/profile.json" 2>/dev/null || \
        echo "  WARN: speedscope JSON missing for $lane" >&2
    scp "${SSH_OPTS[@]}" "$SERVER_REMOTE:/tmp/profile_${lane}.log" \
        "$lane_dir/profile.log" 2>/dev/null || true
    scp "${SSH_OPTS[@]}" "$LOADGEN_REMOTE:/tmp/wrk_${lane}.txt" \
        "$lane_dir/wrk.txt" 2>/dev/null || \
        echo "  WARN: wrk output missing for $lane" >&2

    echo "  <<< $lane done: artefacts at $lane_dir"
    echo
}

for lane in $LANES; do
    _run_one_lane "$lane"
done

echo "=== all lanes complete ==="
echo "Artefacts at: $LOCAL_DEST"
echo
echo "View flamegraphs:"
echo "  Drag-and-drop each profile.json into https://www.speedscope.app"
echo "  Use 'Left Heavy' view for top-N self-time stack frames"
echo
echo "Don't forget: TOPO=split bash bench/aws/down.sh"
