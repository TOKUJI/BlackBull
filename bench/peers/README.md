# Peer benchmark harness

Minimal `/ping` apps for ad-hoc local comparison of BlackBull against other
Python ASGI HTTP/2 servers.

**This is a work in progress.**  Coverage is limited (one endpoint, one host,
one load generator), the Hypercorn config is our own tuning, and we have not
sought review from upstream maintainers.  What you find here is the current
best effort, not a polished or peer-reviewed benchmark.

## Files

| Path | Purpose |
|---|---|
| `quart_app.py` | Quart `/ping` returning `b'pong'` |
| `starlette_app.py` | Starlette `/ping` returning `Response(b'pong')` |
| `fastapi_app.py` | FastAPI `/ping` returning `Response(b'pong')` |
| `hypercorn.toml` | Hypercorn config — see comments inside the file |
| `k6_one.sh` | Run one k6 stress pass and print `req/s | p50 | p95 | p99 | error%` |

## Install

```bash
pip install hypercorn quart starlette fastapi
```

## Running

Bring up one server at a time on port 8443, measure it, kill it, run the next.

```bash
# BlackBull (override window sizes to match Hypercorn's h2 library default)
BB_UVLOOP=1 BB_WORKERS=1 \
    BB_H2_INITIAL_WINDOW_SIZE=65535 \
    BB_H2_CONNECTION_WINDOW_SIZE=65535 \
    BB_H2_MAX_CONCURRENT_STREAMS=100 \
    python bench/app.py --port 8443 \
        --cert tests/cert.pem --key tests/key.pem

# Hypercorn (Quart / Starlette / FastAPI)
hypercorn --config bench/peers/hypercorn.toml bench.peers.quart_app:app
hypercorn --config bench/peers/hypercorn.toml bench.peers.starlette_app:app
hypercorn --config bench/peers/hypercorn.toml bench.peers.fastapi_app:app

# Measurements
h2load -n 50000 -c 50 -m 1  https://localhost:8443/ping
h2load -n 90000 -c 50 -m 10 https://localhost:8443/ping
h2load -n 90000 -c 50 -m 50 https://localhost:8443/ping
bash bench/peers/k6_one.sh <stack-name>
```
