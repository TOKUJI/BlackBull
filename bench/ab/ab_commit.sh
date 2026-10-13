#!/usr/bin/env bash
# bench/ab/ab_commit.sh — A/B two BlackBull commits on HTTP/1.1 (wrk).
#
# "Did my commit cost anything?": one stack, two code states, ABBA inside one
# session, an A/A null phase for the floor.  Discipline and the shared knobs
# are in abba.sh; the method and the verdict rules in AB-HIGH-PRECISION.md.
#
#   bash bench/ab/ab_commit.sh
#   REF_BASE=v0.67.0 ROUNDS=8 URL_PATH=/conn bash bench/ab/ab_commit.sh
#
# Lane env:
#   URL_PATH(=/plaintext) DURATION(=10) WARMUP(=5, discarded) THREADS(=4) CONNS(=32)
#   PIPELINE(=1)  wrk pipeline depth via bench/load/wrk/pipeline.lua
#   WRK_HEADERS   extra wrk header args, e.g. "-H Accept-Encoding:gzip" — without
#                 one the compression middleware never selects an encoding
#   WRK_SCRIPT / WRK_SCRIPT_ARGS  a wrk Lua script (e.g. bench/load/wrk/post_echo.lua)
#   APP(=bench.apps.native_app:app) APP_PYTHONPATH  the served app
#   TLS_CERT / TLS_KEY  serve HTTPS with these files
#
# Output: raw.tsv, report.md and wrk logs in the run directory.

source "$(dirname "$0")/abba.sh"

URL_PATH="${URL_PATH:-/plaintext}"
DURATION="${DURATION:-10}"
WARMUP="${WARMUP:-5}"
THREADS="${THREADS:-4}"
CONNS="${CONNS:-32}"
PIPELINE="${PIPELINE:-1}"
WRK_HEADERS="${WRK_HEADERS:-}"
WRK_SCRIPT="${WRK_SCRIPT:-}"
WRK_SCRIPT_ARGS="${WRK_SCRIPT_ARGS:-}"
APP="${APP:-bench.apps.native_app:app}"
BASE_URL="$([ -n "${TLS_CERT:-}" ] && echo https || echo http)://127.0.0.1:${PORT}"
WRK_EXTRA=()
if [ -n "$WRK_SCRIPT" ]; then
    WRK_EXTRA=(-s "$WRK_SCRIPT" ${WRK_SCRIPT_ARGS:+-- $WRK_SCRIPT_ARGS})
elif [ "$PIPELINE" != 1 ]; then
    WRK_EXTRA=(-s bench/load/wrk/pipeline.lua -- "$PIPELINE")
fi

AB_TOOL=ab_commit
AB_LANE="wrk -t$THREADS -c$CONNS -d${DURATION}s $WRK_HEADERS $BASE_URL$URL_PATH ${WRK_EXTRA[*]}"

ab_start_server() {
    PYTHONPATH="${APP_PYTHONPATH:-}" ab_spawn "$PY" -m blackbull.cli "$APP" \
        --bind "127.0.0.1:$PORT" ${TLS_CERT:+--certfile "$TLS_CERT" --keyfile "$TLS_KEY"}
    # Same headers as wrk, so readiness exercises the measured middleware branch.
    # shellcheck disable=SC2086  # WRK_HEADERS is a word list
    ab_wait_ready $WRK_HEADERS "$BASE_URL$URL_PATH"
}

ab_measure() {
    # shellcheck disable=SC2086
    "${PIN_LOAD[@]}" wrk -t"$THREADS" -c"$CONNS" -d"${WARMUP}s" $WRK_HEADERS \
        "$BASE_URL$URL_PATH" "${WRK_EXTRA[@]}" > /dev/null 2>&1
    # shellcheck disable=SC2086
    "${PIN_LOAD[@]}" wrk -t"$THREADS" -c"$CONNS" -d"${DURATION}s" --latency $WRK_HEADERS \
        "$BASE_URL$URL_PATH" "${WRK_EXTRA[@]}" > "$AB_RUN/wrk_$1.txt" 2>&1
    awk '/Requests\/sec:/ {print $2}' "$AB_RUN/wrk_$1.txt"
}

abba_main
