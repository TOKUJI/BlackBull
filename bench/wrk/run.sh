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

mkdir -p "$OUTDIR"

# wrk doesn't disable TLS verification natively; LD_PRELOAD or env trickery
# isn't portable.  Use --latency for distribution; --no-check-certificate
# is supported by some wrk forks but not vanilla.  Mkcert CA in env handles it.
WRK="wrk --latency"

run_one() {
    local label="$1" threads="$2" conns="$3" extra="$4" url="$5" pipe_args="$6"
    local logfile="$OUTDIR/wrk_${LABEL_PREFIX}${label}.txt"
    local cmd="$WRK -t${threads} -c${conns} -d${D}s ${extra} ${url}"
    if [ -n "$pipe_args" ]; then
        cmd="$cmd -- $pipe_args"
    fi
    {
        echo "# scenario: $label"
        echo "# command: $cmd"
        eval "$cmd"
    } > "$logfile" 2>&1 || true   # tolerate connection-refused mid-run

    # If wrk couldn't connect at all, emit an err row and bail.
    if grep -q "unable to connect" "$logfile" 2>/dev/null; then
        echo "| $label | err | — | — | — | — |  (unable to connect)"
        return 0
    fi

    # Parse: req/s, avg latency, p50, p99, max, socket errors.
    # Anchor 50%/99% to the Latency Distribution rows — bare `grep 50%`
    # can also match the Thread Stats "+/- Stdev" column when its value
    # ends in `X.50%` / `X.99%`, splitting the cell across lines.
    # wrk's `--latency` p50/p99 can overflow to `0.00us` under pipelining;
    # show `—` in that case.
    local rps avg p50 p99 max errs
    rps=$(grep 'Requests/sec' "$logfile" | awk '{print $2}')
    avg=$(grep -m1 '^    Latency' "$logfile" | awk '{print $2}')
    p50=$(grep -E '^[[:space:]]+50%[[:space:]]' "$logfile" | awk '{print $2}')
    p99=$(grep -E '^[[:space:]]+99%[[:space:]]' "$logfile" | awk '{print $2}')
    max=$(grep -m1 '^    Latency' "$logfile" | awk '{print $4}')
    # Socket errors line, if present: `Socket errors: connect N, read N, write N, timeout N`
    errs=$(grep -m1 'Socket errors:' "$logfile" | sed 's/^[[:space:]]*Socket errors:[[:space:]]*//')
    [ "$p50" = "0.00us" ] && p50="—"
    [ "$p99" = "0.00us" ] && p99="—"
    if [ -n "$errs" ]; then
        echo "| $label | $rps | $avg | $p50 | $p99 | $max | $errs |"
    else
        echo "| $label | $rps | $avg | $p50 | $p99 | $max |  |"
    fi
}

# Header
echo "| Scenario | req/s | mean lat | p50 | p99 | max | socket errors |"
echo "|---|---|---|---|---|---|---|"

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
