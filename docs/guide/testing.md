# Testing

BlackBull threads a typed `Connection` end to end and keeps the ASGI
`scope` dict at two boundaries only.  That shapes the test tooling:
there is more than one client here, each drives a different layer,
and **a defect on one layer is invisible to the others**.

Start from what you are asserting:

| You are asserting… | Reach for | Why |
|---|---|---|
| Routing, middleware, handlers, DI, events | `blackbull.testing.native` | Calls `app(conn, receive, send)` — the entry point every production request takes.  No socket, no protocol actor. |
| Anything the wire decides — framing, keep-alive, `HEAD`, chunking, connection close | `NativeTestServer` | BlackBull's own server on a loopback port.  Accept → parse → `Connection` → dispatch → bytes. |
| That ASGI compatibility still works | `blackbull.testing.TestClient` | The `as_scope()` / `from_scope()` round-trip, driven the way uvicorn drives it. |
| Cross-protocol behaviour — TLS, ALPN, HTTP/2 framing, WebSocket fragments | BlackBull's own clients + an ephemeral port | Full protocol negotiation against a real server. |
| One function, no framework | Direct handler / middleware calls | Stub `receive` / `send`, no routing, no transport. |

`TestClient` used to be the default recommendation.  It is now the
**ASGI-boundary instrument**, not the everyday one — the reasoning is in
[Choosing between `native` and `TestClient`](#choosing-between-native-and-testclient).

## Setup

Install the testing extras for `pytest`, `pytest-asyncio`,
`httpx[http2]`, `websockets`, and `hypothesis`:

```bash
pip install -e '.[testing]'
```

A minimal `pytest.ini`:

```ini
[pytest]
asyncio_mode = strict
```

Strict mode requires every async test to carry
`@pytest.mark.asyncio` explicitly — matching BlackBull's own
suite — so tests don't accidentally run in the wrong loop
shape.  Tests written against the synchronous clients
(`TestClient`, `NativeClient`) don't need this mark.

## Quick start with `native`

`blackbull.testing.native` builds a `Connection` and calls
`app(conn, receive, send)` — the same call BlackBull's protocol
actors make.  Everything from `Connection` inward runs for real:
the dispatcher, the middleware chain, the router, dependency
injection, events, and response serialisation.

```python
import pytest
from blackbull import BlackBull
from blackbull.testing import native

app = BlackBull()


@app.route(path='/hello')
async def hello():
    return 'hi'


@pytest.mark.asyncio
async def test_hello():
    resp = await native.get(app, '/hello')
    assert resp.status == 200
    assert resp.body == b'hi'
```

The helpers are `get` / `head` / `options` / `post` / `put` /
`patch` / `delete`, plus `request` for full control.  A response is
a `NativeTestResponse` — `status`, `headers` (a `Headers`), `body`, and
`.json()` / `.text()` convenience readers:

```python
resp = await native.post(app, '/tasks', json={'name': 'write tests'})
assert resp.status == 201
assert resp.json()['id']

resp = await native.post(app, '/upload', body=b'\x00\x01',
                         headers={b'content-type': b'application/octet-stream'})
```

Headers accept `str` or `bytes` in either position and are lowercased
for you.  A `host` header is supplied when you give none, and
`content-length` is derived from the body — both because a real
request carries them, and code that branches on them should behave
the same under test as on the wire.

For a request the helpers can't express, build the `Connection`
yourself:

```python
from blackbull import Connection
from blackbull.headers import Headers

conn = Connection(
    method='POST', path='/tasks', raw_path=b'/tasks',
    headers=Headers([(b'content-type', b'application/json')]),
)
conn.state['tenant'] = 'acme'          # pre-seed what a middleware would set
resp = await native.request(app, conn, body=b'{"name":"x"}')
```

`build_connection()` is the same constructor the helpers use, if you
want the parser-faithful defaults and then a tweak.

### Synchronous tests

`NativeClient` wraps the same functions for tests written as plain
`def`.  It owns one background event loop for the whole session — not
one per request — and runs the ASGI `lifespan` protocol around the
block:

```python
from blackbull.testing import NativeClient


def test_hello_sync():
    with NativeClient(app) as client:
        resp = client.get('/hello')
        assert resp.status == 200
```

Prefer the coroutines from an `async def` test: they call the app on
the test's own loop, with no thread hand-off at all.

### What `native` does not see

Tier 1 collects the events the **application** emitted, not the bytes
a server would write.  Framing headers (`Content-Length`,
`Transfer-Encoding`) are injected by the protocol sender, below this
tier, so they are absent here.  `HEAD` reaches your handler as `HEAD`
— the rewrite to `GET` (RFC 9110 §9.3.2) is the HTTP/1.1 actor's job.

Those are `NativeTestServer` questions.

## Full stack with `NativeTestServer`

`NativeTestServer` binds a real loopback socket and runs BlackBull's
own server, so a request travels the entire production path: TCP
accept → `ConnectionActor` → `HTTP1Actor` parsing → `Connection` →
native dispatch → the bytes the sender writes.

```python
import pytest
from blackbull.testing import NativeTestServer


@pytest.mark.asyncio
async def test_head_has_get_headers_and_no_body():
    async with NativeTestServer(app) as server:
        get_resp = await server.client.get('/hello')
        head_resp = await server.client.head('/hello')

    assert head_resp.content == b''
    assert head_resp.headers['content-length'] == get_resp.headers['content-length']
```

`server.client` is an `httpx.AsyncClient` bound to `server.url`, so
the full httpx API is available — cookies, redirects, streaming,
custom timeouts.  `server.port` is the OS-assigned port.

The server starts once per context manager and serves as many requests
as you make, so keep-alive reuse, connection close semantics, and
pipelining are all observable:

```python
@pytest.mark.asyncio
async def test_keep_alive():
    async with NativeTestServer(app) as server:
        for _ in range(10):
            assert (await server.client.get('/hello')).status_code == 200
        assert server.connections_served == 1
```

`server.connections_served` counts TCP **accepts**, not requests, so ten
keep-alive requests on one connection leave it at `1`.  It is the honest
way to assert connection reuse: `Connection: close` is a header the
server *may* send, so its absence proves nothing on its own.

The synchronous form runs the server on one background loop for the
session:

```python
def test_full_stack_sync():
    with NativeTestServer(app) as server:
        assert server.client.get('/hello').status_code == 200
```

Scope and limits:

- **Loopback only.** The listener binds `127.0.0.1`, so a test never
  publishes a port beyond the machine.
- **Plaintext HTTP/1.1 and WebSocket.** TLS and HTTP/2 are out of
  scope for this tier — use the BlackBull clients + ephemeral port
  pattern below, which negotiates ALPN against a real certificate.
- **Startup cost is a single `asyncio.start_server`** — no subprocess,
  no fork.  Per-request cost is loopback TCP, well under a
  millisecond.

## Choosing between `native` and `TestClient`

Both run in-process and neither needs a port, so the difference is not
speed — it is *which code runs*.

```text
native.get(app, '/x')                TestClient(app).get('/x')
  → Connection                         → httpx.ASGITransport
  → app(conn, receive, send)           → builds an ASGI scope dict
  → isinstance(conn, Connection)       → app(scope, receive, send)
      → True   ← production branch     → isinstance(conn, Connection)
  → dispatch                               → False
                                       → Connection.from_scope(scope)
                                       → dispatch
```

`TestClient` therefore never takes the branch every production request
takes.  What it *uniquely* covers is the conversion chain itself: a
missing `_CONNECTION_FIELDS` entry, or a coercion bug in
`from_scope()`, shows up there and nowhere else — which is exactly why
it stays, and why BlackBull keeps a CI lane that runs the whole suite
under `BB_FORCE_ASGI_SCOPE=1`.

So:

- **Writing an application test?** Use `native` (or `NativeTestServer`
  when the wire matters).
- **Deploying under uvicorn, hypercorn, or another ASGI host?** Keep a
  handful of `TestClient` tests — they are what proves the boundary
  still works.

## `TestClient` — the ASGI boundary

`blackbull.testing.TestClient` is a synchronous in-memory client
modelled on `httpx.Client`: GET / POST / PUT / DELETE all work
the same way they would over the network, but the request is
dispatched directly into the ASGI app — no socket, no port.
The ASGI `lifespan` protocol runs around the `with` block, so
`@app.on_startup` / `@app.on_shutdown` handlers fire in the
expected order.

Its job is the **compatibility boundary**: it drives the app the way
an external ASGI host does, through a scope dict and `from_scope()`.
Everything below still works as documented — it is the recommendation
that changed, not the API.  For application-logic tests, reach for
[`native`](#quick-start-with-native) instead.

```python
from blackbull import BlackBull
from blackbull.testing import TestClient

app = BlackBull()


@app.route(path='/')
async def hello():
    return "hello, world"


@app.route(path='/items/{item_id:int}')
async def get_item(item_id: int):
    return {"id": item_id, "kind": "widget"}


def test_hello():
    with TestClient(app) as client:
        response = client.get('/')
    assert response.status_code == 200
    assert response.text == "hello, world"


def test_get_item():
    with TestClient(app) as client:
        response = client.get('/items/42')
    assert response.json() == {"id": 42, "kind": "widget"}
```

The response object is `httpx.Response` — same shape as a
production HTTP client, so `.status_code`, `.headers`,
`.json()`, `.text`, `.content`, cookies, redirects all work
unchanged.

### POST and JSON bodies

```python
def test_create_item():
    with TestClient(app) as client:
        response = client.post('/items', json={'name': 'widget'})
    assert response.status_code == 201
```

`client.post`, `.put`, `.patch`, `.delete`, `.head`,
`.options`, and the underlying `client.request(method, url, ...)`
all accept the same arguments as `httpx.Client`:

- `content=` for raw bytes
- `data=` for form-encoded fields
- `json=` for JSON-encoded bodies
- `headers=` for request headers
- `cookies=` for cookies
- `params=` for query parameters
- `follow_redirects=False` (default) to assert on 3xx
  responses directly; pass `follow_redirects=True` on the
  `TestClient` constructor or per-call kwarg to traverse them.

### File uploads, auth, timeouts, and other httpx parameters

`TestClient`'s HTTP methods forward `**kwargs` straight to the
underlying `httpx.AsyncClient`, so anything httpx accepts works
unchanged.  Patterns worth knowing:

**Multipart file upload** — pass `files=` (and optional `data=`
for accompanying form fields).  Each `files` entry is
`(field_name, (filename, fileobj, content_type))`:

```python
def test_upload():
    with TestClient(app) as client:
        response = client.post(
            '/upload',
            files={'attachment': ('report.pdf', b'%PDF-1.4...', 'application/pdf')},
            data={'note': 'monthly'},
        )
    assert response.status_code == 201
```

**HTTP authentication** — pass `auth=`:

```python
# Basic auth
response = client.get('/private', auth=('alice', 'hunter2'))

# Custom scheme via httpx.Auth subclass — e.g. a bearer token
response = client.get('/api/me', headers={'Authorization': 'Bearer abc.def.ghi'})
```

**Per-request timeout** — pass `timeout=` (seconds, or an
`httpx.Timeout` for finer control):

```python
response = client.get('/slow', timeout=5.0)
```

Default timeouts can be passed on the `TestClient` constructor
via the `headers=` / `cookies=` / `follow_redirects=` options,
or set after construction by mutating `client.headers` /
`client.cookies` — both forward to the underlying
`httpx.AsyncClient`, so the standard httpx jar semantics apply
(cookies set by responses persist across subsequent requests on
the same client).

```python
with TestClient(app, headers={'X-Test-Tag': 'integration'}) as client:
    # X-Test-Tag goes out on every request below
    client.get('/api/one')
    client.get('/api/two')
```

### WebSocket sessions

`client.websocket_connect(path)` returns a synchronous
context-managed session.  The session lives inside its own
background event loop, so all of `send_text` / `send_bytes` /
`send_json` and the matching receive methods are blocking
synchronous calls:

```python
from blackbull.router import Scheme


@app.route(path='/ws', scheme=Scheme.websocket)
async def ws_echo(conn, receive, send):
    await receive()                              # websocket.connect
    await send({'type': 'websocket.accept'})
    while True:
        event = await receive()
        if event['type'] == 'websocket.disconnect':
            return
        if event.get('text') is not None:
            await send({'type': 'websocket.send', 'text': event['text']})


def test_ws_echo():
    with TestClient(app) as client:
        with client.websocket_connect('/ws') as ws:
            ws.send_text('ping')
            assert ws.receive_text() == 'ping'
```

When the application closes (or rejects) the WebSocket,
`receive_text` / `receive_bytes` raise
`blackbull.testing.WebSocketDisconnect` carrying the RFC 6455
close code:

```python
from blackbull.testing import WebSocketDisconnect


@app.route(path='/ws-auth', scheme=Scheme.websocket)
async def ws_auth(conn, receive, send):
    await receive()
    await send({'type': 'websocket.close', 'code': 4401})


def test_ws_rejects_unauthenticated():
    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect) as excinfo:
            with client.websocket_connect('/ws-auth'):
                pass
        assert excinfo.value.code == 4401
```

For server-streamed sessions where the handler emits a sequence
of frames and then closes, `ws.iter_text()` and
`ws.iter_bytes()` consume the stream until the close arrives —
the `WebSocketDisconnect` is caught and turned into normal
iterator termination so the test reads as a single `for` loop:

```python
@app.route(path='/notifications', scheme=Scheme.websocket)
async def notifications(conn, receive, send):
    await receive()                              # websocket.connect
    await send({'type': 'websocket.accept'})
    for n in range(3):
        await send({'type': 'websocket.send', 'text': f'event-{n}'})
    await send({'type': 'websocket.close', 'code': 1000})


def test_notifications_stream():
    with TestClient(app) as client:
        with client.websocket_connect('/notifications') as ws:
            messages = list(ws.iter_text())
    assert messages == ['event-0', 'event-1', 'event-2']
```

### Lifespan startup and shutdown

`TestClient` drives the ASGI `lifespan` protocol automatically
around the `with` block:

```python
events = []

@app.on_startup
async def _start():
    events.append('startup')

@app.on_shutdown
async def _stop():
    events.append('shutdown')


def test_lifespan_runs():
    with TestClient(app) as client:
        assert events == ['startup']
        client.get('/')
    assert events == ['startup', 'shutdown']
```

If a startup handler raises, `TestClient.__enter__` re-raises a
`RuntimeError` describing the failure, so a broken startup
fails the test rather than silently leaving the app in a
half-initialised state.  Apps that don't implement the
lifespan protocol (i.e. legacy ASGI-2.0-style callables) are
tolerated silently.

### Construction options

```python
TestClient(
    app,
    base_url='http://testserver',
    raise_app_exceptions=True,    # let handler exceptions bubble up
    root_path='',                 # ASGI scope['root_path']
    cookies=None,
    headers=None,
    follow_redirects=False,
)
```

`raise_app_exceptions=True` (the default) is what you want in
tests — handler tracebacks surface as the test failure.  Set it
to `False` to assert on the 500 response the framework would
emit to a real client.

## End-to-end with BlackBull's clients

BlackBull exports four async clients from `blackbull.client`:

| Client | Picks the protocol by | Use for |
|---|---|---|
| `Client` | ALPN (h2 vs http/1.1) | Don't care which — let the handshake decide |
| `HTTP1Client` | Always HTTP/1.1 | Cleartext, or HTTPS without ALPN |
| `HTTP2Client` | Always HTTP/2 | h2c (cleartext h2) or pre-negotiated TLS |
| `WebSocketClient` | RFC 6455 Upgrade | WebSocket round-trip tests |

The pattern is: bind the app on an ephemeral port in a fixture,
then `async with` a client at that port.

### Fixture — ephemeral-port server

```python
import asyncio
import pytest_asyncio
from blackbull import BlackBull, Response
from blackbull.server.server import ASGIServer

_app = BlackBull()


@_app.route(path='/ping')
async def ping():
    return "pong"


@_app.route(path='/echo', methods=['POST'])
async def echo(body: bytes):
    return body


@pytest_asyncio.fixture
async def server_port():
    server = ASGIServer(_app)
    server.open_socket(port=0)            # 0 = ephemeral
    port = server.port

    task = asyncio.create_task(server.run())
    await asyncio.sleep(0.1)               # let bind settle

    try:
        yield port
    finally:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
```

`server.port` returns the kernel-assigned port number after
`open_socket(port=0)` — pass that to the client.

### HTTP/1.1

```python
import pytest
from http import HTTPMethod, HTTPStatus
from blackbull.client import HTTP1Client


@pytest.mark.asyncio
async def test_ping(server_port):
    async with HTTP1Client('localhost', server_port) as c:
        res = await c.request(HTTPMethod.GET, '/ping')
    assert res.status == HTTPStatus.OK
    assert res.body == b'pong'


@pytest.mark.asyncio
async def test_echo(server_port):
    async with HTTP1Client('localhost', server_port) as c:
        res = await c.request(HTTPMethod.POST, '/echo', body=b'hello')
    assert res.body == b'hello'


@pytest.mark.asyncio
async def test_keep_alive(server_port):
    async with HTTP1Client('localhost', server_port) as c:
        r1 = await c.request(HTTPMethod.GET, '/ping')
        r2 = await c.request(HTTPMethod.POST, '/echo', body=b'second')
    assert r1.body == b'pong'
    assert r2.body == b'second'
```

The client persists the TCP connection across `request()`
calls inside one `async with` block (HTTP/1.1 persistent
connections, RFC 9112 §9.3) — useful for verifying keep-alive
behaviour.  The `Host` header is injected automatically.

### Streaming request bodies

`request(...)`'s `body=` accepts an async generator for
streaming uploads:

```python
async def chunks():
    for c in (b'one ', b'two ', b'three'):
        yield c

async with HTTP1Client('localhost', server_port) as c:
    res = await c.request(HTTPMethod.POST, '/echo', body=chunks())
```

For streaming **responses**, use `client.stream(...)` instead of
`request(...)` to consume the body chunk-by-chunk without
buffering everything in memory.

### HTTPS + HTTP/2 with ALPN

For TLS-terminated tests use the `Client` dispatcher.  It opens
the connection with your TLS context, inspects the negotiated
ALPN protocol, and hands off to `HTTP2Client` (when the server
advertises `h2`) or `HTTP1Client` otherwise:

```python
import ssl
from blackbull.client import Client

ctx = ssl.create_default_context()
ctx.set_alpn_protocols(['h2', 'http/1.1'])
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE             # test cert

async with Client('localhost', tls_port, ssl=ctx) as c:
    # c is HTTP2Client or HTTP1Client depending on what the server picked
    res = await c.request(HTTPMethod.GET, '/ping')
```

The cert fixture pattern mirrors how BlackBull's own
conformance tests handle TLS — see
[`tests/conformance/http1/test_client.py`](https://github.com/TOKUJI/BlackBull/blob/master/tests/conformance/http1/test_client.py)
for a worked example.

### WebSocket

`WebSocketClient` opens the RFC 6455 handshake and exposes a
session for sending / receiving frames:

```python
from blackbull.client import WebSocketClient


@pytest.mark.asyncio
async def test_ws_echo(server_port):
    async with WebSocketClient('localhost', server_port).connect('/ws') as session:
        await session.send('hello')
        msg = await session.recv()
        assert msg == 'hello'
```

`send` / `recv` handle both text and binary frames; fragmented
messages are reassembled transparently (matching the
server-side semantics — see
[WebSockets](websockets.md#fragmented-messages)).

## In-process integration with `httpx`

When socket fidelity isn't important (most application tests)
and you want a faster, fully hermetic harness,
[`httpx`](https://www.python-httpx.org/)'s `ASGITransport`
drives the app over an in-process channel — no port, no TCP:

```python
import pytest
import httpx
from myapp import app   # your BlackBull instance


@pytest.mark.asyncio
async def test_register_and_list_tasks():
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url='http://test',
    ) as client:
        r = await client.post('/register',
                              json={'username': 'alice', 'password': 'secret'})
        assert r.status_code == 200
        token = r.json()['token']

        r = await client.post('/tasks',
                              json={'title': 'Buy milk'},
                              headers={'Authorization': f'Bearer {token}'})
        assert r.status_code == 201

        r = await client.get('/tasks',
                             headers={'Authorization': f'Bearer {token}'})
        assert r.json()[0]['title'] == 'Buy milk'
```

`httpx[http2]` lets you pass `http2=True` and drive the HTTP/2
path through the same in-process adapter.

When to pick which:

- **`NativeTestServer`** for HTTP/1.1 and WebSocket wire behaviour
  in-process — framing, keep-alive, `HEAD`, chunking.  No
  subprocess, no certificate.
- **BlackBull clients + ephemeral port** when the test needs what
  `NativeTestServer` leaves out: TLS, ALPN, HTTP/2 framing,
  fragmented WS messages.
- **`httpx.ASGITransport`** when you are asserting on the ASGI
  boundary and want the async equivalent of `TestClient`.

## Direct handler tests

For unit-level assertions on a single handler — no transport,
no routing — call the app callable directly with a hand-rolled
scope.  `BlackBull.__call__` is a standard ASGI 3.0 callable so
the stub `send` is a single-argument coroutine that receives
event dicts:

```python
import pytest
from blackbull import BlackBull, JSONResponse
from blackbull.server.headers import Headers

app = BlackBull()


@app.route(path='/ping')
async def ping(conn, receive, send):
    await send(JSONResponse({'pong': True}))


def make_scope(method='GET', path='/ping'):
    return {
        'type': 'http',
        'method': method,
        'path': path,
        'query_string': b'',
        'headers': Headers([]),
        'state': {},
    }


async def fake_receive():
    return {'type': 'http.request', 'body': b'', 'more_body': False}


@pytest.mark.asyncio
async def test_ping_handler():
    events = []

    async def fake_send(event):
        events.append(event)

    await app(make_scope(), fake_receive, fake_send)
    start = next(e for e in events if e['type'] == 'http.response.start')
    body = next(e for e in events if e['type'] == 'http.response.body')
    assert start['status'] == 200
    assert b'"pong"' in body['body']
```

Useful when you want to assert on the raw ASGI event sequence.
For anything routing-shaped, prefer `native` — it costs almost
nothing more, exercises the whole dispatch pipeline, and hands the
handler the same `Connection` the server would.  `NativeTestResponse.events`
keeps the raw event list if that is what you were reaching for here.

## Middleware in isolation

Middleware is an async function — test it by passing stub
callables.  Useful for asserting short-circuit behaviour,
header mutation, or `state` injection.  Drive it with the same
`Connection` the server would:

```python
@pytest.mark.asyncio
async def test_auth_mw_rejects_missing_token():
    from myapp import auth_mw
    from blackbull.connection import Connection
    from blackbull.headers import Headers
    from blackbull.response import wrap_native_send

    conn = Connection(method='GET', path='/tasks', raw_path=b'/tasks',
                      headers=Headers([]), type='http')

    events = []
    async def collect(event):
        events.append(event)

    call_next_called = False
    async def fake_call_next(conn, receive, send):
        nonlocal call_next_called
        call_next_called = True

    # `wrap_native_send` is the seam the app puts above a middleware: it
    # converts whatever the middleware sends — a `Response`, an ASGI dict —
    # into the `NativeResponse` the sender receives.  Without it the stub
    # sees the raw object the middleware happened to pass.
    await auth_mw(conn, None, wrap_native_send(collect), fake_call_next)

    assert not call_next_called          # short-circuited
    start = next(e for e in events if e.header is not None)
    assert start.status == 401
```

The pattern works for async-function middleware and
`@as_middleware` classes alike.  A middleware that declared `scope`
instead receives an ASGI scope dict and emits ASGI event dicts — build
the stub input with `conn.to_asgi_scope()`, drop the
`wrap_native_send`, and assert on `event['type']` / `event['status']`.

## What BlackBull's own suite uses

BlackBull's test suite (`tests/`) splits into four layers:

| Layer | Directory | Asserts |
|---|---|---|
| Unit | `tests/unit/` | Single-module logic — parsing, framing, routing — no I/O |
| Architecture | `tests/architecture/` | Actor behaviour, scope fields, framework wiring — `AsyncMock` fixtures |
| Integration | `tests/integration/` | Real subprocess + real socket; full server lifecycle |
| Conformance | `tests/conformance/` | RFC 9112 / 9113 / 6455, h2spec, Autobahn — external compliance |

You don't need this much structure for an application suite;
the layers exist because BlackBull also vets protocol behaviour.
A typical app project gets by with a `tests/` directory using
`native` for application logic and `NativeTestServer` for the
handful of cases where the wire is the point.

## Next

- [Routing](routing.md) — what's available to assert against
  (path params, route names, `url_path_for`).
- [Middleware](middleware.md) — middleware shapes to write
  tests against.
- [Events](events.md) — testing observers and interceptors.
