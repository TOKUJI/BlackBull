#!/usr/bin/env bash
# bench/aws/_pair_bench.sh — per-pair bench sequence for full_ab.sh.
#
# Drives one (server, loadgen) pair through six wrk runs in order:
#
#   1. uvicorn-pre        (control point 1 — host state before A/B)
#   2. blackbull-base-u0  BB_UVLOOP=0 with HEAD code
#   3. blackbull-trt-u0   BB_UVLOOP=0 with treatment files rsync'd in
#   4. blackbull-base-u1  BB_UVLOOP=1 with HEAD code (re-rsync to revert)
#   5. blackbull-trt-u1   BB_UVLOOP=1 with treatment files
#   6. uvicorn-post       (control point 2 — host state after A/B)
#
# Within each run, mpstat -P ALL 1 + vmstat 1 are captured on both
# the server and the loadgen for the duration of the wrk lane.  All
# raw output is pulled back to $OUT_ROOT/pair-<id>/<phase>/.
#
# Env (passed from full_ab.sh):
#   PAIR_ID, SNAP_BASE, SNAP_TRT, OUT_ROOT, WORK_ROOT,
#   DURATION, RUNS_WRK, HEALTH_TIMEOUT

set -euo pipefail

PAIR_ID="${PAIR_ID:?required}"
PDIR="$WORK_ROOT/pair-$PAIR_ID"
OUT_DIR="$OUT_ROOT/pair-$PAIR_ID"
mkdir -p "$OUT_DIR"

AWS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Load pair state
STATE_FILE="$PDIR/.state"
source "$AWS_DIR/config.sh"
KEY_NAME="blackbull-bench-key-pair${PAIR_ID}"
LOCAL_KEY="$AWS_DIR/${KEY_NAME}.pem"
source "$STATE_FILE"

REMOTE_REPO=/home/ubuntu/BlackBull
SSH_OPTS_LOCAL=(
    -i "$LOCAL_KEY"
    -o StrictHostKeyChecking=no
    -o UserKnownHostsFile=/dev/null
    -o LogLevel=ERROR
    -o ConnectTimeout=10
    -o ServerAliveInterval=30
    -o ServerAliveCountMax=3
)
SERVER_REMOTE="ubuntu@$SERVER_PUBLIC_IP"
LOADGEN_REMOTE="ubuntu@$LOADGEN_PUBLIC_IP"

plog() { printf '%s [pair-%s] %s\n' "$(date -u +%H:%M:%S)" "$PAIR_ID" "$*" | tee -a "$OUT_DIR/pair.log"; }

# ----- health probe -----
_health_ok() {
    local host="$1"
    timeout 30 ssh "${SSH_OPTS_LOCAL[@]}" "$host" "true" 2>/dev/null
}

_require_health() {
    local last_ok=$(date +%s)
    while true; do
        if _health_ok "$SERVER_REMOTE" && _health_ok "$LOADGEN_REMOTE"; then
            last_ok=$(date +%s)
            return 0
        fi
        local elapsed=$(( $(date +%s) - last_ok ))
        if [ "$elapsed" -gt "$HEALTH_TIMEOUT" ]; then
            plog "FATAL: pair unresponsive for ${elapsed}s; aborting this pair"
            return 1
        fi
        sleep 10
    done
}

# Verify pair healthy before kicking off the long bench sequence.
plog "health probe before bench start"
_require_health || exit 1

# ----- mpstat + vmstat capture helpers -----
_start_metrics() {
    local phase="$1"
    local pdir="$OUT_DIR/$phase"
    mkdir -p "$pdir"
    # Start mpstat / vmstat on both ends; record PIDs to stop later.
    # Both are launched via nohup + disown so the SSH session can exit.
    ssh "${SSH_OPTS_LOCAL[@]}" "$SERVER_REMOTE" \
        "rm -f /tmp/mpstat.log /tmp/vmstat.log
         (nohup mpstat -P ALL 1 > /tmp/mpstat.log 2>&1 < /dev/null & echo \$! > /tmp/mpstat.pid)
         (nohup vmstat 1       > /tmp/vmstat.log 2>&1 < /dev/null & echo \$! > /tmp/vmstat.pid)" \
        2>&1 | tee -a "$pdir/metrics_start.log" >/dev/null
    ssh "${SSH_OPTS_LOCAL[@]}" "$LOADGEN_REMOTE" \
        "rm -f /tmp/mpstat.log /tmp/vmstat.log
         (nohup mpstat -P ALL 1 > /tmp/mpstat.log 2>&1 < /dev/null & echo \$! > /tmp/mpstat.pid)
         (nohup vmstat 1       > /tmp/vmstat.log 2>&1 < /dev/null & echo \$! > /tmp/vmstat.pid)" \
        2>&1 | tee -a "$pdir/metrics_start.log" >/dev/null
}

_stop_and_pull_metrics() {
    local phase="$1"
    local pdir="$OUT_DIR/$phase"
    for host in "$SERVER_REMOTE" "$LOADGEN_REMOTE"; do
        ssh "${SSH_OPTS_LOCAL[@]}" "$host" \
            "kill \$(cat /tmp/mpstat.pid 2>/dev/null) 2>/dev/null || true
             kill \$(cat /tmp/vmstat.pid 2>/dev/null) 2>/dev/null || true
             rm -f /tmp/mpstat.pid /tmp/vmstat.pid" 2>/dev/null || true
    done
    # Pull logs
    scp "${SSH_OPTS_LOCAL[@]}" "$SERVER_REMOTE:/tmp/mpstat.log" "$pdir/server_mpstat.log" 2>/dev/null || true
    scp "${SSH_OPTS_LOCAL[@]}" "$SERVER_REMOTE:/tmp/vmstat.log" "$pdir/server_vmstat.log" 2>/dev/null || true
    scp "${SSH_OPTS_LOCAL[@]}" "$LOADGEN_REMOTE:/tmp/mpstat.log" "$pdir/loadgen_mpstat.log" 2>/dev/null || true
    scp "${SSH_OPTS_LOCAL[@]}" "$LOADGEN_REMOTE:/tmp/vmstat.log" "$pdir/loadgen_vmstat.log" 2>/dev/null || true
}

# ----- server lifecycle -----
#
# Bug history (Sprint 25 cumulative EC2 run, 2026-05-29):
# the original `pkill -f 'uvicorn'` matched its own parent bash shell
# on the remote side — because SSH passes the literal command string
# as the bash argv, so `bash -c "...pkill -f 'uvicorn'..."` has
# "uvicorn" in its own argv.  pkill's self-exclusion only covers its
# OWN PID, not the parent.  Result: the bash got SIGTERM'd before
# pkill ran a second time → uvicorn survived → BlackBull's bind
# silently failed → wrk measured uvicorn through every "blackbull"
# phase.  Fix: bracket-class pattern (`[u]vicorn`).  The regex
# matches "uvicorn" but the argv literal `[u]vicorn` does NOT match
# the regex (because the regex requires the literal `u`, but the
# argv has `[` before the `u`).
_stop_servers() {
    ssh "${SSH_OPTS_LOCAL[@]}" "$SERVER_REMOTE" \
        "pkill -f '[b]ench/app.py' 2>/dev/null || true
         pkill -f '[u]vicorn' 2>/dev/null || true
         # Poll until port 8443 is actually free, up to 5 s.  The
         # previous fixed sleep 1 wasn't enough during uvicorn's
         # graceful shutdown — the listening socket can linger.
         for _ in 1 2 3 4 5; do
             ss -tln 2>/dev/null | grep -q ':8443 ' || break
             sleep 1
         done
         # Escalation: if still bound after 5 s, SIGKILL anything on 8443.
         if ss -tln 2>/dev/null | grep -q ':8443 '; then
             sudo fuser -k -KILL 8443/tcp 2>/dev/null || true
             sleep 1
         fi" 2>/dev/null || true
}

# Verify the server actually running on 8443 matches the kind we
# expect.  Returns 0 if the stack signature matches, 1 otherwise.
# Distinguishes blackbull (bench/app.py — has /config returning JSON
# with `"uvloop"` field) from uvicorn (no /config — 404 body
# `not found` from bench.peers.asgi_app.py:98).
_assert_server_kind() {
    local expected="$1"   # 'blackbull' | 'uvicorn'
    local body
    body=$(ssh "${SSH_OPTS_LOCAL[@]}" "$LOADGEN_REMOTE" \
        "curl -sk --max-time 2 https://bench-server.internal:8443/config" 2>/dev/null)
    case "$expected" in
        blackbull)
            if [[ "$body" == *'"uvloop"'* ]]; then
                return 0
            fi
            plog "ASSERT FAIL: expected blackbull, /config body was: ${body:0:80}"
            return 1
            ;;
        uvicorn)
            if [[ "$body" == "not found" ]]; then
                return 0
            fi
            plog "ASSERT FAIL: expected uvicorn, /config body was: ${body:0:80}"
            return 1
            ;;
    esac
    return 1
}

_start_blackbull() {
    local uvloop="$1"
    ssh "${SSH_OPTS_LOCAL[@]}" "$SERVER_REMOTE" \
        "cd $REMOTE_REPO && source .venv/bin/activate &&
         (BB_UVLOOP=$uvloop BB_WORKERS=1 BB_ACCESS_LOG=0 \
          nohup python bench/app.py --port 8443 \
            --cert tests/cert.pem --key tests/key.pem \
            > /tmp/bb.log 2>&1 < /dev/null &)
         sleep 4
         ss -tln | grep -q 8443 && echo OK" >/dev/null
}

_start_uvicorn() {
    ssh "${SSH_OPTS_LOCAL[@]}" "$SERVER_REMOTE" \
        "cd $REMOTE_REPO && source .venv/bin/activate &&
         (nohup .venv/bin/uvicorn bench.peers.asgi_app:app \
            --host 0.0.0.0 --port 8443 \
            --ssl-certfile tests/cert.pem --ssl-keyfile tests/key.pem \
            --workers 1 --no-access-log \
            > /tmp/uvi.log 2>&1 < /dev/null &)
         sleep 4
         ss -tln | grep -q 8443 && echo OK" >/dev/null
}

_wait_ready() {
    for i in $(seq 1 30); do
        if ssh "${SSH_OPTS_LOCAL[@]}" "$LOADGEN_REMOTE" \
            "curl -sk --max-time 2 https://bench-server.internal:8443/ping >/dev/null" 2>/dev/null; then
            return 0
        fi
        sleep 1
    done
    return 1
}

# ----- one phase: start server, capture metrics, run wrk lane, stop server -----
_run_phase() {
    local phase="$1"      # e.g. uvicorn-pre, blackbull-base-u0, ...
    local kind="$2"       # 'uvicorn' | 'blackbull'
    local uvloop="${3:-0}"

    plog ">>> phase $phase (kind=$kind, uvloop=$uvloop)"
    local pdir="$OUT_DIR/$phase"
    mkdir -p "$pdir"

    _stop_servers
    case "$kind" in
        uvicorn)   _start_uvicorn ;;
        blackbull) _start_blackbull "$uvloop" ;;
        *) plog "unknown kind $kind"; return 1 ;;
    esac

    if ! _wait_ready; then
        plog "ERROR: server not ready for $phase"
        # Capture server stdout/stderr for forensics.
        scp "${SSH_OPTS_LOCAL[@]}" "$SERVER_REMOTE:/tmp/bb.log" "$pdir/server_bb.log" 2>/dev/null || true
        scp "${SSH_OPTS_LOCAL[@]}" "$SERVER_REMOTE:/tmp/uvi.log" "$pdir/server_uvi.log" 2>/dev/null || true
        return 1
    fi

    # Capture /config for the record + always pull server logs for forensics.
    ssh "${SSH_OPTS_LOCAL[@]}" "$LOADGEN_REMOTE" \
        "curl -sk https://bench-server.internal:8443/config 2>&1" \
        > "$pdir/config.txt" || true
    scp "${SSH_OPTS_LOCAL[@]}" "$SERVER_REMOTE:/tmp/bb.log" "$pdir/server_bb.log" 2>/dev/null || true
    scp "${SSH_OPTS_LOCAL[@]}" "$SERVER_REMOTE:/tmp/uvi.log" "$pdir/server_uvi.log" 2>/dev/null || true

    # Identity check: refuse to run wrk if the wrong stack is serving.
    # This catches the failure mode where _stop_servers self-killed
    # mid-way and the prior server kept running on 8443.
    if ! _assert_server_kind "$kind"; then
        plog "ERROR: server-identity check failed for $phase — aborting this phase"
        return 1
    fi

    _start_metrics "$phase"

    ssh "${SSH_OPTS_LOCAL[@]}" "$LOADGEN_REMOTE" \
        "rm -rf /tmp/wrk_$phase
         cd $REMOTE_REPO && \
         BASE=https://bench-server.internal:8443 \
         OUTDIR=/tmp/wrk_$phase \
         LABEL_PREFIX=${phase}_ \
         RUNS_WRK=$RUNS_WRK \
         DURATION=$DURATION \
         bash bench/wrk/run.sh" \
        > "$pdir/wrk.md" 2>&1 || plog "wrk lane returned non-zero for $phase"

    _stop_and_pull_metrics "$phase"
    _stop_servers

    plog "<<< phase $phase complete; rows:"
    grep -E '^\| [BE][0-9]' "$pdir/wrk.md" | sed 's/^/      /' | tee -a "$OUT_DIR/pair.log"
}

# ----- rsync helper for swapping baseline / treatment files -----
_deploy_snapshot() {
    local snap_dir="$1"   # SNAP_BASE or SNAP_TRT
    plog "deploying file snapshot from $(basename "$snap_dir")"
    # Walk the snap dir, rsync each file to the server preserving repo paths.
    (cd "$snap_dir" && find . -type f) | while read -r rel; do
        rel="${rel#./}"
        rsync -a -e "ssh ${SSH_OPTS_LOCAL[*]}" \
            "$snap_dir/$rel" \
            "$SERVER_REMOTE:$REMOTE_REPO/$rel" \
            >> "$OUT_DIR/rsync.log" 2>&1 || plog "rsync $rel failed"
    done
}

# ===== A/B sequence =====
# State at entry: server has BASELINE code (HEAD bytes) from install.sh.

_run_phase uvicorn-pre uvicorn

_run_phase blackbull-base-u0 blackbull 0

_deploy_snapshot "$SNAP_TRT"
_run_phase blackbull-trt-u0 blackbull 0

_deploy_snapshot "$SNAP_BASE"
_run_phase blackbull-base-u1 blackbull 1

_deploy_snapshot "$SNAP_TRT"
_run_phase blackbull-trt-u1 blackbull 1

_run_phase uvicorn-post uvicorn

plog "=== pair $PAIR_ID complete ==="
