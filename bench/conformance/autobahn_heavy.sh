#!/usr/bin/env bash
# bench/conformance/autobahn_heavy.sh — run the heavy Autobahn lane with one
# retry.
#
# C2 for the 12.3.10/14-18 + 13.x deflate-echo cases: they are 1000 x 64-128
# KiB permessage-deflate round-trips under a 480 s wstest case budget, so a
# shared-runner contention spike can push one case past its budget and mark it
# FAILED.  That is a throughput flake, not a protocol regression — and it
# usually clears on a fresh attempt of the same lane, so the lane re-runs
# itself once before giving up.  Only the small heavy lane is re-run, never
# the whole suite.
#
# Each attempt renders its own results dir (autobahn_run.sh timestamps it) and
# the assert is pointed at that attempt's index.json explicitly — a failed
# attempt must never be judged against a previous attempt's results.
#
# Exit 0 on the first passing attempt; 1 after MAX_ATTEMPTS failures.

set -uo pipefail

CASES="${CASES:?CASES must be set (e.g. 12.*.10,13.*)}"
MAX_ATTEMPTS="${MAX_ATTEMPTS:-2}"

for attempt in $(seq 1 "$MAX_ATTEMPTS"); do
    echo "=== Autobahn heavy lane: attempt $attempt/$MAX_ATTEMPTS ==="
    LOG="/tmp/autobahn_heavy_attempt_${attempt}.log"
    if CASES="$CASES" bash bench/conformance/autobahn_run.sh > "$LOG" 2>&1; then
        cat "$LOG"
    else
        cat "$LOG"
        echo "autobahn_run.sh exited non-zero on attempt $attempt" >&2
    fi

    OUT=$(sed -n 's/^Results: //p' "$LOG" | tail -1)
    INDEX=""
    if [ -n "$OUT" ] && [ -f "$OUT/index.json" ]; then
        INDEX="$OUT/index.json"
    else
        echo "attempt $attempt produced no index.json (outdir=${OUT:-<none>})" >&2
    fi

    if [ -n "$INDEX" ] && bash bench/conformance/autobahn_assert.sh "$INDEX"; then
        echo "Autobahn heavy lane passed on attempt $attempt"
        exit 0
    fi

    if [ "$attempt" -lt "$MAX_ATTEMPTS" ]; then
        echo "Attempt $attempt failed; retrying in 30s ..."
        sleep 30
    fi
done

echo "Autobahn heavy lane failed after $MAX_ATTEMPTS attempts" >&2
exit 1
