# Running BlackBull

BlackBull has three entry points, in increasing order of how
"production-shaped" they are.  All three resolve to the same
underlying `serve()` function — pick whichever shape fits the
deployment surface.

## 1. `app.run(...)` — in-Python, single command

```python
# Single-worker, plain HTTP/1.1 on :8000
app.run(port=8000)

# HTTPS + HTTP/2 via ALPN on :8443
app.run(port=8443, certfile='cert.pem', keyfile='key.pem')

# Multi-worker (pre-forks N workers; master holds the listening sockets)
app.run(port=8443, certfile='cert.pem', keyfile='key.pem', workers=4)

# Development: hot-reload when *.py files change
# Requires the [reload] extra: pip install 'blackbull[reload]'
app.run(port=8000, reload=True)
```

`app.run()` is **synchronous** — it internalises `asyncio.run`
so callers write `app.run(port=8000)` rather than
`asyncio.run(app.run(...))`.  `reload=True` and `workers > 1`
both trigger the master-process path (long-lived supervisor
around the worker pool).

Full signature:

```python
def run(port=0, certfile=None, keyfile=None,
        workers=None, unix_path=None, inherited_fd=None,
        max_connections=None, stream_queue_depth=None,
        ws_queue_depth=None, reload=False, reload_paths=None):
    ...
```

## 2. `blackbull` CLI — useful for systemd, Docker, PaaS

```bash
# Same flags as app.run kwargs
blackbull myapp:app \
    --bind 0.0.0.0:8443 \
    --certfile cert.pem --keyfile key.pem \
    --workers 4

# Auto-reload via the CLI
blackbull myapp:app --bind 127.0.0.1:8000 --reload
```

The CLI accepts any `module:attribute` import path; the
attribute may be a `BlackBull` instance *or* a plain ASGI
callable.  See `blackbull --help` for the full flag list and
[Configuration](../guide/configuration.md) for how flags compose
with environment variables and TOML config files.

## 3. Any external ASGI 3.0 server

BlackBull instances are valid ASGI 3.0 callables, so they work
under any ASGI server:

```bash
uvicorn myapp:app --port 8000
hypercorn myapp:app --bind 0.0.0.0:8000
granian --interface asgi myapp:app
```

You lose BlackBull's native HTTP/2 and WebSocket implementations
(the surrounding server's stack is used instead), but the
application code is portable as-is.

## Embedded use under an existing event loop

To run BlackBull inside an existing event loop, or to pre-bind a
socket before forking a test subprocess, instantiate
`ASGIServer` directly:

```python
import asyncio
from blackbull.server import ASGIServer

server = ASGIServer(app, certfile='cert.pem', keyfile='key.pem')
server.open_socket(8443)
await server.run()
```

`open_socket(port=0)` binds to a kernel-assigned port and
exposes it as `server.port` — the same pattern the
[testing fixture](../guide/testing.md#fixture-ephemeral-port-server)
uses.

## Picking the deployment shape

| Shape | When |
|---|---|
| `python myapp.py` (calls `app.run()`) | Development, one-shot scripts, tutorial-style apps |
| `blackbull myapp:app …` | systemd, Docker, PaaS — anywhere the deployment surface is a CLI |
| External ASGI server | When the surrounding deployment dictates uvicorn / granian / hypercorn for tooling reasons |
| `ASGIServer` direct | Embedded inside another asyncio program, or when fine-grained socket / TLS control is needed (e.g. mTLS — see [TLS](tls.md#mtls)) |

## Next

- [Workers](workers.md) — pre-fork multiprocessing, uvloop,
  abuse defences, production checklist.
- [TLS](tls.md) — HTTPS, HTTP/2 via ALPN, self-signed certs,
  mTLS.
- [Behind nginx](behind-nginx.md) — terminating TLS upstream,
  trusted-proxy headers.
- [Unix and fd inheritance](unix-and-fd.md) — `AF_UNIX`,
  systemd socket activation.
- [Hot reload](hot-reload.md) — `--reload` for development.
