#!/usr/bin/env bash
# bench/ab/ab_commit_ws.sh — A/B two BlackBull commits on WebSocket echo (k6).
#
# The ab_commit.sh discipline (abba.sh) on a WebSocket lane: k6 floods
# concurrent connections (bench/load/k6/websocket_echo_throughput.js) and the
# server's echo rate (the ws_echoed counter, msg/s) is the measured axis.
# Identical refs with BASE_EXTRA_ENV / TREAT_EXTRA_ENV compare two runtime
# settings on one build, e.g. BB_WS_QUEUE_DEPTH=0 against 256.
#
#   REF_BASE=HEAD~1 ROUNDS=4 bash bench/ab/ab_commit_ws.sh
#
# Lane env:
#   WS_PATH(=/ws)  /ws = raw (conn, receive, send) form; /ws-object = object form
#   WS_VUS(=100) WS_DURATION(=10s) WS_TICK_MS(=1) WS_BURST(=8) WS_LIFETIME_MS(=8000)
#
# Output: raw.tsv, report.md and k6 summaries in the run directory.

source "$(dirname "$0")/abba.sh"

WS_PATH="${WS_PATH:-/ws}"
WS_VUS="${WS_VUS:-100}"
WS_DURATION="${WS_DURATION:-10s}"
WS_TICK_MS="${WS_TICK_MS:-1}"
WS_BURST="${WS_BURST:-8}"
WS_LIFETIME_MS="${WS_LIFETIME_MS:-8000}"

AB_TOOL=ab_commit_ws
AB_LANE="k6 websocket_echo_throughput.js vus=$WS_VUS d=$WS_DURATION tick=${WS_TICK_MS}ms x$WS_BURST $WS_PATH"

ab_start_server() {
    ab_spawn "$PY" -m blackbull.cli bench.apps.native_app:app --bind "127.0.0.1:$PORT"
    ab_wait_ready "http://127.0.0.1:$PORT/plaintext"
}

ab_measure() {
    "${PIN_LOAD[@]}" k6 run --quiet --summary-export "$AB_RUN/k6_$1.json" \
        --env WS_URL="ws://127.0.0.1:$PORT$WS_PATH" \
        --env WS_VUS="$WS_VUS" --env WS_DURATION="$WS_DURATION" \
        --env WS_TICK_MS="$WS_TICK_MS" --env WS_BURST="$WS_BURST" \
        --env WS_LIFETIME_MS="$WS_LIFETIME_MS" \
        bench/load/k6/websocket_echo_throughput.js > "$AB_RUN/k6_$1.log" 2>&1
    "$PY" -c 'import json, sys
try: print(json.load(open(sys.argv[1]))["metrics"]["ws_echoed"]["rate"])
except (OSError, KeyError, ValueError): print("NaN")' "$AB_RUN/k6_$1.json"
}

abba_main
