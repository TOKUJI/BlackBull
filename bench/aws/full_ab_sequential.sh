#!/usr/bin/env bash
# bench/aws/full_ab_sequential.sh — sequential M-pair wrapper for full_ab.sh.
#
# Why: AWS account vCPU limit (32 in the default bucket) caps parallel
# M to 2 for c7i.xlarge + c7i.2xlarge pairs (12 vCPU each, 36 total at
# M=3).  Running pairs sequentially stays within 12 vCPU at any moment
# while preserving cross-instance variance estimation (in fact
# strengthening it — sequential pairs sample different time windows on
# the host, so neighbour-drift contamination is sampled too).
#
# Trade-off: ~M× wall time vs parallel mode.
#
# Usage:
#   N_PAIRS=3 BASE_REF=2cd20f5 bash bench/aws/full_ab_sequential.sh
#
# Env knobs passed through to full_ab.sh:
#   BASE_REF, DURATION, RUNS_WRK, LANES, HEALTH_TIMEOUT, REGION
#   N_PAIRS                  Number of sequential pairs (default 3)
#   OUT_ROOT_AGG             Final merged output dir (default
#                            bench/results/aws/full_ab_sequential-<ts>)

set -euo pipefail

N_PAIRS="${N_PAIRS:-3}"
AWS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$AWS_DIR/../.." && pwd)"
TS="$(date -u +%Y%m%d-%H%M%SZ)"
OUT_ROOT_AGG="${OUT_ROOT_AGG:-$REPO_ROOT/bench/results/aws/full_ab_sequential-$TS}"
mkdir -p "$OUT_ROOT_AGG"

log() {
    printf '%s [seq] %s\n' "$(date -u +%H:%M:%S)" "$*" | tee -a "$OUT_ROOT_AGG/sequential.log"
}

log "=== sequential run: N_PAIRS=$N_PAIRS, aggregated at $OUT_ROOT_AGG ==="
log "env: BASE_REF=${BASE_REF:-HEAD} DURATION=${DURATION:-60} RUNS_WRK=${RUNS_WRK:-3} LANES=${LANES:-B-wrk}"

PAIR_SUCCESS=()
PAIR_FAIL=()

for n in $(seq 1 "$N_PAIRS"); do
    sub_root="$OUT_ROOT_AGG/run-$n"
    log "--- pair $n / $N_PAIRS ---"

    # Each iteration is its own full_ab.sh M=1 run with a fresh OUT_ROOT.
    # That gives us per-pair snap-baseline/snap-treatment dirs (identical
    # across runs in BASE_REF mode, but harmless to recompute) and a
    # $sub_root/pair-1/ subdir containing the per-phase wrk.md results
    # we'll re-index as pair-$n in the aggregated tree.
    if M=1 OUT_ROOT="$sub_root" bash "$AWS_DIR/full_ab.sh" \
            >> "$OUT_ROOT_AGG/run-$n.stdout.log" 2>&1; then
        log "pair $n: full_ab.sh M=1 succeeded"
        PAIR_SUCCESS+=("$n")
    else
        rc=$?
        log "pair $n: full_ab.sh M=1 FAILED (exit $rc) — see run-$n.stdout.log"
        # Preserve provisioning artifacts (up.log / install.log /
        # .state) BEFORE the next iteration's full_ab.sh clobbers
        # bench/aws/multi/pair-1/.  Without this the forensic trail
        # for transient up.sh / install.sh failures is lost (Sprint 26
        # close-out finding).
        if [ -d "$AWS_DIR/multi/pair-1" ]; then
            mv "$AWS_DIR/multi/pair-1" "$OUT_ROOT_AGG/run-$n-multi-debug" \
                && log "preserved failed-pair artifacts at run-$n-multi-debug/"
        fi
        PAIR_FAIL+=("$n")
        continue
    fi

    # Copy the per-phase wrk.md results from this run's OUT_ROOT into
    # the aggregated tree.  $sub_root/pair-1/ is the one full_ab.sh writes
    # to (under OUT_ROOT); bench/aws/multi/pair-1/ holds only the
    # provisioning logs.  We capture both: the per-phase results under
    # pair-$n/ (for _aggregate_ab.py) and provisioning logs alongside
    # them as supplementary diagnostics.
    if [ -d "$sub_root/pair-1" ]; then
        cp -r "$sub_root/pair-1" "$OUT_ROOT_AGG/pair-$n"
        # Fold in the provisioning logs so a single pair-$n/ directory
        # has everything a postmortem might want.
        if [ -f "$AWS_DIR/multi/pair-1/up.log" ]; then
            cp "$AWS_DIR/multi/pair-1/up.log"      "$OUT_ROOT_AGG/pair-$n/up.log"
        fi
        if [ -f "$AWS_DIR/multi/pair-1/install.log" ]; then
            cp "$AWS_DIR/multi/pair-1/install.log" "$OUT_ROOT_AGG/pair-$n/install.log"
        fi
    else
        log "WARN: $sub_root/pair-1 missing after success report on run $n; aggregation will skip it"
        # demote from success to failure
        PAIR_FAIL+=("$n")
        PAIR_SUCCESS=("${PAIR_SUCCESS[@]/$n}")
    fi

    # Wipe the working state for the next pair.
    rm -rf "$AWS_DIR/multi/pair-1"
done

log "=== sequential run complete: ${#PAIR_SUCCESS[@]} pair(s) succeeded, ${#PAIR_FAIL[@]} failed ==="

if [ "${#PAIR_SUCCESS[@]}" -eq 0 ]; then
    log "FATAL: no pairs succeeded; nothing to aggregate"
    exit 1
fi

log "Aggregating ${#PAIR_SUCCESS[@]} pair(s): ${PAIR_SUCCESS[*]}"
python3 "$AWS_DIR/_aggregate_ab.py" \
    --root "$OUT_ROOT_AGG" \
    --pairs "${PAIR_SUCCESS[*]}" \
    > "$OUT_ROOT_AGG/aggregate.md" 2>&1 || {
        log "WARN: aggregation failed; per-pair results are still in $OUT_ROOT_AGG/pair-*"
    }

log "Done.  Aggregated report: $OUT_ROOT_AGG/aggregate.md"
