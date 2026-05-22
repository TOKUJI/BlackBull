#!/usr/bin/env bash
# bench/peers/run_peer.sh — start one peer server for benchmarking.
#
# Usage:
#   bash bench/peers/run_peer.sh <server> [port] [cert] [key]
#
# Servers:
#   blackbull   BlackBull (HTTP/1.1 + HTTP/2 + WS)
#   uvicorn     uvicorn   (HTTP/1.1 + WS)             — no HTTP/2
#   hypercorn   hypercorn (HTTP/1.1 + HTTP/2 + WS)
#   granian     granian   (HTTP/1.1 + HTTP/2 + WS)
#   daphne      daphne    (HTTP/1.1 + WS)             — no HTTP/2
#
# Defaults: port=8443, cert=tests/cert.pem, key=tests/key.pem
#
# Tuning: 1 worker, uvloop where available, h2 flow-control windows
# aligned to RFC default (65535) so library defaults don't confound.

set -e

server="$1"
port="${2:-8443}"
cert="${3:-tests/cert.pem}"
key="${4:-tests/key.pem}"

if [ -z "$server" ]; then
    echo "Usage: $0 <blackbull|uvicorn|hypercorn|granian|daphne> [port] [cert] [key]" >&2
    exit 1
fi

case "$server" in
    blackbull)
        # Apples-to-apples logging: all peers disable per-request access
        # logging during benchmarks (uvicorn --no-access-log, hypercorn
        # default-off, granian --no-access-log, daphne /dev/null, nginx
        # access_log off).  Match by setting BB_ACCESS_LOG=0 here.
        # Production defaults (BB_ACCESS_LOG=1, BB_ASYNC_LOGGING=1)
        # remain unchanged in env.py — only this harness overrides.
        exec env BB_UVLOOP=1 BB_WORKERS=1 \
            BB_H2_INITIAL_WINDOW_SIZE=65535 \
            BB_H2_CONNECTION_WINDOW_SIZE=65535 \
            BB_H2_MAX_CONCURRENT_STREAMS=100 \
            BB_ACCESS_LOG=0 \
            python bench/app.py --port "$port" --cert "$cert" --key "$key"
        ;;
    uvicorn)
        # --http auto picks httptools if installed, else h11.
        # --loop auto picks uvloop if installed, else asyncio.
        # For best numbers install uvicorn[standard] (httptools + uvloop).
        exec uvicorn bench.peers.asgi_app:app \
            --host 127.0.0.1 --port "$port" \
            --ssl-certfile "$cert" --ssl-keyfile "$key" \
            --loop auto --http auto --workers 1 \
            --log-level warning --no-access-log
        ;;
    hypercorn)
        # bench/peers/hypercorn.toml carries the tuning; --bind on the CLI
        # overrides the toml's bind so we share $port with the other peers.
        exec hypercorn --config bench/peers/hypercorn.toml \
            --bind "127.0.0.1:${port}" \
            bench.peers.asgi_app:app
        ;;
    granian)
        # GRANIAN_LOG_TARGET (optional): absolute path for granian's
        # FileHandler-based log. When set, we generate a dictConfig that
        # routes granian's own logger straight to the file, bypassing the
        # shell pipe entirely. Falls back to stdout (buffered through
        # stdbuf/PYTHONUNBUFFERED) when not set.
        if [ -n "${GRANIAN_LOG_TARGET:-}" ]; then
            cfg="/tmp/granian_log_$$.json"
            mkdir -p "$(dirname "$GRANIAN_LOG_TARGET")"
            python3 -c "
import json, sys
cfg = {
  'version': 1, 'disable_existing_loggers': False,
  'formatters': {'d': {'format': '[%(asctime)s] [%(levelname)s] %(name)s: %(message)s'}},
  'handlers': {'f': {'class': 'logging.FileHandler',
                     'filename': sys.argv[1], 'mode': 'w', 'formatter': 'd'}},
  'root': {'level': 'WARNING', 'handlers': ['f']},
  'loggers': {
    '_granian':       {'level': 'WARNING', 'handlers': ['f'], 'propagate': False},
    'granian.access': {'level': 'WARNING', 'handlers': ['f'], 'propagate': False},
    'granian.server': {'level': 'WARNING', 'handlers': ['f'], 'propagate': False},
  }}
json.dump(cfg, open(sys.argv[2], 'w'))
" "$GRANIAN_LOG_TARGET" "$cfg"
            exec env PYTHONUNBUFFERED=1 \
                granian --interface asgi \
                --host 127.0.0.1 --port "$port" \
                --ssl-certificate "$cert" --ssl-keyfile "$key" \
                --workers 1 --loop uvloop \
                --http auto \
                --log-level warning --no-access-log \
                --log-config "$cfg" \
                bench.peers.asgi_app:app
        else
            exec env PYTHONUNBUFFERED=1 stdbuf -oL -eL \
                granian --interface asgi \
                --host 127.0.0.1 --port "$port" \
                --ssl-certificate "$cert" --ssl-keyfile "$key" \
                --workers 1 --loop uvloop \
                --http auto \
                --log-level warning --no-access-log \
                bench.peers.asgi_app:app
        fi
        ;;
    daphne)
        # daphne endpoint spec: ssl:<port>:privateKey=<key>:certKey=<cert>
        exec daphne --bind 127.0.0.1 \
            -e "ssl:${port}:privateKey=${key}:certKey=${cert}" \
            --access-log /dev/null \
            bench.peers.asgi_app:app
        ;;
    nginx)
        # Reference floor — pure C, static content only.
        if [ "$port" != "8443" ]; then
            echo "WARNING: nginx peer config is hardcoded to 8443; ignoring port=$port" >&2
        fi
        # Fixture files for the large-body GET routes.
        for sz in 1024:1kb 16000:16kb 65536:64kb 1048576:1mb; do
            size="${sz%%:*}"; tag="${sz##*:}"
            f="/tmp/bench_${tag}.bin"
            [ -f "$f" ] || head -c "$size" /dev/urandom > "$f"
        done
        # Substitute absolute cert/key paths into a /tmp copy of the conf.
        # (nginx resolves relative paths against its compiled-in prefix,
        # so the conf needs absolute paths.)
        cert_abs="$(readlink -f "$cert")"
        key_abs="$(readlink -f "$key")"
        conf="/tmp/bench-nginx.$$.conf"
        sed -e "s|__BB_CERT__|$cert_abs|" -e "s|__BB_KEY__|$key_abs|" \
            bench/peers/nginx.conf > "$conf"
        nginx -c "$conf" -t 2>&1 || exit 1
        exec nginx -c "$conf"
        ;;
    *)
        echo "Unknown server: $server" >&2
        echo "Valid: blackbull, uvicorn, hypercorn, granian, daphne, nginx" >&2
        exit 1
        ;;
esac
