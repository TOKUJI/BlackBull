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

**[Live demo →](https://blackbull.alwaysdata.net/)** — BlackBull running in
production on a free-tier host, no external server in front of it.

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
- **Declare, don't plumb.** Handlers name what they need — path
  params, query params, the body, a `Request` view, or a
  `Depends(get_db)` resource with teardown after the response — and
  the router resolves it all when the route is *registered*.  A
  handler that uses none of it compiles to the same bare wrapper:
  zero per-request cost for features you didn't ask for.
- **Break things on purpose.** The same protocol code that serves
  real traffic can drive a programmable misbehaving client or
  server.  Test your own HTTP/2 client against half-closed streams,
  exhausted windows, and illegal SETTINGS — in CI.
- **Multi-protocol, one process.** A pure-Python MQTT 5 broker and
  gRPC ride beside HTTP/2 and WebSocket on the same runtime;
  extensions add new protocols without touching the core — still no
  C extension.  That combination is an
  [edge inference API server](https://TOKUJI.github.io/BlackBull/guide/edge-inference/)
  in one `python app.py`: SSE token streaming with HTTP/2
  multiplexing for clients, MQTT ingest and `$share/…` work queues
  for devices — on any box CPython runs on, ARM included.
- **Actor-model internals.** Connection-level isolation without a
  single shared lock: a `ConnectionActor` spawns a per-connection
  protocol actor whose inbox loop owns its own state.  The same
  message-passing concurrency runs your HTTP routes, the MQTT broker,
  and the gRPC handlers — one model across every protocol.
- **RFC-grade correctness.** Passes the same external conformance
  suites used to validate nginx and Envoy (`h2spec`, Autobahn).  First
  Python framework with native **HTTP QUERY** (RFC 10008) support — the
  new safe, idempotent, cacheable method that carries a request body,
  with `Accept-Query` content negotiation.
- **Typed throughout.** Your editor and `mypy` / `pyright` see
  every parameter; PEP 561 typed distribution.

## Install

```bash
pip install blackbull
pip install 'blackbull[compression]'      # add brotli + zstandard codecs
pip install 'blackbull[speed]'            # add uvloop event loop
pip install 'blackbull[reload]'           # add watchfiles for --reload
pip install 'blackbull[fault-injection]'  # add cryptography + httpx for the toolkit
pip install 'blackbull[mqtt]'             # MQTT 5 broker extension
pip install 'blackbull[grpc]'             # gRPC over HTTP/2 (all four RPC shapes)
pip install 'blackbull[protobuf]'         # + protobuf servicers, reflection, health, rich errors
```

## Simplified handlers

Route handlers may return a `str`, `bytes`, `dict`, or `Response`;
path parameters are coerced to the annotation type:

```python
@app.route(path='/tasks/{task_id:int}')
async def get_task(task_id: int):
    return {"id": task_id, "title": "..."}
```

Need more than the URL? Declare a `Request` parameter — headers,
cookies, client, and `body()`/`json()`/`text()`, injected only for
handlers that ask for it:

```python
@app.route(path='/notes/{note_id:int}', methods=[HTTPMethod.POST])
async def create_note(note_id: int, request: Request):
    return {"id": note_id, "note": await request.json()}
```

Query params and per-request resources are declared the same way —
everything is resolved when the route is registered, so handlers that
don't use a feature pay nothing for it at request time:

```python
from blackbull import Depends

async def get_db():
    conn = await pool.acquire()
    try:
        yield conn                 # injected value
    finally:
        await pool.release(conn)  # runs after the response is sent

@app.route(path='/search')
async def search(q: str, page: int = 1, db=Depends(get_db)):
    return await db.find(q, page=page)   # /search?q=bull&page=2
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

## Beyond HTTP — the Non-ASGI bridge

One process, more than one protocol. Extensions attach non-HTTP
protocols through a single seam — `app.add_extension(...)` — while the
HTTP core stays protocol-agnostic. The flagship is a **pure-Python
MQTT 5 broker**: CONNECT / SUBSCRIBE / PUBLISH at QoS 0–2, retained
messages, Last-Will, and shared subscriptions (`$share/…` work queues)
on the standard `:1883` port — or over TLS with
`MQTTExtension(port=8883, tls=True)` — beside your HTTP routes, no
Mosquitto sidecar, no C extension.

```python
from blackbull import BlackBull
from blackbull.mqtt import MQTTExtension, Message

app = BlackBull()
mqtt = app.add_extension(MQTTExtension(port=1883))

@mqtt.on_message(topic='sensors/{room}/temperature')
async def on_temp(msg: Message, room: str):
    print(room, msg.payload.decode())   # {room} captured like an HTTP path param

app.run(port=8000)   # HTTP on 8000, MQTT on 1883
```

Need a protocol BlackBull doesn't ship? `@app.raw_handler` hands you the
raw reader/writer for a port. See
[`docs/guide/mqtt.md`](https://github.com/TOKUJI/BlackBull/blob/master/docs/guide/mqtt.md)
and [`docs/guide/raw-protocols.md`](https://github.com/TOKUJI/BlackBull/blob/master/docs/guide/raw-protocols.md).

Deeper dives:
[Architecture](https://github.com/TOKUJI/BlackBull/blob/master/docs/about/architecture.md) ·
[Is BlackBull right for you?](https://github.com/TOKUJI/BlackBull/blob/master/docs/getting-started/why-blackbull.md) ·
[Known limitations](https://github.com/TOKUJI/BlackBull/blob/master/KNOWN_LIMITATIONS.md)

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

For the MQTT broker there is a messaging counterpart: `AsyncAPIExtension`
publishes an AsyncAPI 3.0 document for your `@mqtt.on_message` taps at
`/asyncapi.json` (with an HTML viewer at `/asyncapi`).  See
[`docs/guide/mqtt.md`](https://github.com/TOKUJI/BlackBull/blob/master/docs/guide/mqtt.md).

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
| [`examples/ChatServer/`](examples/ChatServer/) | WebSocket, SSE, long polling side by side; `blackbull-session` + Compression + custom auth |
| [`examples/mqtt_broker.py`](examples/mqtt_broker.py) | MQTT 5 broker beside HTTP; `on_message` taps with `{capture}` topic params |
| [`examples/translation_hub.py`](examples/translation_hub.py) | Protocol translation hub — MQTT → WebSocket, MQTT → SSE, REST → gRPC in one process |
| [`examples/edge_inference.py`](examples/edge_inference.py) | Edge inference API server — SSE token streaming (browser demo included) + MQTT telemetry + `$share` work queue, dependency-free fake model |
| [`examples/grpc_greeter.py`](examples/grpc_greeter.py) | Canonical gRPC Greeter speaking real protobuf — works with stock `grpcurl` / `grpcio` clients unmodified |
| [`examples/typed_routes_ok.py`](examples/typed_routes_ok.py) | `{param:converter}` syntax, `url_path_for` |
| [`examples/scenario_h1_fault_injection.py`](examples/scenario_h1_fault_injection.py) | HTTP/1.1 fault scenarios driven against stdlib `http.server` |
| [`examples/scenario_h2_fault_injection.py`](examples/scenario_h2_fault_injection.py) | HTTP/2 fault scenarios served against httpx |
| [`examples/request_object.py`](examples/request_object.py) | Opt-in `Request` context object — headers, cookies, client, `body()`/`json()`/`text()` |
| [`examples/dependency_injection.py`](examples/dependency_injection.py) | `Depends` on a pseudo DB pool — per-request acquire/release with teardown after the response, query params, `use_cache` sharing |

## Documentation

- **Guide**: [`docs/guide/index.md`](https://github.com/TOKUJI/BlackBull/blob/master/docs/guide/index.md)
- **Architecture & design rationale**: [`docs/about/architecture.md`](https://github.com/TOKUJI/BlackBull/blob/master/docs/about/architecture.md)
- **Implementation internals**: [`docs/about/internals.md`](https://github.com/TOKUJI/BlackBull/blob/master/docs/about/internals.md)
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
