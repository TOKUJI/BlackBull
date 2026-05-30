#!/usr/bin/env bash
# bench/wrk/run.sh — drive HTTP/1.1 scenarios B1-B7 from CHARACTERIZATION.md.
#
# Assumes the target server is already running on $BASE.
# Outputs a markdown table on stdout; raw wrk output is captured to
# $OUTDIR/wrk_<scenario>.txt for forensics.
#
# Usage:
#   BASE=https://localhost:8443 OUTDIR=bench/results bash bench/wrk/run.sh
#
# Env:
#   BASE      target URL          (default https://localhost:8443)
#   OUTDIR    raw-log directory   (default bench/results)
#   LABEL     stack label         (default <none> — header only)
#   DURATION  per-scenario time   (default 30 — seconds)

# NOTE: we deliberately do NOT use `set -e`. wrk's exit code becomes
# non-zero when the target dies mid-run, and aborting here cascades up
# into the orchestrator. Each run_one isolates its own failures and
# emits an `err` row instead.

BASE="${BASE:-https://localhost:8443}"
OUTDIR="${OUTDIR:-bench/results}"
LABEL="${LABEL:-}"
LABEL_PREFIX="${LABEL_PREFIX:-}"   # filename prefix so per-stack runs don't overwrite
D="${DURATION:-30}"
# Sprint 24 (audit 4-1, 4-2): RUNS_WRK > 1 enables multi-run aggregation —
# median req/s with MAD / min / max / noise% columns.  RUNS_WRK=1 keeps
# Sprint 13–23 single-run behaviour (the report just emits the wrk numbers
# directly and the noise columns read `—`).
RUNS_WRK="${RUNS_WRK:-3}"

mkdir -p "$OUTDIR"

_SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# wrk doesn't disable TLS verification natively; LD_PRELOAD or env trickery
# isn't portable.  Use --latency for distribution; --no-check-certificate
# is supported by some wrk forks but not vanilla.  Mkcert CA in env handles it.
WRK="wrk --latency"

run_one() {
    local label="$1" threads="$2" conns="$3" extra="$4" url="$5" pipe_args="$6"
    local cmd="$WRK -t${threads} -c${conns} -d${D}s ${extra} ${url}"
    if [ -n "$pipe_args" ]; then
        cmd="$cmd -- $pipe_args"
    fi

    # Sprint 24: loop RUNS_WRK times, capture each run's req/s + the
    # latency stats from the LAST run (the latency distribution is
    # already a per-run percentile; aggregating it across runs would
    # be misleading, while aggregating throughput is straightforward).
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
            echo "# scenario: $label"
            echo "# run: $i / $RUNS_WRK"
            echo "# command: $cmd"
            eval "$cmd"
        } > "$logfile" 2>&1 || true   # tolerate connection-refused mid-run

        # If wrk couldn't connect at all, emit an err row and bail.
        if grep -q "unable to connect" "$logfile" 2>/dev/null; then
            echo "| $label | err | — | — | — | — |  (unable to connect) | — | — |"
            return 0
        fi
        local rps_one
        rps_one=$(grep 'Requests/sec' "$logfile" | awk '{print $2}')
        rps_list="$rps_list $rps_one"
    done

    # Parse latency / socket errors from the last run.  See above for why
    # latency distribution is reported per-run rather than aggregated.
    local avg p50 p99 max errs
    avg=$(grep -m1 '^    Latency' "$last_logfile" | awk '{print $2}')
    p50=$(grep -E '^[[:space:]]+50%[[:space:]]' "$last_logfile" | awk '{print $2}')
    p99=$(grep -E '^[[:space:]]+99%[[:space:]]' "$last_logfile" | awk '{print $2}')
    max=$(grep -m1 '^    Latency' "$last_logfile" | awk '{print $4}')
    errs=$(grep -m1 'Socket errors:' "$last_logfile" | sed 's/^[[:space:]]*Socket errors:[[:space:]]*//')
    [ "$p50" = "0.00us" ] && p50="—"
    [ "$p99" = "0.00us" ] && p99="—"

    # Aggregate req/s across runs.  When RUNS_WRK=1 the helper returns
    # the single value as the median and reports MAD=0 / noise=0; the
    # noise-flag rule then naturally leaves the row unmarked.
    # shellcheck disable=SC2086
    local agg
    agg=$(python3 "$_SCRIPT_DIR/_stats.py" $rps_list)
    local rps_med mad lo hi noise_pct
    read -r rps_med mad lo hi noise_pct <<< "$agg"
    # Format the noise column: "MAD=X (Y%)" or "🌫 MAD=X (Y%)" if Y>=10.
    local noise_col
    if [ "$mad" = "—" ] || [ "$RUNS_WRK" = "1" ]; then
        noise_col="—"
    else
        local flag=""
        # Bash compare floats by stripping the dot — close enough for the
        # 10 % threshold.  noise_pct is always 0.0..100.0.
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

# Header — Sprint 24 added the trailing "noise (MAD)" column.  Rows
# where MAD/median > 10 % carry a 🌫 marker; older RUNS_WRK=1 reports
# leave the column blank.
echo "| Scenario | req/s | mean lat | p50 | p99 | max | socket errors | noise (MAD) |"
echo "|---|---|---|---|---|---|---|---|"

# B1 — /plaintext, no pipeline (TechEmpower-style baseline)
run_one "B1_plaintext_c256"   4  256 ""                                  "$BASE/plaintext" ""
# B2 — /plaintext, pipeline depth 16 (TechEmpower-style high-concurrency)
run_one "B2_plaintext_c1024_p16" 4 1024 "-s bench/wrk/pipeline.lua"       "$BASE/plaintext" "16"
# B3 — /json (TechEmpower JSON-comparable)
run_one "B3_json_c256"        4  256 ""                                  "$BASE/json" ""
# B4 — /16kb (internal)
run_one "B4_16kb_c100"        4  100 ""                                  "$BASE/16kb" ""
# B5 — /64kb (internal)
run_one "B5_64kb_c50"         4   50 ""                                  "$BASE/64kb" ""
# B6 + B7 — /echo POST. Skipped for static-only references (nginx) which
# can't accept POST without an upstream.
if [ "${SKIP_POST:-0}" != "1" ]; then
    run_one "B6_echo_1kb"         4  100 "-s bench/wrk/post_echo.lua"   "$BASE/echo" "1024"
    run_one "B7_echo_100kb"       4   50 "-s bench/wrk/post_echo.lua"   "$BASE/echo" "102400"
fi
