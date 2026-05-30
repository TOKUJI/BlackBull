# BlackBull

**From-scratch async ASGI 3.0 framework** with native HTTP/1.1, HTTP/2,
and WebSocket implementations — no `httptools`, no `uvicorn`, no
`hypercorn` underneath.  Pure-Python protocol stack, single
deployable, zero C-extension footprint outside the standard library.

[![PyPI](https://img.shields.io/pypi/v/blackbull.svg)](https://pypi.org/project/blackbull/)
[![Python](https://img.shields.io/pypi/pyversions/blackbull.svg)](https://pypi.org/project/blackbull/)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)

## Why BlackBull

- **One package, one process** — the framework *is* the server.  No
  separate ASGI runner; `app.run()` opens the socket and serves.
- **HTTP/1.1 + HTTP/2 + WebSocket** all implemented natively (RFC 9112
  for H/1, RFC 9113 for H/2, RFC 6455 + RFC 8441 for WebSocket).
- **Pure-Python identity** — no `httptools`, no `uvloop` dependency
  (uvloop available as an optional `[speed]` extra).
- **Conformance-tested** against `h2spec`, Autobahn, and a
  differential nginx fuzz corpus.
- **Modern Python** — requires 3.11+, full type hints, PEP 561 typed
  distribution.

## Install

```bash
pip install blackbull
pip install 'blackbull[compression]'   # add brotli + zstandard codecs
pip install 'blackbull[speed]'         # add uvloop event loop
pip install 'blackbull[reload]'        # add watchfiles for --reload
```

## Hello, world

```python
from blackbull import BlackBull

app = BlackBull()

@app.route(path='/')
async def hello():
    return "Hello, world!"

if __name__ == '__main__':
    app.run(port=8000)
```

Run it:

```bash
python app.py              # HTTP/1.1 on :8000
```

Or via the bundled CLI:

```bash
blackbull app:app --bind 0.0.0.0:8000
```

## Simplified handlers

Route handlers may return a `str`, `bytes`, `dict`, or `Response`;
path parameters are coerced to the annotation type:

```python
@app.route(path='/tasks/{task_id:int}')
async def get_task(task_id: int):
    return {"id": task_id, "title": "..."}
```

Drop down to full ASGI `(scope, receive, send)` whenever you need it
— routes accept either shape.

## TLS + HTTP/2

```python
app.run(port=8443, certfile='cert.pem', keyfile='key.pem')
```

ALPN negotiates `h2` automatically; HTTP/1.1 clients fall back via
the same socket.

## WebSocket

```python
from http import HTTPMethod
from blackbull.utils import Scheme

@app.route(path='/ws', methods=[HTTPMethod.GET], scheme=Scheme.websocket)
async def ws_echo(scope, receive, send):
    await receive()                              # websocket.connect
    await send({'type': 'websocket.accept'})
    while True:
        msg = await receive()
        if msg['type'] == 'websocket.disconnect':
            break
        if msg['type'] == 'websocket.receive':
            await send({'type': 'websocket.send',
                        'text': msg.get('text') or ''})
```

## Built-in middleware

Compose via `app.use(...)` or per-route `middlewares=[...]`:

| Middleware       | What it does |
|------------------|---|
| `Compression`    | Negotiates `br` / `zstd` / `gzip` from `Accept-Encoding` |
| `StaticFiles`    | Serves files from a directory under a URL prefix |
| `Cache`          | Per-worker LRU + ETag / `Cache-Control` honouring |
| `Session`        | Signed-cookie sessions (HMAC-SHA256) |
| `CORS`           | Preflight + actual-request header injection |
| `TrustedProxy`   | Rewrites `scope['client']` / `scope['scheme']` from proxy headers |

## OpenAPI / Swagger UI

```python
app.enable_openapi()   # publishes /openapi.json and /docs
```

Auto-generates an OpenAPI 3.1 spec from route signatures, path-param
converters, docstrings, and `@dataclass` annotations on body
parameters.  Dataclass-typed bodies are also deserialized at runtime
— `async def h(body: CreateTask): ...` receives a constructed
instance, no manual `json.loads`.

## Examples

| Example | Demonstrates |
|---|---|
| [`examples/SimpleTaskManager/`](examples/SimpleTaskManager/) | REST API + HTML UI, middleware pipeline, route groups, SQLite, Bearer token auth |
| [`examples/ChatServer/`](examples/ChatServer/) | WebSocket, SSE, long polling side by side; Session + Compression + custom auth |
| [`examples/typed_routes_ok.py`](examples/typed_routes_ok.py) | `{param:converter}` syntax, `url_path_for` |

## Documentation

- **Guide**: [`docs/guide.md`](docs/guide.md)
- **Architecture**: [`docs/ActorDesign.md`](docs/ActorDesign.md)
- **Changelog**: [`CHANGELOG.md`](CHANGELOG.md)

## Versioning

BlackBull uses [ZeroVer](https://0ver.org/) prior to a 1.0 commitment.
`MINOR` advances at each sprint close; `PATCH` is for bug fixes and
harness work between sprints.  See [`CHANGELOG.md`](CHANGELOG.md) for
the full release history.

## License

[Apache License 2.0](LICENSE) — © TOKUJI.
