#!/usr/bin/env bash
# bench/wrk/lane_e.sh — Sprint 24 Lane E: connection-churn / short-lived
# connections.  Mirrors bench/wrk/run.sh's contract (same env, same row
# format including the trailing "noise (MAD)" column) but only runs E1
# and E2 with the no_keepalive.lua script forcing `Connection: close`.
#
# Lane E exposes accept-loop + TLS-handshake costs that the existing
# keep-alive-dominated Lane B hides.
#
# Assumes the target server is already running on $BASE.

BASE="${BASE:-https://localhost:8443}"
OUTDIR="${OUTDIR:-bench/results}"
LABEL="${LABEL:-}"
LABEL_PREFIX="${LABEL_PREFIX:-}"
D="${DURATION:-60}"
RUNS_WRK="${RUNS_WRK:-3}"

mkdir -p "$OUTDIR"

_SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
WRK="wrk --latency"

run_one() {
    local label="$1" threads="$2" conns="$3" url="$4"
    local cmd="$WRK -t${threads} -c${conns} -d${D}s -s ${_SCRIPT_DIR}/no_keepalive.lua ${url}"

    local rps_list=""
    local last_logfile=""
    local i
    for i in $(seq 1 "$RUNS_WRK"); do
        local logfile
        if [ "$RUNS_WRK" = "1" ]; then
            logfile="$OUTDIR/wrk_${LABEL_PREFIX}${label}.txt"
        else
            logfile="$OUTDIR/wrk_${LABEL_PREFIX}${label}_run${i}.txt"
        fi
        last_logfile="$logfile"
        {
            echo "# scenario: $label (no-keepalive)"
            echo "# run: $i / $RUNS_WRK"
            echo "# command: $cmd"
            eval "$cmd"
        } > "$logfile" 2>&1 || true

        if grep -q "unable to connect" "$logfile" 2>/dev/null; then
            echo "| $label | err | — | — | — | — |  (unable to connect) | — |"
            return 0
        fi
        local rps_one
        rps_one=$(grep 'Requests/sec' "$logfile" | awk '{print $2}')
        rps_list="$rps_list $rps_one"
    done

    local avg p50 p99 max errs
    avg=$(grep -m1 '^    Latency' "$last_logfile" | awk '{print $2}')
    p50=$(grep -E '^[[:space:]]+50%[[:space:]]' "$last_logfile" | awk '{print $2}')
    p99=$(grep -E '^[[:space:]]+99%[[:space:]]' "$last_logfile" | awk '{print $2}')
    max=$(grep -m1 '^    Latency' "$last_logfile" | awk '{print $4}')
    errs=$(grep -m1 'Socket errors:' "$last_logfile" | sed 's/^[[:space:]]*Socket errors:[[:space:]]*//')
    [ "$p50" = "0.00us" ] && p50="—"
    [ "$p99" = "0.00us" ] && p99="—"

    # shellcheck disable=SC2086
    local agg
    agg=$(python3 "$_SCRIPT_DIR/_stats.py" $rps_list)
    local rps_med mad lo hi noise_pct
    read -r rps_med mad lo hi noise_pct <<< "$agg"
    local noise_col
    if [ "$mad" = "—" ] || [ "$RUNS_WRK" = "1" ]; then
        noise_col="—"
    else
        local flag=""
        local noise_x10
        noise_x10=$(printf '%.0f' "$noise_pct")
        if [ "$noise_x10" -ge 10 ]; then
            flag="🌫 "
        fi
        noise_col="${flag}${lo}..${hi} (MAD ${mad}, ${noise_pct}%)"
    fi
    if [ -n "$errs" ]; then
        echo "| $label | $rps_med | $avg | $p50 | $p99 | $max | $errs | $noise_col |"
    else
        echo "| $label | $rps_med | $avg | $p50 | $p99 | $max |  | $noise_col |"
    fi
}

echo "| Scenario | req/s | mean lat | p50 | p99 | max | socket errors | noise (MAD) |"
echo "|---|---|---|---|---|---|---|---|"

# E1 — /plaintext, no keep-alive, c=256.  Same concurrency as B1 so the
# delta vs B1 is the connection-setup cost (handshake + accept-loop).
run_one "E1_plaintext_c256_noka" 4 256 "$BASE/plaintext"
