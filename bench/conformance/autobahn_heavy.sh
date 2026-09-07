#!/usr/bin/env bash
# bench/conformance/autobahn_heavy.sh — run the heavy Autobahn lane with one
# retry.
#
# Deflate-echo cases can exceed their case budgets under runner contention.
# A retry allows recovery, but neither a timeout nor a tester crash proves
# the server is conformant. Preserve each attempt's evidence for diagnosis.
# Only the heavy lane is re-run, never the whole suite.
#
# Each attempt renders its own results dir (autobahn_run.sh timestamps it) and
# the assert is pointed at that attempt's index.json explicitly — a failed
# attempt must never be judged against a previous attempt's results.
#
# Exit 0 on the first passing attempt; 1 after MAX_ATTEMPTS failures.

set -uo pipefail

CASES="${CASES:?CASES must be set (e.g. 12.*.10,13.*)}"
MAX_ATTEMPTS="${MAX_ATTEMPTS:-2}"
LOG="$(mktemp "${TMPDIR:-/tmp}/autobahn_heavy.XXXXXX.log")" || exit 1
trap 'rm -f "$LOG"' EXIT

for attempt in $(seq 1 "$MAX_ATTEMPTS"); do
    echo "=== Autobahn heavy lane: attempt $attempt/$MAX_ATTEMPTS ==="
    if CASES="$CASES" bash bench/conformance/autobahn_run.sh 2>&1 | tee "$LOG"; then
        RUN_STATUS=0
    else
        RUN_STATUS=$?
        echo "autobahn_run.sh exited $RUN_STATUS on attempt $attempt" >&2
    fi

    OUT=$(sed -n 's/^Results: //p' "$LOG" | tail -1)
    INDEX=""
    if [ -n "$OUT" ] && [ -f "$OUT/index.json" ]; then
        INDEX="$OUT/index.json"
    else
        echo "attempt $attempt produced no index.json (outdir=${OUT:-<none>})" >&2
    fi

    if [ "$RUN_STATUS" -eq 0 ] && [ -n "$INDEX" ] && \
        bash bench/conformance/autobahn_assert.sh "$INDEX"; then
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
