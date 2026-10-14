#!/usr/bin/env bash
# bench/ab/ab_commit_h2.sh — A/B two BlackBull commits on HTTP/2 (h2load).
#
# The ab_commit.sh discipline (abba.sh) on an h2c lane: h2load floods
# concurrent streams at bench/ab/h2load/app.py and its aggregate req/s is the
# measured axis.
#
#   REF_BASE=c93de3b ROUNDS=8 bash bench/ab/ab_commit_h2.sh
#
# Lane env:
#   URL_PATH(=/1kb)  also the readiness probe
#   H2_CONNS(=32) H2_STREAMS(=16) H2_N(=100000) H2_WARMUP(=10000, discarded)
#   H2_URIS     file of request URIs for h2load -i; each origin is replaced by this server's
#   H2_HEADER   one extra request header, e.g. 'Accept-Encoding: gzip'
#   APP / APP_PYTHONPATH  serve this app through the blackbull CLI instead
#   TLS_CERT / TLS_KEY    with APP: serve HTTPS (ALPN h2)
#
# Output: raw.tsv, report.md and h2load logs in the run directory.

source "$(dirname "$0")/abba.sh"

URL_PATH="${URL_PATH:-/1kb}"
H2_CONNS="${H2_CONNS:-32}"
H2_STREAMS="${H2_STREAMS:-16}"
H2_N="${H2_N:-100000}"
H2_WARMUP="${H2_WARMUP:-10000}"
BASE_URL="$([ -n "${TLS_CERT:-}" ] && echo https || echo http)://127.0.0.1:${PORT}"
if [ -n "${APP:-}" ]; then
    SERVER_CMD=("$PY" -m blackbull.cli "$APP" --bind "127.0.0.1:$PORT"
                ${TLS_CERT:+--certfile "$TLS_CERT" --keyfile "$TLS_KEY"})
else
    SERVER_CMD=("$PY" bench/ab/h2load/app.py --no-tls --port "$PORT")
fi

AB_TOOL=ab_commit_h2
AB_LANE="h2load -c $H2_CONNS -m $H2_STREAMS -n $H2_N ${H2_URIS:-$BASE_URL$URL_PATH} ${H2_HEADER:+-H '$H2_HEADER'}"

ab_start_server() {
    PYTHONPATH="${APP_PYTHONPATH:-}" ab_spawn "${SERVER_CMD[@]}"
    ab_wait_ready "$BASE_URL$URL_PATH"
}

ab_measure() {
    local target=("$BASE_URL$URL_PATH")
    if [ -n "${H2_URIS:-}" ]; then
        sed -E "s#^[a-z]+://[^/]+##; s#^#${BASE_URL}#" "$H2_URIS" > "$BENCH_TMP/uris.txt"
        target=(-i "$BENCH_TMP/uris.txt")
    fi
    [ -z "${H2_HEADER:-}" ] || target+=(-H "$H2_HEADER")
    "${PIN_LOAD[@]}" h2load -c "$H2_CONNS" -m "$H2_STREAMS" -n "$H2_WARMUP" "${target[@]}" \
        > /dev/null 2>&1
    "${PIN_LOAD[@]}" h2load -c "$H2_CONNS" -m "$H2_STREAMS" -n "$H2_N" "${target[@]}" \
        > "$AB_RUN/h2_$1.txt" 2>&1
    awk '/finished in/ {print $4}' "$AB_RUN/h2_$1.txt"
}

abba_main
