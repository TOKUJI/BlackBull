#!/usr/bin/env bash
# bench/conformance/autobahn_heavy.sh — run the heavy Autobahn lane in
# isolated CaseSet subgroups, with one retry of the complete lane.
#
# Deflate-echo cases can exceed their case budgets under runner contention.
# A retry allows recovery, but neither a timeout nor a tester crash proves
# the server is conformant. Preserve each attempt's evidence for diagnosis.
# Only the heavy lane is re-run, never the whole suite.
#
# The pinned tester resolves the configured selectors to concrete IDs. Cases
# are partitioned by their first two ID components, and each subgroup gets a
# fresh tester process so completed cases cannot retain memory into the next
# subgroup. Every report must contain exactly its expected IDs.
#
# Exit 0 on the first passing attempt; 1 after MAX_ATTEMPTS failures.

set -euo pipefail

CASES="${CASES:?CASES must be set (e.g. 12.*.10,13.*)}"
MAX_ATTEMPTS="${MAX_ATTEMPTS:-2}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
. "$SCRIPT_DIR/autobahn_common.sh"
RESULT_BASE=bench/conformance/results
mkdir -p "$RESULT_BASE"
TS="$(date +%Y%m%d-%H%M%S)"
HEAVY_ROOT="$(mktemp -d "$RESULT_BASE/autobahn_heavy_${TS}.XXXXXX")" || exit 1
RESOLVER_CIDFILE="$HEAVY_ROOT/resolver.cid"
RESOLVER_CONTAINER=''

cleanup() {
    local status=$?
    trap - EXIT INT TERM
    set +e
    if [ -z "$RESOLVER_CONTAINER" ] && [ -s "$RESOLVER_CIDFILE" ]; then
        IFS= read -r RESOLVER_CONTAINER < "$RESOLVER_CIDFILE" || true
    fi
    if [ -n "$RESOLVER_CONTAINER" ]; then
        timeout 10 docker rm -f "$RESOLVER_CONTAINER" >/dev/null 2>&1
    fi
    printf '%s\n' "$status" > "$HEAVY_ROOT/exit-code.txt"
    exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

if ! command -v docker >/dev/null 2>&1; then
    echo 'ERROR: docker not on PATH' >&2
    exit 1
fi
if ! command -v python3 >/dev/null 2>&1; then
    echo 'ERROR: python3 not on PATH' >&2
    exit 1
fi

if ! docker create --cidfile "$RESOLVER_CIDFILE" "$AUTOBAHN_IMAGE" \
    python /tmp/autobahn_cases.py resolve "$CASES" /tmp/resolved.json \
    > "$HEAVY_ROOT/resolver-create.log" 2>&1; then
    echo 'ERROR: could not create Autobahn CaseSet resolver' >&2
    exit 1
fi
# Docker's cidfile has no trailing newline, so `read` returns non-zero after
# assigning a valid ID. Validate the assigned value instead of the EOF status.
IFS= read -r RESOLVER_CONTAINER < "$RESOLVER_CIDFILE" || true
if [ -z "$RESOLVER_CONTAINER" ]; then
    echo 'ERROR: Autobahn CaseSet resolver produced no container ID' >&2
    exit 1
fi
if ! docker cp "$SCRIPT_DIR/autobahn_cases.py" \
    "$RESOLVER_CONTAINER:/tmp/autobahn_cases.py" \
    > "$HEAVY_ROOT/resolver-copy-in.log" 2>&1; then
    echo 'ERROR: could not copy CaseSet resolver into tester' >&2
    exit 1
fi
if docker start -a "$RESOLVER_CONTAINER" \
    > "$HEAVY_ROOT/resolver.log" 2>&1; then
    RESOLVER_STATUS=0
else
    RESOLVER_STATUS=$?
fi
if ! timeout 10 docker inspect --format '{{json .State}}' "$RESOLVER_CONTAINER" \
    > "$HEAVY_ROOT/resolver-state.json"; then
    echo 'ERROR: could not inspect Autobahn CaseSet resolver' >&2
    exit 1
fi
if [ "$RESOLVER_STATUS" -eq 0 ]; then
    if docker cp "$RESOLVER_CONTAINER:/tmp/resolved.json" \
        "$HEAVY_ROOT/resolved.json" \
        > "$HEAVY_ROOT/resolver-copy-out.log" 2>&1; then
        RESOLVER_COPY_STATUS=0
    else
        RESOLVER_COPY_STATUS=$?
    fi
else
    RESOLVER_COPY_STATUS=1
fi
timeout 10 docker rm -f "$RESOLVER_CONTAINER" >/dev/null
: > "$RESOLVER_CIDFILE"
RESOLVER_CONTAINER=''

if [ "$RESOLVER_STATUS" -ne 0 ] || [ "$RESOLVER_COPY_STATUS" -ne 0 ] || \
    ! jq -e '.ExitCode == 0 and .OOMKilled == false' \
        "$HEAVY_ROOT/resolver-state.json" >/dev/null; then
    echo 'ERROR: Autobahn CaseSet resolver failed' >&2
    exit 1
fi
if ! python3 "$SCRIPT_DIR/autobahn_cases.py" manifest \
    "$HEAVY_ROOT/resolved.json" "$HEAVY_ROOT/manifest.json"; then
    echo 'ERROR: Autobahn CaseSet manifest is invalid' >&2
    exit 1
fi

mapfile -t BATCHES < <(jq -r '.batches[].name' "$HEAVY_ROOT/manifest.json")
EXPECTED_BATCHES="$(jq -r '.batch_count' "$HEAVY_ROOT/manifest.json")"
if [ "${#BATCHES[@]}" -ne "$EXPECTED_BATCHES" ] || \
    [ "$EXPECTED_BATCHES" -eq 0 ]; then
    echo 'ERROR: Autobahn CaseSet manifest has an invalid batch count' >&2
    exit 1
fi

for attempt in $(seq 1 "$MAX_ATTEMPTS"); do
    echo "=== Autobahn heavy lane: attempt $attempt/$MAX_ATTEMPTS ==="
    ATTEMPT_ROOT="$(mktemp -d "$HEAVY_ROOT/attempt_${attempt}.XXXXXX")" || exit 1
    cp "$HEAVY_ROOT/manifest.json" "$ATTEMPT_ROOT/manifest.json"
    ATTEMPT_PASSED=1
    COMPLETED_BATCHES=0

    for batch in "${BATCHES[@]}"; do
        SAFE_BATCH="${batch//./_}"
        EXPECTED="$ATTEMPT_ROOT/${SAFE_BATCH}.expected.json"
        LOG="$ATTEMPT_ROOT/${SAFE_BATCH}.log"
        jq -c --arg batch "$batch" \
            '.batches[] | select(.name == $batch) | .cases' \
            "$HEAVY_ROOT/manifest.json" > "$EXPECTED"
        BATCH_CASES="$(jq -r --arg batch "$batch" \
            '.batches[] | select(.name == $batch) | .cases | join(",")' \
            "$HEAVY_ROOT/manifest.json")"
        if [ -z "$BATCH_CASES" ]; then
            echo "attempt $attempt batch $batch has an empty manifest" >&2
            ATTEMPT_PASSED=0
            break
        fi

        echo "--- Autobahn subgroup $batch ---"
        if CASES="$BATCH_CASES" bash "$SCRIPT_DIR/autobahn_run.sh" \
            2>&1 | tee "$LOG"; then
            RUN_STATUS=0
        else
            RUN_STATUS=$?
            echo "autobahn_run.sh exited $RUN_STATUS for batch $batch" >&2
        fi
        OUT="$(sed -n 's/^Results: //p' "$LOG" | tail -1)"
        INDEX=''
        if [ -n "$OUT" ] && [ -f "$OUT/index.json" ]; then
            INDEX="$OUT/index.json"
        else
            echo "batch $batch produced no index.json (outdir=${OUT:-<none>})" >&2
        fi
        if [ "$RUN_STATUS" -ne 0 ] || [ -z "$INDEX" ] || \
            ! python3 "$SCRIPT_DIR/autobahn_cases.py" verify \
                "$EXPECTED" "$INDEX" || \
            ! bash "$SCRIPT_DIR/autobahn_assert.sh" "$INDEX"; then
            ATTEMPT_PASSED=0
            break
        fi
        COMPLETED_BATCHES=$((COMPLETED_BATCHES + 1))
    done

    if [ "$ATTEMPT_PASSED" -eq 1 ] && \
        [ "$COMPLETED_BATCHES" -eq "$EXPECTED_BATCHES" ]; then
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
