#!/usr/bin/env bash
# bench/ab/ab_commit_ws.sh — A/B two BlackBull commits on WebSocket echo.
#
# The ab_commit.sh discipline (abba.sh) on a WebSocket lane, the server's echo
# rate (msg/s) being the measured axis.  Two load shapes:
#   WS_TOOL=k6       k6 floods concurrent connections with bursts
#                    (bench/load/k6/websocket_echo_throughput.js)
#   WS_TOOL=gcannon  WS_CONNS long-lived connections, one frame in flight each:
#                    HttpArena's echo-ws shape (bench/load/install_gcannon.sh)
# Identical refs with BASE_EXTRA_ENV / TREAT_EXTRA_ENV compare two runtime
# settings on one build, e.g. BB_WS_QUEUE_DEPTH=0 against 256.
#
#   REF_BASE=HEAD~1 ROUNDS=4 bash bench/ab/ab_commit_ws.sh
#   REF_BASE=v0.78.1 WS_TOOL=gcannon WS_CONNS=1024 bash bench/ab/ab_commit_ws.sh
#
# Lane env:
#   WS_PATH(=/ws)  /ws = raw (conn, receive, send) form; /ws-object = object form
#   WS_TOOL(=k6)
#   k6:      WS_VUS(=100) WS_DURATION(=10s) WS_TICK_MS(=1) WS_BURST(=8) WS_LIFETIME_MS(=8000)
#   gcannon: WS_CONNS(=256) WS_THREADS(=4) DURATION(=15) WARMUP(=5), seconds
#
# Output: raw.tsv, report.md and the load tool's logs in the run directory.

source "$(dirname "$0")/abba.sh"

WS_PATH="${WS_PATH:-/ws}"
WS_TOOL="${WS_TOOL:-k6}"
WS_VUS="${WS_VUS:-100}"
WS_DURATION="${WS_DURATION:-10s}"
WS_TICK_MS="${WS_TICK_MS:-1}"
WS_BURST="${WS_BURST:-8}"
WS_LIFETIME_MS="${WS_LIFETIME_MS:-8000}"
WS_CONNS="${WS_CONNS:-256}"
WS_THREADS="${WS_THREADS:-4}"
DURATION="${DURATION:-15}"
WARMUP="${WARMUP:-5}"

AB_TOOL=ab_commit_ws
case "$WS_TOOL" in
    k6) AB_LANE="k6 websocket_echo_throughput.js vus=$WS_VUS d=$WS_DURATION tick=${WS_TICK_MS}ms x$WS_BURST $WS_PATH" ;;
    gcannon)
        AB_LANE="gcannon --ws -c $WS_CONNS -t $WS_THREADS -d ${DURATION}s $WS_PATH"
        command -v gcannon >/dev/null || _ab_die "gcannon is not installed (bench/load/install_gcannon.sh)"
        # One descriptor per connection on both sides of the loopback.
        ulimit -n 65536 || _ab_die "cannot raise the open-file limit for $WS_CONNS connections" ;;
    *) _ab_die "WS_TOOL must be k6 or gcannon" ;;
esac

ab_start_server() {
    ab_spawn "$PY" -m blackbull.cli bench.apps.native_app:app --bind "127.0.0.1:$PORT"
    ab_wait_ready "http://127.0.0.1:$PORT/plaintext"
}

_gcannon() {  # $1 = seconds; prints frames received per second
    "${PIN_LOAD[@]}" gcannon "http://127.0.0.1:$PORT$WS_PATH" --ws \
        -c "$WS_CONNS" -t "$WS_THREADS" -d "${1}s" -p 1 2>&1 \
        | tee -a "$AB_RUN/gcannon_$2.log" \
        | awk '/frames sent in/ { sub(/s,$/, "", $5); print ($6 > 0 && $5 > 0) ? $6 / $5 : "NaN"; found = 1 }
               END { if (!found) print "NaN" }'
}

ab_measure() {
    if [ "$WS_TOOL" = gcannon ]; then
        _gcannon "$WARMUP" "$1" > /dev/null
        _gcannon "$DURATION" "$1"
        return
    fi
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
