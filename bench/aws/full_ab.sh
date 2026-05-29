#!/usr/bin/env bash
# bench/aws/full_ab.sh — cross-instance paired-A/B harness with
# control server, CPU/memory monitoring, and unconditional cleanup.
#
# Provisions M independent (server + loadgen) pairs in parallel,
# runs the per-pair A/B matrix on each (uvicorn-pre → bb-base-u0 →
# bb-trt-u0 → bb-base-u1 → bb-trt-u1 → uvicorn-post) with
# mpstat / vmstat capture, aggregates cross-pair statistics, then
# terminates every instance.
#
# Why M parallel pairs: same-instance A/B cancels random within-run
# noise but does not cancel time-varying neighbour drift on the host
# (Sprint 25 Phase A finding).  Cross-pair MEDIAN delta + IQR
# estimates the systematic floor.
#
# Why uvicorn bookends: a control whose code does NOT change between
# baseline and treatment.  If uvicorn drifts between pre and post,
# the pair's host moved; that drift bounds how much of any BlackBull
# delta could be attributable to host state.
#
# Safety:
#   * trap EXIT INT TERM HUP unconditionally calls _force_down_all,
#     which terminates instances both by per-pair .state file AND by
#     project tag sweep.
#   * a global wall-clock timeout (WALL_CLOCK_BUDGET, default 6 h)
#     wraps the orchestration; on hit, the trap fires.
#   * per-pair SSH health probes detect prolonged unresponsiveness;
#     dead pairs are aborted and torn down individually while
#     surviving pairs continue.
#
# Usage:
#   bash bench/aws/full_ab.sh                 # M=3, defaults
#   M=2 bash bench/aws/full_ab.sh
#   LANES="B-wrk" DURATION=30 RUNS_WRK=2 bash bench/aws/full_ab.sh
#
# Env knobs:
#   M                       Number of parallel pairs (default 3)
#   STACKS                  Always implicitly includes blackbull + uvicorn;
#                           reserved for future expansion (ignored for now)
#   DURATION                wrk per-scenario seconds (default 60)
#   RUNS_WRK                wrk repeats per scenario (default 3)
#   WALL_CLOCK_BUDGET       Total budget in seconds (default 21600 = 6 h)
#   HEALTH_TIMEOUT          Per-pair SSH unresponsiveness threshold (default 600 = 10 min)
#   REGION                  AWS region (default us-east-1, via config.sh)
#   OUT_ROOT                Local output root (default bench/results/aws/full_ab-<ts>)

set -euo pipefail

# ----- configuration -----
M="${M:-3}"
DURATION="${DURATION:-60}"
RUNS_WRK="${RUNS_WRK:-3}"
WALL_CLOCK_BUDGET="${WALL_CLOCK_BUDGET:-21600}"
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-600}"
LANES="${LANES:-B-wrk}"

AWS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$AWS_DIR/../.." && pwd)"
TS="$(date -u +%Y%m%d-%H%M%SZ)"
OUT_ROOT="${OUT_ROOT:-$REPO_ROOT/bench/results/aws/full_ab-$TS}"
WORK_ROOT="$AWS_DIR/multi"
mkdir -p "$OUT_ROOT" "$WORK_ROOT"

# Shared config — for tag-scan teardown
source "$AWS_DIR/config.sh"

PAIR_IDS=()
PAIR_PIDS=()
START_EPOCH=$(date +%s)

# ----- logging helpers -----
log() { printf '%s [full_ab] %s\n' "$(date -u +%H:%M:%S)" "$*" | tee -a "$OUT_ROOT/orchestrator.log"; }
warn() { log "WARN: $*"; }
die() { log "FATAL: $*"; exit 1; }

# ----- cleanup -----
_force_down_all() {
    local rc=$?
    # Disable the trap once we're inside it so SIGINT during cleanup
    # doesn't re-enter and double-terminate.
    trap - EXIT INT TERM HUP
    # Kill the watchdog subshell so it can't fire a spurious WATCHDOG
    # line after the main script has finished cleaning up.
    if [ -n "${WATCHDOG_PID:-}" ]; then
        kill "$WATCHDOG_PID" 2>/dev/null || true
    fi
    log "=== cleanup starting (exit=$rc, elapsed=$(( $(date +%s) - START_EPOCH ))s) ==="

    # Per-pair down.sh (uses each pair's .state).
    for pid in "${PAIR_IDS[@]}"; do
        local state="$WORK_ROOT/pair-$pid/.state"
        if [ -f "$state" ]; then
            log "cleanup: down.sh for pair $pid"
            STATE_FILE="$state" \
            KEY_NAME="blackbull-bench-key-pair${pid}" \
            SG_NAME="blackbull-bench-sg-pair${pid}" \
            PLACEMENT_GROUP_NAME="blackbull-bench-cpg-pair${pid}" \
            timeout 180 bash "$AWS_DIR/down.sh" \
                >> "$OUT_ROOT/teardown-pair-${pid}.log" 2>&1 || \
                warn "down.sh pair $pid failed; tag-scan will pick up orphans"
        fi
    done

    # Belt-and-braces: tag-scan sweep for any instances we missed.
    # This catches the case where the per-pair state file went missing
    # (e.g. orchestrator crashed before writing it) but the instances
    # exist in AWS with the project tag.
    _tag_scan_terminate

    log "=== cleanup complete (preserving exit code $rc) ==="
    exit "$rc"
}

_tag_scan_terminate() {
    log "tag-scan: looking for orphaned BlackBull-bench instances..."
    local ids
    ids=$(aws ec2 describe-instances --region "$REGION" \
        --filters "Name=tag:Project,Values=BlackBull-bench" \
                  "Name=instance-state-name,Values=pending,running,stopping,stopped" \
        --query 'Reservations[].Instances[].InstanceId' \
        --output text 2>/dev/null | tr '\t' ' ' | xargs || echo)
    if [ -n "$ids" ]; then
        log "tag-scan: terminating orphans: $ids"
        aws ec2 terminate-instances --region "$REGION" --instance-ids $ids \
            >> "$OUT_ROOT/tagscan-terminate.log" 2>&1 || warn "tag-scan terminate failed"
        # Best-effort wait — don't block teardown forever
        timeout 300 aws ec2 wait instance-terminated --region "$REGION" --instance-ids $ids \
            >> "$OUT_ROOT/tagscan-terminate.log" 2>&1 || \
            warn "tag-scan wait timed out; instances may still be terminating"
    fi

    # Best-effort cleanup of per-pair SG / key / PG.  Each is namespaced
    # by pair id and idempotent (delete returns success if gone).
    for pid in "${PAIR_IDS[@]}"; do
        aws ec2 delete-security-group --region "$REGION" \
            --group-name "blackbull-bench-sg-pair${pid}" >/dev/null 2>&1 || true
        aws ec2 delete-key-pair --region "$REGION" \
            --key-name "blackbull-bench-key-pair${pid}" >/dev/null 2>&1 || true
        aws ec2 delete-placement-group --region "$REGION" \
            --group-name "blackbull-bench-cpg-pair${pid}" >/dev/null 2>&1 || true
        rm -f "$AWS_DIR/blackbull-bench-key-pair${pid}.pem"
    done

    # Final verification
    local remaining
    remaining=$(aws ec2 describe-instances --region "$REGION" \
        --filters "Name=tag:Project,Values=BlackBull-bench" \
                  "Name=instance-state-name,Values=pending,running,stopping,stopped" \
        --query 'Reservations[].Instances[].InstanceId' \
        --output text 2>/dev/null | tr '\t' ' ' | xargs || echo)
    if [ -n "$remaining" ]; then
        warn "post-cleanup: $remaining still present; manual termination needed"
    else
        log "post-cleanup: no BlackBull-bench instances remain"
    fi
}

trap _force_down_all EXIT INT TERM HUP

# ----- wall-clock budget enforcer (background watchdog) -----
(
    sleep "$WALL_CLOCK_BUDGET"
    log "WATCHDOG: wall-clock budget ${WALL_CLOCK_BUDGET}s exceeded; killing orchestrator"
    kill -TERM $$ 2>/dev/null || true
) &
WATCHDOG_PID=$!
disown $WATCHDOG_PID

# ----- snapshot baseline vs treatment file states -----
# Treatment = current working tree (the modifications under test).
# Baseline  = git HEAD bytes for the same files.
log "snapshotting baseline vs treatment file contents"
SNAP_BASE="$OUT_ROOT/snap-baseline"
SNAP_TRT="$OUT_ROOT/snap-treatment"
mkdir -p "$SNAP_BASE" "$SNAP_TRT"

TREATMENT_FILES=$(cd "$REPO_ROOT" && git diff --name-only -- 'blackbull/' 'tests/' || echo)
if [ -z "$TREATMENT_FILES" ]; then
    die "no modified files under blackbull/ or tests/ — nothing to A/B"
fi
log "treatment files: $TREATMENT_FILES"
for f in $TREATMENT_FILES; do
    mkdir -p "$SNAP_BASE/$(dirname "$f")" "$SNAP_TRT/$(dirname "$f")"
    (cd "$REPO_ROOT" && git show "HEAD:$f" > "$SNAP_BASE/$f")
    cp "$REPO_ROOT/$f" "$SNAP_TRT/$f"
done

# Temporarily revert to baseline for the install.sh rsync.
# We'll restore the treatment files later, but the initial deploy
# is BASELINE.
log "checking out baseline file contents for initial deploy"
( cd "$REPO_ROOT" && git checkout -- $TREATMENT_FILES )

# ----- helper: drive a pair through up.sh + install.sh -----
_provision_pair() {
    local i="$1"
    local pdir="$WORK_ROOT/pair-$i"
    mkdir -p "$pdir"

    log "pair $i: up.sh"
    TOPO=split \
    STATE_FILE="$pdir/.state" \
    KEY_NAME="blackbull-bench-key-pair${i}" \
    SG_NAME="blackbull-bench-sg-pair${i}" \
    PLACEMENT_GROUP_NAME="blackbull-bench-cpg-pair${i}" \
    timeout 600 bash "$AWS_DIR/up.sh" \
        > "$pdir/up.log" 2>&1 || { warn "pair $i: up.sh failed"; return 1; }

    log "pair $i: install.sh (deploys BASELINE since treatment files reverted)"
    TOPO=split \
    STATE_FILE="$pdir/.state" \
    KEY_NAME="blackbull-bench-key-pair${i}" \
    SG_NAME="blackbull-bench-sg-pair${i}" \
    PLACEMENT_GROUP_NAME="blackbull-bench-cpg-pair${i}" \
    timeout 900 bash "$AWS_DIR/install.sh" \
        > "$pdir/install.log" 2>&1 || { warn "pair $i: install.sh failed"; return 1; }

    log "pair $i: provisioned"
}

# ----- provision all M pairs in parallel -----
log "=== provisioning $M pairs in parallel ==="
PROV_PIDS=()
for i in $(seq 1 "$M"); do
    PAIR_IDS+=("$i")
    _provision_pair "$i" &
    PROV_PIDS+=($!)
done
PROV_FAILED=()
for idx in "${!PROV_PIDS[@]}"; do
    if ! wait "${PROV_PIDS[$idx]}"; then
        PROV_FAILED+=("$((idx + 1))")
    fi
done
SURVIVING_PAIRS=()
for i in "${PAIR_IDS[@]}"; do
    skip=0
    for f in "${PROV_FAILED[@]:-}"; do
        if [ "$i" = "$f" ]; then skip=1; break; fi
    done
    [ "$skip" = "0" ] && SURVIVING_PAIRS+=("$i")
done
if [ "${#SURVIVING_PAIRS[@]}" -eq 0 ]; then
    die "no pairs survived provisioning; aborting"
fi
log "provisioning complete: surviving pairs = ${SURVIVING_PAIRS[*]}; failed = ${PROV_FAILED[*]:-none}"

# ----- restore treatment files now that all installs landed -----
# install.sh has finished its rsync.  The local working tree can now
# go back to the treatment state without affecting the deployed code.
log "restoring local treatment files (server still has baseline)"
for f in $TREATMENT_FILES; do
    cp "$SNAP_TRT/$f" "$REPO_ROOT/$f"
done

# ----- per-pair bench (parallel across pairs) -----
log "=== starting per-pair bench sequence in parallel ==="
BENCH_PIDS=()
for i in "${SURVIVING_PAIRS[@]}"; do
    PAIR_ID="$i" \
    SNAP_BASE="$SNAP_BASE" \
    SNAP_TRT="$SNAP_TRT" \
    OUT_ROOT="$OUT_ROOT" \
    WORK_ROOT="$WORK_ROOT" \
    DURATION="$DURATION" \
    RUNS_WRK="$RUNS_WRK" \
    HEALTH_TIMEOUT="$HEALTH_TIMEOUT" \
    bash "$AWS_DIR/_pair_bench.sh" &
    BENCH_PIDS+=($!)
done
BENCH_FAILED=()
for idx in "${!BENCH_PIDS[@]}"; do
    if ! wait "${BENCH_PIDS[$idx]}"; then
        BENCH_FAILED+=("${SURVIVING_PAIRS[$idx]}")
    fi
done
log "bench complete; failed pairs = ${BENCH_FAILED[*]:-none}"

# ----- aggregate across pairs -----
log "=== aggregating cross-pair results ==="
python3 "$AWS_DIR/_aggregate_ab.py" \
    --root "$OUT_ROOT" \
    --pairs "${SURVIVING_PAIRS[*]}" \
    > "$OUT_ROOT/aggregate.md" 2>&1 || warn "aggregate failed"
cat "$OUT_ROOT/aggregate.md" || true

# Trap will fire and tear down.
log "=== full_ab.sh main path complete (cleanup runs via trap) ==="
