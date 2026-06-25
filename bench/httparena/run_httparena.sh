#!/usr/bin/env bash
# bench/httparena/run_httparena.sh — runs on EC2 instance.
#
# Runs HttpArena validate.sh + benchmark.sh for the given frameworks
# and profiles.  Called by bench/aws/httparena_compare.sh via scp + exec.
#
# Args: <frameworks_csv> <profiles_csv> [skip_validate]
#   frameworks_csv  — comma-separated: "blackbull,fastapi"
#   profiles_csv    — comma-separated: "baseline,static,json,..."
#   skip_validate   — "1" to skip validate.sh
#   wrk_cpus        — optional: GCANNON_CPUS value for taskset

set -euo pipefail

FRAMEWORKS_CSV="${1:-blackbull}"
PROFILES_CSV="${2:-baseline,json,json-tls,static}"
SKIP_VALIDATE="${3:-0}"
WRK_CPUS="${4:-}"

IFS=',' read -ra FRAMEWORKS <<< "$FRAMEWORKS_CSV"
IFS=',' read -ra PROFILES <<< "$PROFILES_CSV"

HARENA_DIR="${HOME}/HttpArena"
RESULTS_DIR="${HOME}/results"
mkdir -p "$RESULTS_DIR"

# ── Validate ───────────────────────────────────────────────────────────────
if [ "$SKIP_VALIDATE" != "1" ]; then
    echo "=== HttpArena validate (correctness check) ==="
    for fw in "${FRAMEWORKS[@]}"; do
        echo "  - $fw"
        cd "$HARENA_DIR"
        if sudo VALIDATE_TIMEOUT=600 ./scripts/validate.sh "$fw" 2>&1 | tee "$RESULTS_DIR/validate-${fw}.log"; then
            echo "    validate $fw: PASS"
        else
            echo "    validate $fw: FAIL (non-zero exit — kept going)"
        fi
    done
fi

# ── Benchmark ──────────────────────────────────────────────────────────────
echo "=== HttpArena benchmark ==="

# GCANNON_CPUS for wrk CPU pinning.
if [ -n "$WRK_CPUS" ]; then
    if [[ "$WRK_CPUS" =~ ^[0-9]+$ ]]; then
        _GCANNON_CPUS="0-$(( WRK_CPUS - 1 ))"
    else
        _GCANNON_CPUS="$WRK_CPUS"
    fi
    _SUDO_ENV="env GCANNON_CPUS=${_GCANNON_CPUS}"
else
    _SUDO_ENV=""
fi

for fw in "${FRAMEWORKS[@]}"; do
    for prof in "${PROFILES[@]}"; do
        echo "  - $fw / $prof"
        cd "$HARENA_DIR"
        if sudo ${_SUDO_ENV} ./scripts/benchmark.sh "$fw" "$prof" --save 2>&1 | tee "$RESULTS_DIR/benchmark-${fw}-${prof}.log"; then
            echo "    benchmark $fw / $prof: OK"
        else
            echo "    benchmark $fw / $prof: FAIL (non-zero exit — kept going)"
        fi

        # Extract shim annotations for wrk from combined output.
        grep -E '^\[bench-shim\].*wrk' \
            "$RESULTS_DIR/benchmark-${fw}-${prof}.log" \
            > "$RESULTS_DIR/wrk-${fw}-${prof}.log" 2>/dev/null || true

        # Best-effort: grab docker logs from any exited wrk containers.
        for cid in $(sudo docker ps -a --filter 'ancestor=wrk' -q 2>/dev/null); do
            sudo docker logs "$cid" \
                >> "$RESULTS_DIR/wrk-${fw}-${prof}.log" \
                2>> "$RESULTS_DIR/wrk-${fw}-${prof}.err" \
                || true
        done
    done
done

echo "=== run_httparena.sh complete ==="
echo "Results in $RESULTS_DIR"
ls -la "$RESULTS_DIR/"
