#!/usr/bin/env bash
# bench/peers/run_peer.sh — start one peer server for benchmarking.
#
# Usage:
#   bash bench/peers/run_peer.sh <stack> [port] [cert] [key]
#
# Base stacks:
#   blackbull   BlackBull (HTTP/1.1 + HTTP/2 + WS)
#   uvicorn     uvicorn   (HTTP/1.1 + WS)             — no HTTP/2
#   hypercorn   hypercorn (HTTP/1.1 + HTTP/2 + WS)
#   granian     granian   (HTTP/1.1 + HTTP/2 + WS)
#   daphne      daphne    (HTTP/1.1 + WS)             — no HTTP/2
#   nginx       reference floor (static GETs, HTTP/2, no upstream)
#
# Sprint 14 — topology / parser variants (blackbull, uvicorn, granian only):
#   <base>-cleartext   plain HTTP on $port (no TLS); isolates TLS cost
#   <base>-nginx       nginx HTTPS on $port + base on $((port+1)) cleartext
#                      via nginx_proxy.conf; isolates TLS+accept-loop offload
#   uvicorn-h11        TLS as default but force the pure-Python h11 parser
#
# Sprint 16 — multi-worker variants (blackbull only):
#   blackbull-w<N>     N worker processes (BB_WORKERS=N), SO_REUSEPORT
#                      single TLS port.  e.g. blackbull-w2, blackbull-w4.
#
# Defaults: port=8443, cert=tests/cert.pem, key=tests/key.pem
#
# Tuning: 1 worker, uvloop where available, h2 flow-control windows
# aligned to RFC default (65535) so library defaults don't confound.

set -e

stack="$1"
port="${2:-8443}"
cert="${3:-tests/cert.pem}"
key="${4:-tests/key.pem}"

if [ -z "$stack" ]; then
    echo "Usage: $0 <stack> [port] [cert] [key]" >&2
    echo "Bases: blackbull, uvicorn, hypercorn, granian, daphne, nginx" >&2
    echo "Variants (Sprint 14): <base>-cleartext, <base>-nginx, uvicorn-h11" >&2
    echo "Variants (Sprint 16): blackbull-w<N> (N workers, e.g. blackbull-w4)" >&2
    exit 1
fi

# --- Parse stack name: <base>[-<variant>] ---------------------------------
# Variants are mutually exclusive (one suffix per stack name).  workers
# defaults to 1; *-w<N> overrides it.
workers=1
case "$stack" in
    *-cleartext)
        base="${stack%-cleartext}"; variant="cleartext" ;;
    *-nginx)
        base="${stack%-nginx}";     variant="nginx" ;;
    *-h11)
        base="${stack%-h11}";       variant="h11" ;;
    *-w[0-9]|*-w[0-9][0-9])
        base="${stack%-w*}"
        workers="${stack##*-w}"
        variant="workers" ;;
    *)
        base="$stack";              variant="tls" ;;
esac

# When the stack is fronted by nginx, the base server binds one port up
# (8444 by default) and nginx listens on $port (8443).  Otherwise the base
# server binds directly on $port.
if [ "$variant" = "nginx" ]; then
    upstream_port=$((port + 1))
else
    upstream_port="$port"
fi

# Per-server cert/key arrays.  Empty for cleartext / nginx-fronted.  For
# h11 and multi-worker we keep TLS on (same as the default uvicorn /
# blackbull) — only the parser / worker count varies.
case "$variant" in
    tls|h11|workers)
        bb_tls_args=(--certfile "$cert" --keyfile "$key")
        uv_tls_args=(--ssl-certfile "$cert" --ssl-keyfile "$key")
        gr_tls_args=(--ssl-certificate "$cert" --ssl-keyfile "$key")
        ;;
    cleartext|nginx)
        bb_tls_args=()
        uv_tls_args=()
        gr_tls_args=()
        ;;
esac

# --- Helpers --------------------------------------------------------------

# Block until the upstream binds (only used in nginx-fronted mode).
wait_for_upstream() {
    local p="$1"
    for _ in $(seq 1 60); do
        if ss -ltn 2>/dev/null | grep -q ":$p "; then
            return 0
        fi
        sleep 0.5
    done
    echo "run_peer.sh: upstream did not bind on port $p" >&2
    return 1
}

# Finalize launch.  In nginx-fronted mode, background the base server and
# exec nginx; otherwise exec the base server directly.  LAUNCH_CMD must be
# set before calling.  When this script is itself backgrounded by the
# orchestrator (compare_servers.sh), the exec'd process becomes the
# "spawn pid" the orchestrator tracks.
finalize() {
    if [ "$variant" = "nginx" ]; then
        "${LAUNCH_CMD[@]}" &
        upstream_pid=$!
        if ! wait_for_upstream "$upstream_port"; then
            kill "$upstream_pid" 2>/dev/null || true
            exit 1
        fi
        local cert_abs key_abs conf
        cert_abs="$(readlink -f "$cert")"
        key_abs="$(readlink -f "$key")"
        conf="/tmp/bench-nginx-proxy.$$.conf"
        sed -e "s|__BB_CERT__|$cert_abs|" \
            -e "s|__BB_KEY__|$key_abs|" \
            -e "s|__BB_UPSTREAM_PORT__|$upstream_port|" \
            -e "s|__BB_LISTEN_PORT__|$port|" \
            bench/peers/nginx_proxy.conf > "$conf"
        nginx -c "$conf" -t 2>&1 || exit 1
        exec nginx -c "$conf"
    else
        exec "${LAUNCH_CMD[@]}"
    fi
}

# --- Dispatch -------------------------------------------------------------

case "$base" in
    blackbull)
        # Apples-to-apples logging: all peers disable per-request access
        # logging during benchmarks (uvicorn --no-access-log, hypercorn
        # default-off, granian --no-access-log, daphne /dev/null, nginx
        # access_log off).  Match by setting BB_ACCESS_LOG=0 here.
        # Production defaults (BB_ACCESS_LOG=1, BB_ASYNC_LOGGING=1)
        # remain unchanged in env.py — only this harness overrides.
        #
        # Sprint 11: the BlackBull console-script CLI lets us point at the
        # *same* shared peer app (bench.peers.asgi_app:app) that uvicorn /
        # hypercorn / granian / daphne load — no BlackBull-only bench/app.py
        # path any more.
        LAUNCH_CMD=(
            env BB_UVLOOP=1 "BB_WORKERS=$workers"
                BB_H2_INITIAL_WINDOW_SIZE=65535
                BB_H2_CONNECTION_WINDOW_SIZE=65535
                BB_H2_MAX_CONCURRENT_STREAMS=100
                BB_ACCESS_LOG=0
            blackbull bench.peers.asgi_app:app
                --bind "127.0.0.1:${upstream_port}"
                "${bb_tls_args[@]}"
        )
        finalize
        ;;
    uvicorn)
        # --http auto picks httptools if installed, else h11.
        # --loop auto picks uvloop if installed, else asyncio.
        # For best numbers install uvicorn[standard] (httptools + uvloop).
        # Sprint 14: uvicorn-h11 forces the pure-Python h11 parser to
        # isolate the C-parser advantage.
        if [ "$variant" = "h11" ]; then
            uv_http=h11
        else
            uv_http=auto
        fi
        LAUNCH_CMD=(
            uvicorn bench.peers.asgi_app:app
                --host 127.0.0.1 --port "$upstream_port"
                "${uv_tls_args[@]}"
                --loop auto --http "$uv_http" --workers 1
                --log-level warning --no-access-log
        )
        finalize
        ;;
    hypercorn)
        # bench/peers/hypercorn.toml carries the tuning; --bind on the CLI
        # overrides the toml's bind so we share $port with the other peers.
        # Sprint 14: hypercorn intentionally has no cleartext/nginx variants
        # (Sprint 13 ranked it well behind the matrix; the layer-attribution
        # A/B would not be informative).
        LAUNCH_CMD=(
            hypercorn --config bench/peers/hypercorn.toml
                --bind "127.0.0.1:${upstream_port}"
                bench.peers.asgi_app:app
        )
        finalize
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
            LAUNCH_CMD=(
                env PYTHONUNBUFFERED=1
                granian --interface asgi
                    --host 127.0.0.1 --port "$upstream_port"
                    "${gr_tls_args[@]}"
                    --workers 1 --loop uvloop
                    --http auto
                    --log-level warning --no-access-log
                    --log-config "$cfg"
                    bench.peers.asgi_app:app
            )
        else
            LAUNCH_CMD=(
                env PYTHONUNBUFFERED=1 stdbuf -oL -eL
                granian --interface asgi
                    --host 127.0.0.1 --port "$upstream_port"
                    "${gr_tls_args[@]}"
                    --workers 1 --loop uvloop
                    --http auto
                    --log-level warning --no-access-log
                    bench.peers.asgi_app:app
            )
        fi
        finalize
        ;;
    daphne)
        # daphne endpoint spec: ssl:<port>:privateKey=<key>:certKey=<cert>
        # Sprint 14: daphne intentionally has no cleartext/nginx variants
        # (same rationale as hypercorn).
        LAUNCH_CMD=(
            daphne --bind 127.0.0.1
                -e "ssl:${upstream_port}:privateKey=${key}:certKey=${cert}"
                --access-log /dev/null
                bench.peers.asgi_app:app
        )
        finalize
        ;;
    nginx)
        # Reference floor — pure C, static content only.  This is the
        # standalone-nginx peer (Sprint 13).  Sprint 14's nginx-fronted
        # variants use the same nginx binary but a different config
        # (nginx_proxy.conf) via the *-nginx suffix on other base stacks.
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
        echo "Unknown base stack: $base (from input '$stack')" >&2
        echo "Valid bases: blackbull, uvicorn, hypercorn, granian, daphne, nginx" >&2
        echo "Variants (blackbull|uvicorn|granian only): -cleartext, -nginx" >&2
        echo "Variants (uvicorn only): -h11" >&2
        echo "Variants (blackbull only):  -w<N>  (e.g. blackbull-w4)" >&2
        exit 1
        ;;
esac
