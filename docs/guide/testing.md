# Testing

BlackBull's `app` is a plain ASGI 3.0 callable and ships its own
async HTTP/1.1, HTTP/2, and WebSocket clients, so there are three
useful shapes for tests:

- **End-to-end with BlackBull's own clients** — start the server
  on an ephemeral port, drive it through real sockets via
  `HTTP1Client` / `HTTP2Client` / `Client` / `WebSocketClient`.
  Best fidelity: exercises the wire, ALPN, TLS, framing.
- **In-process integration via `httpx.ASGITransport`** — no
  sockets, no ports.  Drives the app object directly.  Good for
  fast, hermetic suites where transport detail doesn't matter.
- **Direct handler / middleware unit tests** — hand-rolled
  scope dict + stub callables.  For asserting a single
  function's behaviour without involving routing or transport.

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
shape.

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

- **BlackBull clients + ephemeral port** when the test cares
  about the wire — TLS, ALPN, HTTP/2 framing, fragmented WS
  messages, keep-alive, chunked encoding boundaries.
- **`httpx.ASGITransport`** when you want fast, hermetic
  request/response assertions and don't care about transport
  detail.

## Direct handler tests

For unit-level assertions on a single handler — no transport,
no routing — call the app callable directly with a hand-rolled
scope:

```python
import pytest
from http import HTTPStatus
from blackbull import BlackBull, JSONResponse
from blackbull.server.headers import Headers

app = BlackBull()


@app.route(path='/ping')
async def ping(scope, receive, send):
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
    responses = []

    async def fake_send(body, status=HTTPStatus.OK, headers=[]):
        responses.append({'body': body, 'status': status})

    await app(make_scope(), fake_receive, fake_send)
    assert responses[0]['status'] == HTTPStatus.OK
    assert b'"pong"' in responses[0]['body']
```

Useful when you want to assert on response shape without
involving HTTP framing or transport.  Prefer the integration
patterns above for anything routing-shaped — they cost almost
nothing more and exercise more of the framework.

## Middleware in isolation

Middleware is an async function — test it by passing stub
callables.  Useful for asserting short-circuit behaviour,
header mutation, or scope injection:

```python
@pytest.mark.asyncio
async def test_auth_mw_rejects_missing_token():
    from myapp import auth_mw
    from blackbull.server.headers import Headers
    from http import HTTPStatus

    scope = {'type': 'http', 'method': 'GET', 'path': '/tasks',
             'headers': Headers([]), 'state': {}}

    responses = []
    async def fake_send(body, status=HTTPStatus.OK, headers=[]):
        responses.append(status)

    call_next_called = False
    async def fake_call_next(scope, receive, send):
        nonlocal call_next_called
        call_next_called = True

    await auth_mw(scope, None, fake_send, fake_call_next)

    assert not call_next_called          # short-circuited
    assert responses[0] == HTTPStatus.UNAUTHORIZED
```

The pattern works for async-function middleware and
`@as_middleware` classes alike.

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
A typical app project gets by with a `tests/` directory of
integration tests using either the BlackBull client + ephemeral
port pattern or the `httpx.ASGITransport` pattern from above.

## Next

- [Routing](routing.md) — what's available to assert against
  (path params, route names, `url_path_for`).
- [Middleware](middleware.md) — middleware shapes to write
  tests against.
- [Events](events.md) — testing observers and interceptors.
