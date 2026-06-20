# BlackBull

**BlackBull** is a Python ASGI 3.0 web framework for developers who
want one `pip install`, zero C compilers, and the ability to
programmatically test their HTTP clients and servers against
deliberate protocol misbehaviour.

[![PyPI](https://img.shields.io/pypi/v/blackbull.svg)](https://pypi.org/project/blackbull/)
[![Python](https://img.shields.io/pypi/pyversions/blackbull.svg)](https://pypi.org/project/blackbull/)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
[![RFC conformance](https://github.com/TOKUJI/BlackBull/actions/workflows/conformance.yml/badge.svg)](https://github.com/TOKUJI/BlackBull/actions/workflows/conformance.yml)
[![Benchmarked by HttpArena](https://cdn.jsdelivr.net/gh/MDA2AV/httparena-badge/wordmark.svg)](https://www.http-arena.com/leaderboard/)

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

```bash
$ python app.py &
$ curl -i http://localhost:8000/
HTTP/1.1 200 OK
content-type: text/plain; charset=utf-8
content-length: 13

Hello, world!
```

## Why BlackBull

- **Zero ceremony.** `app.run()` is the entire deploy story — or
  `blackbull serve ./public` for a static site with ETag + HTTP/2 and
  no code at all.  No separate ASGI runner, no YAML config, no
  `gunicorn` class path.
- **Readable stack.** Every byte on the wire passes through Python
  you can step through with `pdb`.  No C extensions to debug.
- **Break things on purpose.** The same protocol code that serves
  real traffic can drive a programmable misbehaving client or
  server.  Test your own HTTP/2 client against half-closed streams,
  exhausted windows, and illegal SETTINGS — in CI.
- **RFC-grade correctness.** Passes the same external conformance
  suites used to validate nginx and Envoy (`h2spec`, Autobahn).
- **Typed throughout.** Your editor and `mypy` / `pyright` see
  every parameter; PEP 561 typed distribution.

## Install

```bash
pip install blackbull
pip install 'blackbull[compression]'      # add brotli + zstandard codecs
pip install 'blackbull[speed]'            # add uvloop event loop
pip install 'blackbull[reload]'           # add watchfiles for --reload
pip install 'blackbull[fault-injection]'  # add cryptography + httpx for the toolkit
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

## Event API

Two decorators cover both lifecycle hooks and per-request behaviour
— observation (`@app.on`, fire-and-forget) and interception
(`@app.intercept`, synchronous, may short-circuit):

```python
@app.on_startup
async def warm_caches():
    ...

@app.intercept('request_received')
async def auth(scope, receive, send, call_next):
    # raise to abort, or skip call_next to short-circuit
    await call_next(scope, receive, send)

@app.on('request_completed')
async def emit_metrics(event):
    metrics.increment('requests', status=event['status'])
```

Events: `app_startup`, `app_shutdown`, `request_received`,
`before_handler`, `request_completed`, `websocket_message`.  `@app.on`
isolates exceptions per observer; `@app.intercept` is part of the
request path and can deny / rewrite / pass through.  See
[`docs/guide/events.md`](https://github.com/TOKUJI/BlackBull/blob/master/docs/guide/events.md)
for the full event catalogue and `detail` payloads.

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
| `websocket`      | Auto-accepts the WebSocket handshake and emits `websocket.close` after the handler returns |

## OpenAPI / Swagger UI

```python
app.enable_openapi()   # publishes /openapi.json and /docs
```

Auto-generates an OpenAPI 3.1 spec from route signatures, path-param
converters, docstrings, and `@dataclass` annotations on body
parameters.  Dataclass-typed bodies are also deserialized at runtime
— `async def h(body: CreateTask): ...` receives a constructed
instance, no manual `json.loads`.

## Fault injection

BlackBull's single most distinctive feature: a programmable
deliberate-misbehaviour toolkit you can point at your own HTTP/2
client (or proxy, or middleware) directly from a pytest suite.

```python
import pytest
from blackbull.fault_injection import H2FaultServer, make_self_signed_h2_context
from blackbull.fault_injection.catalogue import half_closed_stream_no_data

@pytest.mark.asyncio
async def test_my_client_handles_half_closed_streams():
    ssl_ctx = make_self_signed_h2_context()
    async with H2FaultServer(
        scenario=half_closed_stream_no_data(), ssl_context=ssl_ctx,
    ) as srv:
        # Your client must time out or RST_STREAM rather than block
        # forever when the server sends HEADERS without END_STREAM.
        with pytest.raises(TimeoutError):
            await my_h2_client.get(srv.url, timeout=1.0)
```

The catalogue ships four spec-grade categories (half-closed streams,
exhausted flow-control windows, illegal SETTINGS, weird frame
sequences); the symmetric HTTP/1.1 client side drives a real server
through trickled headers, partial requests, and abrupt RST.  See
[`docs/guide/fault_injection.md`](https://github.com/TOKUJI/BlackBull/blob/master/docs/guide/fault_injection.md)
for the full tutorial.

## Early Alpha

> ⚠ **Early Alpha** — The API may change between MINOR versions.
> See [Conformance](https://github.com/TOKUJI/BlackBull/blob/master/docs/about/conformance.md)
> for protocol-level test coverage and [Known Limitations](https://github.com/TOKUJI/BlackBull/blob/master/KNOWN_LIMITATIONS.md)
> for the explicit list of behaviours to expect before adopting.

## Examples

| Example | Demonstrates |
|---|---|
| [`examples/SimpleTaskManager/`](examples/SimpleTaskManager/) | REST API + HTML UI, middleware pipeline, route groups, SQLite, Bearer token auth |
| [`examples/ChatServer/`](examples/ChatServer/) | WebSocket, SSE, long polling side by side; Session + Compression + custom auth |
| [`examples/typed_routes_ok.py`](examples/typed_routes_ok.py) | `{param:converter}` syntax, `url_path_for` |
| [`examples/scenario_h1_fault_injection.py`](examples/scenario_h1_fault_injection.py) | HTTP/1.1 fault scenarios driven against stdlib `http.server` |
| [`examples/scenario_h2_fault_injection.py`](examples/scenario_h2_fault_injection.py) | HTTP/2 fault scenarios served against httpx |

## Documentation

- **Guide**: [`docs/guide/index.md`](https://github.com/TOKUJI/BlackBull/blob/master/docs/guide/index.md)
- **Architecture**: [`docs/about/internals.md`](https://github.com/TOKUJI/BlackBull/blob/master/docs/about/internals.md)
- **Changelog**: [`CHANGELOG.md`](https://github.com/TOKUJI/BlackBull/blob/master/CHANGELOG.md)

## Versioning

BlackBull uses [ZeroVer](https://0ver.org/) prior to a 1.0 commitment.
`MINOR` advances at each sprint close; `PATCH` is for bug fixes and
harness work between sprints.  See [`CHANGELOG.md`](https://github.com/TOKUJI/BlackBull/blob/master/CHANGELOG.md) for
the full release history.

## License

[Apache License 2.0](LICENSE) — © TOKUJI.

## Next steps

→ **[Read the Guide](https://tokuji.github.io/BlackBull/guide/)** —
  routing, middleware, WebSockets, HTTP/2, and more.

→ **[Browse the examples](examples/)** — copy-pasteable starting
  points for REST APIs, chat servers, and SSE streams.

→ **[Star on GitHub](https://github.com/TOKUJI/BlackBull)** — every
  star helps the project grow.
