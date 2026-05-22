# BlackBull Developer Guide

BlackBull is a Python ASGI 3.0 web framework supporting HTTP/1.1, HTTP/2, and WebSocket.

---

## §0  Prerequisites & Installation

**Python 3.11+** is required.

```bash
git clone <repo>
cd BlackBull
pip install .
```

Core imports:

```python
from blackbull import BlackBull, Response, JSONResponse, read_body
from http import HTTPMethod, HTTPStatus
```

### §0.1  What BlackBull is not

BlackBull is a **routing and middleware layer**.  It does not include:

- An ORM or database layer — bring your own (SQLite, SQLAlchemy, asyncpg, …)
- A template engine — render HTML with Jinja2, Mako, or plain f-strings
- A request-validation library — validate with Pydantic, marshmallow, or manually
- A server-side session store — for cookie-based sessions, see [Session](#session) (§4.4); for server-side state bring your own (dict, Redis, …)

This keeps the framework small and lets you swap any of these concerns independently.

---

## §1  Quick Start

```python
import asyncio
from blackbull import BlackBull, Response

app = BlackBull()

@app.route(path='/')
async def hello(scope, receive, send):
    await send(Response(b'Hello, world!'))

if __name__ == '__main__':
    asyncio.run(app.run(port=8000))
```

Every handler is an `async` function.  The full form receives `(scope, receive, send)`
and calls `send` directly.  A simplified form omits those parameters and returns the
response value instead — BlackBull wraps it automatically (see §3.5).

---

## §2  Core Concepts

Every HTTP request arrives as the ASGI triple `(scope, receive, send)`.

| Argument | Type | Role |
|---|---|---|
| `scope` | `dict` | Request metadata (method, path, headers, …) |
| `receive` | `async callable` | Read request body events |
| `send` | `async callable` | Write the response |

### Common scope keys

| Key | Type | Notes |
|---|---|---|
| `scope['type']` | `str` | `'http'` or `'websocket'` |
| `scope['method']` | `str` | `'GET'`, `'POST'`, … |
| `scope['path']` | `str` | URL path, e.g. `'/tasks/42'` |
| `scope['headers']` | `Headers` | See §6.3 for the Headers API |
| `scope['query_string']` | `bytes` | Raw query string, e.g. `b'q=hello&page=2'`; parse with `urllib.parse.parse_qs` — see §6.5 |
| `scope['path_params']` | `dict` | Values from `{name}` path segments; see §4.3 |
| `scope['state']` | `dict` | Framework error info; see §8 |

Custom middleware may add additional keys — e.g. `scope['user']`, `scope['json']`.

---

## §3  Routing

### 3.1  HTTP routes

```python
from http import HTTPMethod

@app.route(methods=[HTTPMethod.GET], path='/tasks')
async def list_tasks(scope, receive, send):
    await send(Response(b'[]'))
```

`methods` defaults to `[HTTPMethod.GET]`.  Pass a list to accept multiple methods
on the same handler:

```python
@app.route(methods=[HTTPMethod.GET, HTTPMethod.HEAD], path='/healthz')
async def healthz(scope, receive, send):
    await send(Response(b'ok'))
```

### 3.2  Path parameters

Use `{name}` segments in the path string.  Captured values are available in
`scope['path_params']`:

```python
@app.route(path='/tasks/{task_id}')
async def get_task(scope, receive, send):
    task_id = scope['path_params']['task_id']   # str (default converter)
    await send(Response(task_id.encode()))
```

`{name}` (no converter) matches `[^/]+` and injects a `str`.  Append `:converter`
to control both the pattern and the injected type — see §3.5 for `int`, `uuid`,
`path`, and custom converters.

For fully custom patterns supply a compiled regex with named groups; the captured
values are injected into `scope['path_params']` — the same place as `{name}`
parameters:

```python
import re

@app.route(path=re.compile(r'^/items/(?P<id>\d+)$'))
async def get_item(scope, receive, send):
    item_id = scope['path_params']['id']    # same interface as {name} style
    await send(Response(item_id.encode()))
```

### 3.3  WebSocket routes

```python
from blackbull.utils import Scheme

@app.route(path='/ws', scheme=Scheme.websocket)
async def ws_handler(scope, receive, send):
    await receive()                          # consume 'websocket.connect'
    await send({'type': 'websocket.accept'})
    while True:
        event = await receive()
        if event['type'] == 'websocket.disconnect':
            break
        # event['type'] == 'websocket.receive'
        text = event.get('text') or event.get('bytes', b'').decode()
        await send({'type': 'websocket.send', 'text': text})
```

The built-in `blackbull.middleware.websocket` handles the connect/accept handshake
automatically; include it in `middlewares=[websocket, ...]` on the route.

BlackBull validates `Sec-WebSocket-Version: 13`.

#### permessage-deflate (RFC 7692)

`permessage-deflate` compression is negotiated automatically when the client
offers it on the handshake.  The server replies with `Sec-WebSocket-Extensions:
permessage-deflate; server_no_context_takeover; client_no_context_takeover` —
the no-context-takeover flags trade a small compression-ratio penalty for bounded
per-connection memory (each side resets its deflate state between messages
instead of keeping it for the whole connection).

| Aspect | Behaviour |
|---|---|
| Default | On — matches modern browsers, Node `ws`, Python `websockets`, aiohttp. |
| Disable | `BB_WS_PERMESSAGE_DEFLATE=0` (see §18.6).  The handshake still succeeds; just no extension is negotiated. |
| Per-message-deflate strategy | Both `server_no_context_takeover` and `client_no_context_takeover` always advertised by the server. |
| RSV1 bit | Set on compressed data frames per §7 of the RFC; clients without the negotiated extension that send RSV1 are rejected as protocol violations. |

#### Transport: HTTP/1.1 Upgrade and HTTP/2 Extended CONNECT (RFC 8441)

WebSocket is always available over the HTTP/1.1 `Upgrade` handshake (RFC 6455 §4).
Over HTTP/2 it is **opt-in** via Extended CONNECT (RFC 8441):

```bash
BB_H2_ENABLE_WEBSOCKET=1 python app.py --port 8443 --cert cert.pem --key key.pem
```

When enabled the server advertises `SETTINGS_ENABLE_CONNECT_PROTOCOL=1` in its
initial SETTINGS frame, after which an HTTP/2 peer may open a WebSocket by
sending `:method = CONNECT`, `:protocol = websocket`, and the usual
`Sec-WebSocket-*` pseudo-headers on a single stream.  The bidirectional DATA
frames on that stream then carry WebSocket frames.

This path is off by default because it has fewer conformance tests than the
HTTP/1.1 Upgrade path and few clients use it in practice — Cloudflare's edge
stack is the main consumer.  Browsers that negotiate HTTP/2 via ALPN normally
still use HTTP/1.1 for WebSocket, so most apps do not need to enable RFC 8441.

#### Subprotocol negotiation

Register the protocols the server supports before starting:

```python
app.available_ws_protocols = ['chat', 'superchat']
```

BlackBull picks the first protocol from the client's `Sec-WebSocket-Protocol` offer
that appears in this list and returns it in the 101 handshake response.  If there is
no match, or if the client did not offer any protocol, no `Sec-WebSocket-Protocol`
header is sent and the connection proceeds without a subprotocol.

The list accepts `str` or `bytes` values.  Typical protocol names:

| Protocol | Use case |
|---|---|
| `graphql-ws` | Legacy GraphQL subscriptions (Apollo) |
| `graphql-transport-ws` | Modern GraphQL subscriptions |
| `stomp` / `v12.stomp` | STOMP messaging (RabbitMQ, ActiveMQ) |
| `mqtt` | MQTT over WebSocket (IoT) |
| `wamp` | Web Application Messaging Protocol |
| `ocpp1.6` / `ocpp2.0` | EV charging stations |

#### Fragmented messages

WebSocket clients may split a single logical message across multiple frames
(RFC 6455 §5.4).  BlackBull reassembles fragments transparently — the app
always receives one `websocket.receive` event containing the full payload,
regardless of how many frames the client used.

A fragmented sequence looks like this on the wire:

```
FIN=0, opcode=TEXT,  payload=b'hel'   ← opener
FIN=0, opcode=0x0,   payload=b'lo'    ← continuation
FIN=1, opcode=0x0,   payload=b''      ← final continuation
```

The app sees a single event:

```python
{'type': 'websocket.receive', 'text': 'hello', 'bytes': None}
```

Control frames (ping, pong, close) may legally appear between data fragments;
BlackBull handles them immediately (responding to pings with pong) and then
continues reassembling the fragmented message.

The following are protocol violations and raise `ProtocolError`:

| Violation | RFC reference |
|---|---|
| CONTINUATION frame with no fragmentation in progress | §5.4 |
| New TEXT or BINARY frame while a fragment sequence is open | §5.4 |
| Control frame (ping/pong/close) with FIN=0 | §5.5 |

### 3.4  Simplified handler signatures

The full `(scope, receive, send)` triplet is always available, but for most
handlers only a subset of that information is needed.  BlackBull detects at
registration time whether a handler omits those parameters and wraps it
automatically — no boilerplate needed.

#### No parameters

The handler in [`examples/helloworld-simple.py`](https://github.com/TOKUJI/BlackBull/blob/master/examples/helloworld-simple.py)
is the minimal form:

```python
@app.route(path='/')
async def hello():
    return "Hello, world!"
```

BlackBull sends the return value as the response body.  No `send` call needed.

#### Path parameters by name

Declare parameters whose names match `{name}` segments in the path.  BlackBull
extracts the captured value from `scope['path_params']` and injects it directly:

```python
@app.route(path='/tasks/{task_id}')
async def get_task(task_id):       # str by default
    return f"Task {task_id}"
```

Use a `{param:int}` converter (see §3.5) so the URL pattern only matches integers
and the value arrives already coerced — this is the preferred approach:

```python
@app.route(path='/tasks/{task_id:int}')
async def get_task(task_id: int):  # int injected by the router
    return {"id": task_id}
```

Alternatively, annotate without a converter and BlackBull will coerce the captured
string at call time (raises HTTP 500 if not convertible):

```python
@app.route(path='/tasks/{task_id}')
async def get_task(task_id: int):  # str captured; int() applied by _adapt_handler
    return {"id": task_id}
```

#### Request body

Name a parameter `body` to receive the complete request body as `bytes`.
BlackBull reads all chunks before calling the handler:

```python
import json

@app.route(path='/echo', methods=[HTTPMethod.POST])
async def echo(body: bytes):
    data = json.loads(body)
    return data          # dict → JSONResponse automatically
```

#### The `scope` dict

Name a parameter `scope` to receive the full scope dict alongside other
simplified parameters:

```python
@app.route(path='/items/{item_id}')
async def get_item(item_id: int, scope):
    lang = scope['headers'].get(b'accept-language', b'en').decode()
    return {"id": item_id, "lang": lang}
```

#### Return value mapping

| Return type | Response sent |
|---|---|
| `str` | `Response(value.encode())` — `text/html; charset=utf-8` |
| `bytes` | `Response(value)` — `text/html; charset=utf-8` |
| `dict` or `list` | `JSONResponse(value)` — `application/json` |
| `Response` / `JSONResponse` | Passed to `send` as-is |
| `None` | Nothing sent (handler called `send` directly, or intentionally empty) |

#### When to use the full triplet

- **WebSocket handlers** always receive the full `(scope, receive, send)` triplet.
- **Middleware functions** must keep the `call_next` (or `inner`) parameter — the
  simplified adaptation does not apply to them.
- When you need to call `receive()` in a loop (streaming uploads, long-polling)
  use the full form and call `receive()` yourself.

### 3.6  Detecting client disconnection

When the remote side closes the connection, `receive()` returns
`{'type': 'http.disconnect'}`.  This is useful for long-polling and
server-sent events (SSE) to avoid writing to a closed socket.

> **Note — streaming responses**: For most cases use `StreamingResponse` (see
> §17), which wraps an async generator and emits `more_body=True` on every
> chunk.  The example below uses the raw ASGI event-dict API directly, which
> is convenient when each chunk needs custom shaping (e.g. SSE event framing).
> Either form works; passing `Response(b'')` to `send` ends the connection
> immediately with `Content-Length: 0` and cannot be followed by additional chunks.

```python
@app.route(path='/events')
async def sse(scope, receive, send):
    # Open the response without Content-Length, keep connection alive
    await send({
        'type': 'http.response.start',
        'status': 200,
        'headers': [
            (b'content-type',  b'text/event-stream'),
            (b'cache-control', b'no-cache'),
        ],
    })
    while True:
        event = await receive()
        if event['type'] == 'http.disconnect':
            break
        await send({
            'type': 'http.response.body',
            'body': b'data: ping\n\n',
            'more_body': True,   # keep the response stream open
        })
    # Signal end of stream
    await send({'type': 'http.response.body', 'body': b'', 'more_body': False})
```

---

### 3.5  Typed Routes

#### Path parameter converters

Append `:converter` to a path parameter to control both the URL pattern and the Python type injected into the handler:

| Syntax | Regex matched | Python type |
|---|---|---|
| `{name}` or `{name:str}` | `[^/]+` | `str` |
| `{id:int}` | `-?[0-9]+` | `int` |
| `{uid:uuid}` | UUID hex pattern | `uuid.UUID` |
| `{rest:path}` | `.+` (matches `/`) | `str` |

```python
import uuid

@app.route(path='/items/{id:int}', methods=HTTPMethod.GET)
async def get_item(id: int):
    return {'id': id}            # id is already int, not a string

@app.route(path='/users/{uid:uuid}', methods=HTTPMethod.GET)
async def get_user(uid: uuid.UUID):
    return {'uid': str(uid)}

@app.route(path='/files/{rest:path}', methods=HTTPMethod.GET)
async def get_file(rest: str):   # rest may contain slashes
    return {'path': rest}
```

#### URL reverse lookup — `url_path_for`

Register a route with a `name=` keyword, then build its path from parameters:

```python
@app.route(path='/items/{id:int}', methods=HTTPMethod.GET, name='item-detail')
async def get_item(id: int):
    return {'id': id}

app.url_path_for('item-detail', id=42)   # → '/items/42'
```

`url_path_for` raises `KeyError` for unknown names and `ValueError` when required parameters are missing.

#### Startup validation

`app.run()` and the ASGI lifespan `startup` event both call `Router.validate()` before accepting connections. Validation checks:

- Every `{param:converter}` uses a known converter name.
- Every path parameter appears in the handler's signature.
- When `typeguard` is installed (`pip install 'blackbull[validation]'`): the converter's output type matches the handler's annotation (e.g. `{id:int}` with `id: str` is an error).

On failure, a `ConfigurationError` is raised (or sent as `lifespan.startup.failed`) listing every violated route. On success, the router is **frozen** — further route registration raises `RuntimeError`.

#### Example — validation passes ([`examples/typed_routes_ok.py`](https://github.com/TOKUJI/BlackBull/blob/master/examples/typed_routes_ok.py))

```python
import asyncio
import uuid
from http import HTTPMethod
from blackbull import BlackBull

app = BlackBull()

@app.route(path='/greet/{name:str}', methods=HTTPMethod.GET, name='greet')
async def greet(name: str):
    return f'Hello, {name}!'

@app.route(path='/double/{n:int}', methods=HTTPMethod.GET, name='double')
async def double(n: int):
    return {'input': n, 'result': n * 2}

@app.route(path='/info/{uid:uuid}', methods=HTTPMethod.GET, name='info')
async def info(uid: uuid.UUID):
    return {'uuid': str(uid), 'version': uid.version}

if __name__ == '__main__':
    print(app.url_path_for('greet', name='Alice'))   # /greet/Alice
    print(app.url_path_for('double', n=21))           # /double/21
    asyncio.run(app.run(port=8000))
```

Every converter type matches its handler annotation, so `validate()` succeeds and the server starts.

#### Example — validation fails ([`examples/typed_routes_fail.py`](https://github.com/TOKUJI/BlackBull/blob/master/examples/typed_routes_fail.py))

```python
import asyncio
from http import HTTPMethod
from blackbull import BlackBull
from blackbull.router import ConfigurationError

app = BlackBull()

@app.route(path='/greet/{name:str}', methods=HTTPMethod.GET)
async def greet(name: str):
    return f'Hello, {name}!'

@app.route(path='/double/{n:int}', methods=HTTPMethod.GET)
async def double(n: str):      # BUG: converter is int, annotation is str
    return f'double of {n}'

if __name__ == '__main__':
    try:
        asyncio.run(app.run(port=8000))
    except* ConfigurationError as eg:
        for exc in eg.exceptions:
            print(f'ConfigurationError: {exc}')
```

Output before the server binds any port:

```
ConfigurationError: Route '/double/{n:int}' param 'n': converter 'int' yields 'int'
but annotation is <class 'str'>: int is not an instance of str
```

---

## §4  Middleware

### 4.1  Writing a middleware

```python
import time

async def logging_mw(scope, receive, send, call_next):
    t0 = time.monotonic()
    await call_next(scope, receive, send)
    elapsed = (time.monotonic() - t0) * 1000
    print(f"{scope['method']} {scope['path']}  {elapsed:.1f} ms")
```

Signature: `async def mw(scope, receive, send, call_next)`.

- Call `await call_next(scope, receive, send)` to pass control to the next layer.
- The legacy parameter name `inner` is accepted as an alias for `call_next`.
- Sending a response without calling `call_next` **short-circuits** all inner layers.

#### The `@as_middleware` decorator

Handlers may return a `Response` / `JSONResponse` object instead of calling
`send(...)` with raw ASGI events.  A middleware that wraps `send` would
otherwise have to guard each event with `isinstance(event, Response)`.  Decorate
the middleware with `@as_middleware` and the inner wrapper sees only plain
dict events — Response objects are expanded into `http.response.start` +
`http.response.body` for you:

```python
from blackbull import as_middleware

@as_middleware
async def add_header_mw(scope, receive, send, call_next):
    async def wrapped(event):
        # event is always a dict here, never a Response object
        if event['type'] == 'http.response.start':
            event = {**event,
                     'headers': list(event.get('headers', [])) + [(b'x-custom', b'1')]}
        await send(event)
    await call_next(scope, receive, wrapped)
```

`@as_middleware` also works on classes (it wraps `__call__`).  All BlackBull's
built-in middlewares (`Cache`, `Session`, `Compression`, `CORS`) use the
class form:

```python
from blackbull import as_middleware

@as_middleware
class TimingMiddleware:
    def __init__(self, threshold_ms: float = 100.0):
        self._threshold = threshold_ms
    async def __call__(self, scope, receive, send, call_next):
        ...   # inner wrappers see dict events
```

Omit the decorator when you need raw `send` arguments — e.g. middleware used
in a deployment that never registers simplified-return handlers.  Without
`@as_middleware`, `call_next` is wired directly to the next layer with no
extra wrapping.

> **See also**: §9 Events covers the underlying hook API. 
> Middleware in this section is in fact a sugar layer over `@app.intercept('before_handler')`; for purely observational concerns (logging, metrics, tracing) prefer `@app.on(...)` from §9 — middleware semantics force every observer to run on the request critical path, and one slow observer can degrade every response.

### 4.2  Attaching middleware to a route

Middleware wraps the handler like nested shells.  The list order is
**outer-to-inner**: the first entry runs first on the way in and last on the way out.

```python
@app.route(path='/protected', middlewares=[auth_mw, logging_mw])
async def handler(scope, receive, send):
    ...
```

```
            ┌─ auth_mw ──────────────────────────────────┐
  request → │  ┌─ logging_mw ─────────────────────────┐  │ → response
            │  │  ┌─ handler ─┐                        │  │
            │  │  │  (runs)   │                        │  │
            │  │  └───────────┘                        │  │
            │  └─────────────────────────────────────── ┘  │
            └────────────────────────────────────────────── ┘
```

`auth_mw` runs first; it either short-circuits (returns without calling `call_next`)
or delegates to `logging_mw`, which then delegates to `handler`.  Post-handler code
(after `await call_next(...)`) runs in reverse order: `logging_mw` post → `auth_mw`
post.

### 4.3  Path parameters

> **Note** — Path parameters — whether from `{name}` string patterns or
> `re.compile(...)` named groups — are always injected into `scope['path_params']`,
> regardless of whether `middlewares=[...]` is present.  The handler signature is
> always `(scope, receive, send)`:
>

```python
@app.route(path='/tasks/{task_id}', methods=[HTTPMethod.DELETE])
async def delete_task(scope, receive, send):
    task_id = scope['path_params']['task_id']   # always correct
    ...
```

### 4.4  Built-in middleware

#### websocket

Consumes the initial `websocket.connect` event and sends `websocket.accept` so
the inner handler can skip that boilerplate:

```python
from blackbull.middleware import websocket
from blackbull.utils import Scheme

@app.route(path='/chat', scheme=Scheme.websocket, middlewares=[websocket])
async def chat(scope, receive, send):
    # Connection already accepted; go straight to reading messages
    while True:
        event = await receive()
        if event['type'] == 'websocket.disconnect':
            break
        await send({'type': 'websocket.send', 'text': event.get('text', '')})
```

#### compress

Compresses HTTP response bodies using the codec the client prefers (brotli > zstd >
gzip, based on `Accept-Encoding`):

```python
from blackbull.middleware import compress

@app.route(path='/data', middlewares=[compress])
async def data_handler(scope, receive, send):
    await send(Response(large_payload))
```

Brotli and zstandard are optional extras:

```bash
pip install 'blackbull[compression]'   # installs brotli + zstandard
```

`compress` skips compression when the response body is smaller than 100 bytes.
To change the threshold, construct `Compression` directly:

```python
from blackbull.middleware.compression import Compression

compress_big = Compression(min_size=4096)

@app.route(path='/large', middlewares=[compress_big])
async def large(scope, receive, send):
    ...
```

#### Session

Signed-cookie sessions: the session data lives entirely in a cookie HMAC-signed
by the server.  No server-side store, no DB, no Redis — every worker can read
any cookie as long as it shares the secret.  Trade-off: cookie size is bounded
(browsers cap to ~4 KiB) and you can't revoke a session early without rotating
the secret.

```python
from blackbull.middleware.session import Session

# Operator sets BB_SESSION_SECRET=<32-byte random> in the deployment env
app.use(Session())

# Or pass the secret explicitly (handy for tests):
app.use(Session(secret=b'<long-random-bytes>'))

@app.route(path='/')
async def index(scope, receive, send):
    scope['session']['user'] = 'alice'           # any JSON-serializable value
    await send(Response('signed in'))

@app.route(path='/whoami')
async def whoami(scope, receive, send):
    await send(Response(scope['session'].get('user', 'anonymous')))
```

`scope['session']` is a `dict` subclass that tracks whether you mutated it.
The middleware only emits `Set-Cookie` when a request handler changed the
session — read-only handlers leave the response cache-friendly.

Setting/missing secret:

* The constructor accepts `secret=` directly.
* When `secret` is `None`, the middleware reads `BB_SESSION_SECRET` from the
  environment.
* If neither is set, construction raises — there is no insecure default.
  Generate one with `python -c "import secrets; print(secrets.token_urlsafe(32))"`.

Cookie attributes (all keyword arguments, with sensible defaults):

| Argument        | Default  | Notes                                                                  |
|-----------------|----------|------------------------------------------------------------------------|
| `cookie_name`   | `'session'` | Name of the cookie carrying the payload.                            |
| `max_age`       | `None`   | Seconds the cookie is valid.  When set, a server-signed timestamp is included; older cookies are rejected.  `None` ⇒ session cookie (lives until the browser closes). |
| `secure`        | `True`   | Send the `Secure` attribute (cookie only over HTTPS).  Disable for local-only dev. |
| `httponly`      | `True`   | Send the `HttpOnly` attribute (JS can't read the cookie).             |
| `samesite`      | `'Lax'`  | `'Strict'`, `'Lax'`, `'None'`, or `None` (omit the attribute).        |
| `path`          | `'/'`    | Cookie `Path`.                                                        |

Clearing the session emits a tombstone cookie with `Max-Age=0`, telling the
browser to drop it:

```python
@app.route(path='/logout')
async def logout(scope, receive, send):
    scope['session'].clear()
    await send(Response('signed out'))
```

A cookie whose signature fails to verify (tampering, wrong secret, replay with
a different `cookie_name`) is silently dropped — the handler sees an empty
session, no error is propagated.

#### Cache

Per-worker in-memory response cache for `GET` and `HEAD`.  The middleware
captures the handler's response on the first hit, stores it under
`(method, path, query_string)`, and replays it directly on subsequent
matching requests until the entry expires.

```python
from blackbull.middleware.cache import Cache

app.use(Cache(max_age=600))   # 10-minute TTL by default

@app.route(path='/feed')
async def feed(scope, receive, send):
    items = await fetch_news()  # expensive
    body = render(items).encode()
    await send({'type': 'http.response.start', 'status': 200,
                'headers': [(b'content-type', b'text/html')]})
    await send({'type': 'http.response.body', 'body': body})
```

A weak ETag (`W/"<sha256-prefix>"`) is generated automatically when the
handler doesn't supply one.  Subsequent requests that send a matching
`If-None-Match` header receive a `304 Not Modified` with no body:

```bash
$ curl -i http://localhost:8000/feed
HTTP/1.1 200 OK
content-type: text/html
etag: W/"a1b2c3d4e5f60718"
...

$ curl -i -H 'If-None-Match: W/"a1b2c3d4e5f60718"' http://localhost:8000/feed
HTTP/1.1 304 Not Modified
etag: W/"a1b2c3d4e5f60718"
```

Standard `Cache-Control` directives are honoured.  Responses carrying
`no-store`, `private`, or `no-cache` are passed through and never
stored.  Requests with `Cache-Control: no-store` bypass the cache too.
Response `Cache-Control: max-age=N` (and `s-maxage=N`, which takes
precedence) shortens the cache lifetime below the middleware's default.

Constructor arguments:

| Argument               | Default                | Notes                                                                  |
|------------------------|------------------------|------------------------------------------------------------------------|
| `max_age`              | `300`                  | TTL in seconds when the response does not specify its own.            |
| `max_entries`          | `1024`                 | LRU cap on the number of cached responses.  Oldest evicted first.     |
| `cacheable_methods`    | `{'GET', 'HEAD'}`      | Methods eligible for caching.                                          |
| `cacheable_statuses`   | `{200, 203, 300, 301, 308, 404, 410, 414, 451}` | Status codes eligible for caching.       |
| `cache_authenticated`  | `False`                | When `False`, requests with `Authorization` bypass the cache (RFC 9111 §3.5). |
| `generate_etag`        | `True`                 | Auto-generate `ETag` when the handler omits it.                        |

Streaming responses (any body chunk sent with `more_body=True`) are
forwarded straight to the client without caching — the body size is
unknown ahead of time and hashing it post-hoc would defeat the
streaming.

Limitations to know:

* **Per-worker.**  Multi-worker deployments hold a separate cache in
  each process; a refresh in one worker doesn't propagate to the others.
* **No cross-restart persistence.**  In-memory only; a worker restart
  drops everything.
* **No `Vary` matching.**  Requests that differ only by `Accept-Encoding`
  or `Accept-Language` share the same cache slot — the first response
  cached wins.  Don't use `Cache` for content-negotiated
  routes unless the negotiation is path/query-string-driven.
* **No explicit invalidation API.**  Wait for TTL or restart the worker.

### 4.5  Injecting values into scope

Middleware may add any key to `scope` for inner layers to consume:

```python
async def auth_mw(scope, receive, send, call_next):
    auth = scope['headers'].get(b'authorization', b'')
    token = auth[7:].decode() if auth.startswith(b'Bearer ') else ''
    user = SESSIONS.get(token)
    if not user:
        await send(JSONResponse({'error': 'Unauthorized'},
                                status=HTTPStatus.UNAUTHORIZED))
        return                  # short-circuit: inner layers never run
    scope['user'] = user        # available to all inner layers
    scope['token'] = token
    await call_next(scope, receive, send)
```

---

## §5  Route Groups

`app.group(middlewares=[...])` returns a `RouteGroup` whose `.route()` method
prepends the group's middlewares to every route registered through it.

```python
public    = app.group(middlewares=[error_mw, logging_mw])
protected = app.group(middlewares=[error_mw, logging_mw, auth_mw])

@public.route(methods=[HTTPMethod.GET], path='/')
async def index(scope, receive, send):
    await send(Response(b'<h1>Login</h1>'))

@protected.route(methods=[HTTPMethod.GET], path='/tasks')
async def get_tasks(scope, receive, send):
    await send(JSONResponse([]))
```

Per-route `middlewares=[...]` are **appended after** the group middlewares:

```python
# Effective chain: error_mw → logging_mw → auth_mw → json_body_mw → create_task
@protected.route(methods=[HTTPMethod.POST], path='/tasks',
                 middlewares=[json_body_mw])
async def create_task(scope, receive, send):
    ...
```

---

## §6  Request Helpers

### 6.1  Reading the request body

```python
from blackbull import read_body

raw: bytes = await read_body(receive)
```

Reads all body chunks until `more_body=False`.  The stream is consumed — call at
most once per request (typically inside a middleware, not the handler).

For streaming uploads, call `receive()` directly — each call returns one chunk
with `more_body=True` until the final chunk arrives with `more_body=False`.
`read_body` is a convenience wrapper that buffers all chunks into a single `bytes`
object before returning.

### 6.2  JSON body — recommended middleware pattern

```python
import json

async def json_body_mw(scope, receive, send, call_next):
    raw = await read_body(receive)
    try:
        scope['json'] = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        await send(JSONResponse({'error': 'Invalid JSON'},
                                status=HTTPStatus.BAD_REQUEST))
        return
    await call_next(scope, receive, send)
```

The handler then reads `scope['json']` without touching `receive`.

### 6.3  Reading request headers

`scope['headers']` is a `Headers` object with the following API:

```python
# First value for a header; returns b'' when absent
ct   = scope['headers'].get(b'content-type')
auth = scope['headers'].get(b'authorization', b'')

# All (name, value) pairs for a header (multi-value support)
pairs = scope['headers'].getlist(b'accept')   # list[tuple[bytes, bytes]]

# ASGI-compliant iteration
for name, value in scope['headers']:
    ...

# Membership test
if b'content-length' in scope['headers']:
    ...
```

Header names are **case-insensitive** and stored lowercase.

#### `get` vs `getlist`

Use `.get(name)` for headers that appear at most once (e.g. `content-type`,
`authorization`, `host`).  Use `.getlist(name)` for headers that may repeat:

| Header | Why it can repeat |
|---|---|
| `accept`, `accept-encoding` | Clients may send multiple preference lines |
| `set-cookie` | Servers send one `Set-Cookie` field per cookie |
| `cookie` (HTTP/2) | RFC 7540 §8.1.2.5 requires one field per cookie pair |

`.getlist` returns `list[tuple[bytes, bytes]]` — the full `(name, value)` pairs
in insertion order, or `[]` if the header is absent.  `.get` returns only the
first value as `bytes`.

> **Why `cookie` needs `getlist` on HTTP/2:** HTTP/1.1 combines all cookies
> into a single `Cookie: a=1; b=2` field.  HTTP/2 must send each cookie as a
> separate header field to enable HPACK compression of individual values.
> Calling `.get(b'cookie')` on an HTTP/2 scope silently discards all but the
> first cookie.  `parse_cookies` (§6.4) handles this correctly for all
> protocols.

### 6.4  Parsing cookies

```python
from blackbull import parse_cookies

cookies: dict[str, str] = parse_cookies(scope)
session = cookies.get('session', '')
```

`parse_cookies` works for HTTP/1.1, HTTP/2, and WebSocket scopes — all three
put a `Headers` object in `scope['headers']`.  Internally it calls
`.getlist(b'cookie')` and joins all fields before parsing, so the result is
identical regardless of how the client sent the cookies.

### 6.5  Query parameters

`scope['query_string']` contains the raw query string as bytes (everything after
`?` in the URL).  Parse it with the standard library:

```python
from urllib.parse import parse_qs, parse_qsl

# parse_qs: each key maps to a list of values (handles ?tag=a&tag=b correctly)
params = parse_qs(scope['query_string'].decode())
page   = int(params.get('page', ['1'])[0])
tags   = params.get('tag', [])             # ['a', 'b'] for ?tag=a&tag=b

# parse_qsl: flat list of (key, value) pairs preserving order
pairs = parse_qsl(scope['query_string'].decode())
```

For convenience, wrap this in a middleware or a per-handler helper:

```python
def qp(scope) -> dict[str, str]:
    """Return first value for each query parameter key."""
    return {k: v[0] for k, v in parse_qs(scope['query_string'].decode()).items()}

@app.route(path='/tasks')
async def list_tasks(scope, receive, send):
    p = qp(scope)
    done = p.get('done', 'false').lower() == 'true'
    await send(JSONResponse({'done_filter': done}))
```

### 6.6  Form data

HTML forms with `enctype="application/x-www-form-urlencoded"` (the default) send
key=value pairs in the body.  Read and parse with `read_body` + `parse_qs`:

```python
from urllib.parse import parse_qs
from blackbull import read_body

async def form_body_mw(scope, receive, send, call_next):
    """Parse application/x-www-form-urlencoded body; inject scope['form']."""
    raw = await read_body(receive)
    scope['form'] = {k: v[0] for k, v in parse_qs(raw.decode()).items()}
    await call_next(scope, receive, send)

@app.route(methods=[HTTPMethod.POST], path='/submit', middlewares=[form_body_mw])
async def submit(scope, receive, send):
    name = scope['form'].get('name', '')
    await send(JSONResponse({'received': name}))
```

> Multipart file uploads (`multipart/form-data`) are not yet supported by a
> built-in helper.  Use the `python-multipart` package to parse the body manually.

---

## §7  Responses

### 7.1  HTML / plain text / redirects

```python
from blackbull import Response
from http import HTTPStatus

await send(Response(b'<h1>Hello</h1>'))
await send(Response('Hello', status=HTTPStatus.OK))           # str also accepted
await send(Response(b'Not found', status=HTTPStatus.NOT_FOUND))

# Redirect
await send(Response(b'', status=HTTPStatus.FOUND,
                    headers=[(b'location', b'/')]))
```

Default `content_type` is `'text/html; charset=utf-8'`.  Override via the
`content_type` parameter:

```python
Response(b'plain text', content_type='text/plain; charset=utf-8')
```

### 7.2  JSON

```python
from blackbull import JSONResponse

await send(JSONResponse({'ok': True}))
await send(JSONResponse({'error': 'Bad request'}, status=HTTPStatus.BAD_REQUEST))
await send(JSONResponse({'id': 1, 'title': 'Buy milk'}, status=HTTPStatus.CREATED))
```

Content-Type is set to `application/json` automatically.

### 7.3  Custom response headers

Both `Response` and `JSONResponse` accept `headers=[(bytes, bytes), ...]`:

```python
await send(JSONResponse({'ok': True}, headers=[
    (b'x-request-id', b'abc123'),
    (b'cache-control', b'no-store'),
]))
```

### 7.4  Set-Cookie helper

```python
from blackbull import cookie_header

hdr = cookie_header('session', token, http_only=True)
# → (b'set-cookie', b'session=TOKEN; Path=/; HttpOnly; SameSite=Lax')

await send(JSONResponse({'ok': True}, headers=[hdr]))
```

Signature: `cookie_header(name, value, path='/', http_only=True)`.

> **Note for browser clients over plain HTTP**: browsers may not reliably forward
> `HttpOnly` cookies set by a `fetch()` response on the next page navigation.
> For single-page apps, store the session token in `sessionStorage` and send it
> as `Authorization: Bearer <token>` instead (see §4.5 example).

### 7.5  HTTP trailers

HTTP/1.1 chunked responses can carry trailing headers after the body.  Use the
`http.response.trailers` event after the last `http.response.body` chunk:

```python
@app.route(path='/chunked')
async def chunked(scope, receive, send):
    await send({
        'type': 'http.response.start',
        'status': 200,
        'headers': [
            (b'content-type',     b'text/plain'),
            (b'transfer-encoding', b'chunked'),
            (b'trailer',          b'x-checksum'),
        ],
    })
    await send({
        'type': 'http.response.body',
        'body': b'chunk data here',
        'more_body': True,
    })
    await send({
        'type': 'http.response.trailers',
        'headers': [(b'x-checksum', b'abc123')],
    })
```

### 7.6  WebSocket frames

```python
from blackbull import WebSocketResponse

await send(WebSocketResponse('hello'))           # str  → text frame
await send(WebSocketResponse(b'\x00\x01'))       # bytes → binary frame
await send(WebSocketResponse({'type': 'msg'}))   # other → JSON-serialised text frame
```

(See also §3.3 and §4.4 for WebSocket routing and the built-in `websocket` middleware.)




---

## §8  Error Handling

### 8.1  Default behaviour

| Condition | Default response |
|---|---|
| Path not registered | 404 plain text |
| Method not allowed | 405 plain text + `Allow` header |
| Unhandled exception in handler | 500 plain text with exception class and message |

### 8.2  Custom error handlers

```python
@app.on_error(HTTPStatus.NOT_FOUND)
async def handle_404(scope, receive, send):
    await send(JSONResponse({'error': 'not found'}, status=HTTPStatus.NOT_FOUND))

@app.on_error(HTTPStatus.METHOD_NOT_ALLOWED)
async def handle_405(scope, receive, send):
    allowed = ', '.join(scope['state'].get('allowed_methods', ()))
    await send(JSONResponse({'error': f'allowed: {allowed}'},
                            status=HTTPStatus.METHOD_NOT_ALLOWED))

@app.on_error(ValueError)
async def handle_value_error(scope, receive, send):
    exc = scope['state'].get('error_exception')
    await send(JSONResponse({'error': str(exc)}, status=HTTPStatus.BAD_REQUEST))
```

Exception handlers use MRO walk: a handler registered for `Exception` catches all
unhandled subclasses.

**`scope['state']` keys set by the framework on errors:**

| Key | Type | Present when |
|---|---|---|
| `'error_status'` | `HTTPStatus` | Always |
| `'error_exception'` | exception | Triggered by uncaught exception |
| `'allowed_methods'` | tuple of str | 405 Method Not Allowed |

---

## §9 Events

BlackBull exposes an event-driven hook API on top of (and integrated with) the ASGI request lifecycle.  Application code registers handlers with one of two decorators — `@app.intercept(...)` for synchronous interception, or `@app.on(...)` for asynchronous observation — and the framework dispatches events to them at well-defined points in the application's lifetime.

### 9.1 Overview — interception vs observation

There are two kinds of hook, with deliberately different semantics:

| Property | `@app.intercept(name)` | `@app.on(name)` |
| --- | --- | --- |
| Execution | Synchronous (awaited inline) | Asynchronous (fire-and-forget) |
| Order | Registration order, serial | Independent, no inter-handler order |
| Exceptions | Propagate to the emitter | Isolated, logged, never propagate |
| Can short-circuit / modify flow | Yes | No |
| Typical use | Auth, validation, rewriting | Logging, metrics, tracing |

The split is a defence against subtle bugs.  Writing an authentication check as an observer would silently let unauthorized requests through (the request proceeds before the observer has finished, and the observer cannot signal failure to the emitter).  Conversely, putting slow telemetry into an interceptor would block the request path on every emission.

Note that BlackBull does not provide hooks to "observe but also block" or "intercept but also fire-and-forget".

### 9.2 The `@app.intercept` decorator

Register a synchronous interceptor for a named event:

```python
from blackbull import BlackBull, Event

app = BlackBull()

@app.intercept('app_startup')
async def warm_cache(event: Event):
    await cache.preload()
```

Interceptors are awaited in registration order during emission.  An exception raised by an interceptor:

* aborts the remaining interceptors for that event, and
* propagates to the code that called `emit`.

For lifespan events, that means an interceptor exception will surface to the ASGI server driving the lifespan protocol — typically aborting startup.

### 9.3 The `@app.on` decorator

Register an asynchronous observer for a named event:

```python
@app.on('app_shutdown')
async def flush_metrics(event: Event):
    await metrics_client.flush()
```

Observers are scheduled with `asyncio.create_task` when the event is emitted and run independently of the emitter.  They cannot block the emitter, and their exceptions are caught, logged on the `blackbull` logger at `ERROR`, and discarded — they cannot surface to the emitter or to other observers.

Use observers for telemetry, audit logging, cache warming, and other side-effects that the request path should not depend on.

### 9.4 The `Event` object

Both decorators register handlers with the signature `async def handler(event: Event)`.  `Event` is a frozen dataclass:

```python
from dataclasses import dataclass, field

@dataclass(frozen=True)
class Event:
    name: str
    detail: dict = field(default_factory=dict)
```

`name` identifies which event fired; `detail` carries event-specific data (for example, a `request_completed` event may carry timing information in its `detail`).  The catalogue of event names and their `detail` shape is in §9.6.

### 9.5 Lifespan events — `app_startup` / `app_shutdown`

The two lifespan events fire once per server lifetime and correspond directly to the ASGI `lifespan.startup` / `lifespan.shutdown` messages.

For the common case where you do not need access to the `Event` object, two zero-argument sugar decorators are provided:

```python
@app.on_startup
async def init_db():
    await db.connect()

@app.on_shutdown
async def close_db():
    await db.disconnect()
```

These are exact equivalents of:

```python
@app.intercept('app_startup')
async def _wrapped_init_db(event: Event):
    await init_db()

@app.intercept('app_shutdown')
async def _wrapped_close_db(event: Event):
    await close_db()
```

Use the sugar form when your handler does not need the event itself; reach for `@app.intercept('app_startup')` directly when you want the consistency of writing every hook as `(event: Event) -> None`.

### 9.6 Event reference

Events that application code can subscribe to.  Internal Level A events used by the framework's own components are documented separately and are not subscribable from application code.

| Event name | Fires when | `detail` keys | Notes |
| --- | --- | --- | --- |
| `app_startup` | Server has bound its socket and is about to accept connections | *(empty)* | Sugar: `@app.on_startup` |
| `app_shutdown` | Server has received a stop signal and is about to exit | *(empty)* | Sugar: `@app.on_shutdown` |
| `websocket_message` | A WebSocket message has been fully received and reassembled by the server, before the ASGI handler reads it via `receive()` | `scope` (the connection scope dict), `text` (`str` for text frames, `None` otherwise), `bytes` (`bytes` for binary frames, `None` otherwise) | Observation only — see §9.6.1 |
| `request_received` | HTTP request headers parsed, before routing and handler dispatch | `scope`, `client_ip`, `method`, `path`, `http_version`, `headers` | Supports both `@app.on` and `@app.intercept` — see §9.6.4 |
| `request_completed` | An HTTP request has finished — the response has been fully sent (or the request failed before that, e.g. 404 or unhandled exception).  **Not** fired if the client disconnected before the response completed, and **not** fired for WebSocket connections | `scope`, `client_ip`, `method`, `path`, `http_version`, `status` (`int` or `'-'`), `response_bytes` (`int`), `duration_ms` (`float`) | Observation only — see §9.6.2 |
| `request_disconnected` | An HTTP client closed the connection before the response was fully sent | `scope`, `client_ip`, `method`, `path`, `http_version` | Observation only — see §9.6.3 |

| `before_handler` | Route matched, immediately before the handler (and its middleware chain) is called | `scope`, `client_ip`, `method`, `path`, `http_version`, `headers` | Supports both `@app.on` and `@app.intercept` — raise to abort the request |
| `after_handler` | Handler (and its middleware chain) has returned normally | `scope`, `client_ip`, `method`, `path`, `http_version` | Observation only |
| `websocket_connected` | WebSocket connection accepted (`websocket.accept` sent) | `connection_id`, `path`, `client_ip`, `subprotocol` | Observation only |
| `websocket_disconnected` | WebSocket connection closed or dropped | `connection_id`, `code` | Observation only |

#### 9.6.1 `websocket_message` — observation only

`websocket_message` fires once per fully reassembled WebSocket message, inside the server read loop, *before* the message is delivered to the application's `receive()` call.  Observers therefore see every message the server accepted — including ones the handler never reads.

```python
@app.on('websocket_message')
async def log_message(event: Event):
    if event.detail['text'] is not None:
        print(f"[ws] text on {event.detail['scope']['path']}: {event.detail['text']!r}")
    else:
        print(f"[ws] binary on {event.detail['scope']['path']}: {len(event.detail['bytes'])} bytes")
```

**This event is observation only.**  Because the message has already been read off the wire and buffered, interceptors registered with `@app.intercept('websocket_message')` cannot suppress delivery to the application handler.  Use the observe path (`@app.on`) for analytics, logging, and monitoring.

!!! note "Per-connection identity"
    The `scope` dict identifies the connection path and headers but does not carry a unique connection ID.  Use the `websocket_connected` event (§9.6.5) which provides a `connection_id` for per-connection identity.

#### 9.6.2 `request_completed` — observation only

`request_completed` fires once per HTTP request, after the response has been sent (or after the request has otherwise concluded — for example, after a 404 or after an unhandled exception in the handler).  It fires from the same site as the `blackbull.access` log record, so every request that produces an access-log entry also produces this event.

`detail` carries both the connection `scope` and the same flattened fields the access log carries:

| Key | Type | Description |
| --- | --- | --- |
| `scope` | `dict` | The ASGI scope dict for the request |
| `client_ip` | `str` | Remote address (`'-'` when unavailable) |
| `method` | `str` | HTTP method (e.g. `'GET'`) |
| `path` | `str` | Request path (e.g. `'/api/items'`) |
| `http_version` | `str` | Protocol version (e.g. `'1.1'`, `'2'`) |
| `status` | `int` \| `str` | Response status code, or `'-'` if not sent |
| `response_bytes` | `int` | Total body bytes written to the wire |
| `duration_ms` | `float` | Wall-clock duration from first byte received to response complete, in milliseconds |

```python
@app.on('request_completed')
async def log_request(event: Event):
    d = event.detail
    print(f"{d['method']} {d['path']} → {d['status']} ({d['duration_ms']:.1f}ms)")
```

Use `scope` when you need request context beyond the flat fields — for example, path parameters injected by the router, or a trace span stored in `scope['state']`.

**This event is observation only.**  The response has already been sent by the time the event fires, so nothing an interceptor registered with `@app.intercept('request_completed')` can do will change what the client received.  The framework makes no guarantees about:

- whether mutations to `detail` are propagated anywhere;
- whether raising from an interceptor changes the response;
- ordering between an interceptor and shutdown or other completion steps.

If you need to act *during* the request (authentication, header rewriting, validation), use `@app.intercept('before_handler')` (§9.6.4) — it runs synchronously before the handler and can abort the request by raising.

#### 9.6.3 `request_disconnected` — observation only

`request_disconnected` fires when an HTTP client closes the connection before the response has been fully sent.  It is **mutually exclusive** with `request_completed`: a request that disconnected does not also fire `request_completed`.  The framework still emits the `blackbull.access` log record for the request (with status `'-'` per §14.1).

`detail` carries the request identification fields but **not** response-side fields (status, response_bytes, duration_ms) — those are not meaningful for an aborted request:

| Key | Type | Description |
| --- | --- | --- |
| `scope` | `dict` | The ASGI scope dict for the request |
| `client_ip` | `str` | Remote address (`'-'` when unavailable) |
| `method` | `str` | HTTP method |
| `path` | `str` | Request path |
| `http_version` | `str` | Protocol version |

```python
@app.on('request_disconnected')
async def on_disconnect(event: Event):
    d = event.detail
    print(f"Client disconnected from {d['method']} {d['path']}")
```

The event fires when the server detects the disconnect via the application's `receive()` call — specifically when `receive()` returns `{'type': 'http.disconnect'}`.  Long-polling or SSE handlers that call `receive()` to watch for disconnect will trigger this event the moment the client closes the connection.

This applies to **both HTTP/1.1 and HTTP/2**.  For HTTP/2, when the underlying TCP connection closes while streams are still in flight (the common case for SSE), the framework injects an `http.disconnect` event into every active stream's receive channel.  Handlers using the `_watch_disconnect` pattern (calling `await receive()` in a concurrent coroutine) will unblock immediately.

**This event is observation only.**  Although `@app.intercept('request_disconnected')` will accept a registration without error, the disconnect has already occurred by the time the event fires; the framework does not guarantee that interceptor side-effects affect anything visible to the client.

WebSocket connections never fire `request_disconnected`.  Use `websocket_disconnected` instead.

#### 9.6.4 `request_received`

`request_received` fires at the earliest point an HTTP request is visible to application code — after the request line and headers are parsed but before routing or handler dispatch.  It fires for both HTTP/1.1 and HTTP/2 (once per stream).  WebSocket connections never fire this event.

**Ordering guarantee:**  `request_received` → routing → `before_handler` → handler → `request_completed`.

| detail key | type | value |
| --- | --- | --- |
| `scope` | `dict` | The full ASGI scope for this request |
| `client_ip` | `str` | Remote address |
| `method` | `str` | e.g. `'GET'` |
| `path` | `str` | e.g. `'/api/users'` |
| `http_version` | `str` | e.g. `'1.1'` or `'2'` |
| `headers` | `Headers` | Parsed request headers |

Use `@app.intercept('request_received')` for early gates (authentication, rate limiting) — the interceptor runs synchronously before the route handler, so returning without raising short-circuits the request.  Use `@app.on('request_received')` for passive observation (metrics, logging).

```python
@app.intercept('request_received')
async def require_api_key(event):
    headers = event.detail['scope']['headers']
    if headers.get(b'x-api-key') != b'secret':
        raise PermissionError('missing or invalid API key')
    # returning normally allows the request to proceed

@app.on('request_received')
async def record_hit(event):
    metrics.increment('http.requests', tags={'path': event.detail['path']})
```

#### 9.6.5 `websocket_connected` and `websocket_disconnected`

`websocket_connected` fires once per connection, immediately after the server sends the `websocket.accept` event.  `websocket_disconnected` fires when the server detects the close, whether the client or the handler closed it.

Both events carry a `connection_id` (a UUID string) that is stable for the lifetime of the connection — you can correlate `connected` and `disconnected` records to compute connection duration.

| Event | Key `detail` fields |
|---|---|
| `websocket_connected` | `connection_id`, `path`, `client_ip`, `subprotocol` |
| `websocket_disconnected` | `connection_id`, `code` |

```python
@app.on('websocket_connected')
async def on_connected(event):
    logger.info('WS connected id=%s path=%s',
                event.detail['connection_id'], event.detail['path'])

@app.on('websocket_disconnected')
async def on_disconnected(event):
    logger.info('WS disconnected id=%s code=%s',
                event.detail['connection_id'], event.detail['code'])
```

Both events are **observation only** — the connection lifecycle is driven by the ASGI handler, not by interceptors.

### 9.7 Exception handling

The two hook kinds have deliberately different exception semantics, summarised in §9.1.  Concretely:

* **Interceptor raises** → remaining interceptors for that event do not run; the exception propagates to the emitter (the framework code that called `emit`).  For lifespan events, this typically aborts startup or shutdown.
* **Observer raises** → the exception is caught and logged at `ERROR` on the `blackbull` logger; other observers for the same event continue to run; the emitter never sees the exception.

There is no built-in re-emission of failures as a separate `error` event yet.  If you want one, register a wrapper observer that emits an application-defined event in its own `except` block.

### 9.8 Observer task lifecycle at shutdown

Observers run as detached `asyncio.Task`s, which means in-flight observers can outlive the request that triggered them.  At `app_shutdown`, BlackBull waits up to `observer_shutdown_timeout` seconds (default 5, set in `BlackBull(observer_shutdown_timeout=...)`) for any still-running observer tasks to finish.  Any task still running after the timeout is cancelled and a `WARNING` is logged on the `blackbull` logger naming the unfinished coroutine.

Observers therefore have a *conditional* fire-and-forget guarantee: during normal request processing they do not block the emitter, but during shutdown they are expected to finish promptly.  If you have observation work that legitimately takes longer than a few seconds (for example, sending events to a remote analytics endpoint), enqueue the work from the observer into your own queue rather than performing it inline — that way the queue can outlive shutdown if you arrange for it to.

---

## §10  Running the Server

BlackBull exposes three entry points, in increasing order of how
"production-shaped" they are:

```python
import asyncio
from blackbull import serve  # module-level function; works with any ASGI app

# 1. In-Python, async — useful inside notebooks, test harnesses, embedded callers
asyncio.run(app.run(port=8000))
asyncio.run(app.run(port=8443, certfile='cert.pem', keyfile='key.pem'))

# 2. In-Python, sync — typical for `python myapp.py` style scripts
app.serve(port=8443, certfile='cert.pem', keyfile='key.pem', workers=4)

# Development: hot-reload when *.py files change.
# Uses watchfiles + master-keeps-listening-sockets across worker recycles.
# Requires the [reload] extra: pip install -e '.[reload]'
app.serve(port=8000, reload=True)
```

```bash
# 3. Console script — same flags work for any ASGI 3.0 callable.
#    Useful for systemd / Docker / PaaS where a CLI is the deployment surface.
blackbull myapp:app --bind 0.0.0.0:8443 \
    --certfile cert.pem --keyfile key.pem --workers 4

# Auto-reload via the CLI:
blackbull myapp:app --bind 127.0.0.1:8000 --reload
```

The CLI accepts any ``module:attribute`` import path; the attribute may
be a :class:`BlackBull` instance *or* a plain ASGI callable (which is
how the benchmark harness loads ``bench/peers/asgi_app.py`` against
every peer server including BlackBull).

`app.run()` signature:

```python
async def run(port=0, certfile=None, keyfile=None,
              workers=1, max_connections=None,
              stream_queue_depth=None, ws_queue_depth=None)
```

`app.serve()` is the synchronous counterpart; pass ``reload=True`` to
enable auto-reload (only available via ``serve()`` because it needs a
long-lived supervisor process around the event loop).  All three
entry points ultimately call :func:`blackbull.serve`, which is the
module-level function the CLI dispatches to — embedded users can
import it directly to serve a raw ASGI callable without instantiating
a :class:`BlackBull`.

### §10.1  Workers and the event loop

By default `app.run()` runs one worker process with the standard asyncio
event loop.  Two environment variables let you change that:

```bash
# Pre-fork N worker processes, each with its own event loop.
# 0 → os.cpu_count() workers.
BB_WORKERS=8 python app.py

# Install uvloop's faster asyncio policy (requires pip install "blackbull[speed]").
BB_UVLOOP=1 python app.py

# Both together (production default).
BB_WORKERS=0 BB_UVLOOP=1 python app.py
```

Multi-worker uses **pre-fork multiprocessing** — each worker is a separate OS
process, not a thread.  The master process binds the socket, forks workers,
and then sleeps until SIGTERM/SIGINT.  On Linux/modern BSDs the workers also
share the listening port via `SO_REUSEPORT` (`BB_SOCKET_REUSEPORT=1` by default)
so the kernel hashes incoming connections across workers' accept queues —
eliminates thundering-herd and gives per-connection CPU affinity for free.

The workers share **nothing** mutable: no shared dict, no shared cache, no
shared lock.  Anything that would otherwise need cross-worker coordination
(session storage, response cache, rate-limit counters) belongs in an external
store — Redis, Postgres, or a sticky-session reverse proxy.  See §4.4 — the
built-in `Cache` and `Session` middlewares state their per-worker limits.

| Setting | Use case |
|---|---|
| `BB_WORKERS=1` (default) | Development, tests, debugging.  Easiest to reason about; everything in one process. |
| `BB_WORKERS=N` (`N > 1`) | Production CPU-bound workload — each worker saturates one core. |
| `BB_WORKERS=0` | Production "use whatever the box has" — resolves to `os.cpu_count()` at start. |
| `BB_UVLOOP=1` | Always worth it once you've installed `blackbull[speed]`.  1.5–2× throughput on HTTP/2 hot paths in our benchmarks. |

See §18 for the full list of runtime tunables.

### Generating a self-signed certificate for local HTTPS

```bash
openssl req -x509 -newkey rsa:4096 -keyout key.pem -out cert.pem \
  -days 365 -nodes -subj '/CN=localhost' \
  -addext 'subjectAltName=DNS:localhost,IP:127.0.0.1'
```

Then start the server:

```bash
python app.py --cert cert.pem --key key.pem --port 8443
# Open https://localhost:8443  (accept the browser security warning once)
```

> Browsers only use HTTP/2 over HTTPS.  With TLS the server negotiates HTTP/2
> automatically via ALPN; plain HTTP connections use HTTP/1.1.

### §10.2  Production deployment

BlackBull binds directly to a TCP port.  For production, run it behind a reverse
proxy (**nginx**, **Caddy**) that handles TLS termination, static files, and load
balancing across multiple processes.

#### Running BlackBull behind Nginx

Start BlackBull **without** TLS — Nginx handles certificates:

```bash
python app.py --port 8000   # plain HTTP/1.1; no --cert / --key
```

Nginx terminates TLS and HTTP/2 toward clients, then proxies to BlackBull over
HTTP/1.1.  All three connection types (HTTP, WebSocket, SSE) work through this
setup.

**Complete Nginx configuration:**

```nginx
upstream blackbull {
    server 127.0.0.1:8000;
    keepalive 64;
}

server {
    listen 443 ssl;
    http2 on;
    server_name example.com;

    ssl_certificate     /etc/letsencrypt/live/example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/example.com/privkey.pem;

    # ── Regular HTTP requests ────────────────────────────────────────────────
    location / {
        proxy_pass         http://blackbull;
        proxy_http_version 1.1;
        proxy_set_header   Host              $host;
        proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;
        proxy_set_header   Connection        "";   # enable keep-alive to upstream
    }

    # ── WebSocket ────────────────────────────────────────────────────────────
    # WebSocket requires HTTP/1.1 Upgrade; match paths that need it explicitly.
    location /ws {
        proxy_pass         http://blackbull;
        proxy_http_version 1.1;
        proxy_set_header   Host       $host;
        proxy_set_header   Upgrade    $http_upgrade;
        proxy_set_header   Connection "upgrade";
        proxy_read_timeout 3600s;   # keep WS connection open
    }

    # ── Server-Sent Events ───────────────────────────────────────────────────
    location /sse {
        proxy_pass            http://blackbull;
        proxy_http_version    1.1;
        proxy_set_header      Host $host;
        proxy_set_header      Connection "";
        proxy_buffering       off;   # flush SSE events immediately
        proxy_cache           off;
        proxy_read_timeout    3600s;
        chunked_transfer_encoding on;
    }
}

# Redirect plain HTTP to HTTPS
server {
    listen 80;
    server_name example.com;
    return 301 https://$host$request_uri;
}
```

#### Trusted-proxy headers

By default, `scope['client']` is the raw TCP peer (Nginx's loopback address) and
`scope['scheme']` is `'http'` even when the client connected via HTTPS.  Enable
`TrustedProxy` to rewrite them from the proxy headers:

```python
# Shortcut on BlackBull — registers TrustedProxy automatically
app = BlackBull(trusted_proxies=['127.0.0.1', '::1'])

# Or register explicitly for more control (e.g. private subnet):
from blackbull import TrustedProxy
app.use(TrustedProxy(['127.0.0.1', '::1', '10.0.0.0/8']))
```

With trusted-proxy support active:

| `scope` key | Without middleware | With middleware |
|---|---|---|
| `scope['client']` | Nginx's loopback IP | real client IP (from `X-Forwarded-For`) |
| `scope['scheme']` | `'http'` | `'https'` (from `X-Forwarded-Proto`) |

Supported headers (in precedence order):

1. RFC 7239 `Forwarded` — `for=<ip>; proto=<scheme>`
2. `X-Forwarded-For` — comma-separated chain; leftmost non-trusted IP wins
3. `X-Forwarded-Proto`

Headers are **only applied when the direct TCP peer is in the trusted set**, preventing
clients from spoofing their IP by forging `X-Forwarded-For`.

**Docker one-liner:**

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY . .
RUN pip install .
EXPOSE 8000
CMD ["python", "app.py", "--port", "8000"]
```

**Environment variables** for secrets (never hardcode):

```python
import os
DB_URL  = os.environ['DATABASE_URL']
SECRET  = os.environ['SECRET_KEY']
PORT    = int(os.environ.get('PORT', 8000))
```

### §10.3  Mutual TLS (mTLS)

mTLS requires clients to present a certificate signed by a trusted CA.  Set it up
before starting the server:

```python
import asyncio
from blackbull import BlackBull

app = BlackBull()

# ... define routes ...

# Create the server manually so we can configure mTLS before accepting connections
app.create_server(certfile='cert.pem', keyfile='key.pem', port=8443)
app.server.configure_mtls(ca_cert='ca.pem')   # enables CERT_REQUIRED
asyncio.run(app.run())
```

`configure_mtls` raises `RuntimeError` if called before TLS is configured (i.e.
before `certfile` and `keyfile` are provided).

Generate a test CA and client certificate:

```bash
# CA
openssl req -x509 -newkey rsa:4096 -keyout ca.key -out ca.pem \
  -days 365 -nodes -subj '/CN=Test CA'

# Server cert signed by the CA
openssl req -newkey rsa:4096 -keyout key.pem -out server.csr \
  -nodes -subj '/CN=localhost'
openssl x509 -req -in server.csr -CA ca.pem -CAkey ca.key \
  -CAcreateserial -out cert.pem -days 365

# Client cert signed by the CA
openssl req -newkey rsa:4096 -keyout client.key -out client.csr \
  -nodes -subj '/CN=client'
openssl x509 -req -in client.csr -CA ca.pem -CAkey ca.key \
  -CAcreateserial -out client.pem -days 365
```

### §10.4  Abuse defences

Three independent limits protect a worker from misbehaving or malicious clients.
All three apply *before* the application is reached, and all three are on by
default — production deployments should leave them on and only tune the numbers.

#### Slowloris — partial-headers attack

A slowloris attack keeps a TCP connection open by sending the request header
block one byte at a time, indefinitely.  Without a deadline, every such
connection holds one worker slot forever — a single attacker can exhaust the
server's connection pool with no abnormal traffic volume.

BlackBull enforces a deadline on the HTTP/1.1 header read.  When the client has
not delivered the complete header block (request-line + headers + `CRLFCRLF`)
within `BB_HEADER_TIMEOUT` seconds (default `10`), the server answers
`408 Request Timeout` and closes the socket.

```bash
# Default — already on
python app.py

# Tighten to 3 seconds (recommended behind a reverse proxy that already buffers).
BB_HEADER_TIMEOUT=3 python app.py

# Disable (only safe on a trusted local socket).
BB_HEADER_TIMEOUT=0 python app.py
```

HTTP/2 has no equivalent header timeout because the protocol does not allow
a peer to drip header bytes one at a time — `HEADERS` and `CONTINUATION`
frames carry their length in the frame header, and the `MAX_HEADER_LIST_SIZE`
SETTING bounds the total.

#### Oversized headers — memory exhaustion

A 1 GB `X-Foo: <random bytes>` header would otherwise sit in `readuntil`'s
buffer until the line terminator arrived.  Two limits stop this:

| Limit | Default | Triggered when |
|---|---|---|
| `BB_HEADER_MAX_LINE` | `8192` | A single header line (or the request line) exceeds this. |
| `BB_HEADER_MAX_TOTAL` | `65536` | The full header block exceeds this. |

A request that exceeds either limit gets `431 Request Header Fields Too Large`
and the connection is closed.  The defaults match Apache's
`LimitRequestLine` / `LimitRequestFieldsize` and nginx's
`large_client_header_buffers`.

#### Connection cap and per-request timeout

| Limit | Default | Behaviour |
|---|---|---|
| `BB_MAX_CONNECTIONS` | `500` per worker | Connections beyond the cap are refused at accept time (the kernel handles the SYN; the application never sees it).  Combine with `BB_SOCKET_BACKLOG` for graceful overload. |
| `BB_REQUEST_TIMEOUT` | `0` (off) | Per-HTTP/2-stream deadline in seconds.  Set in production (e.g. `30`) so that an ASGI handler hung on an upstream call can't keep its stream slot indefinitely.  Stream is cancelled via `RST_STREAM CANCEL`. |

#### Production checklist

```bash
BB_WORKERS=0 \
BB_UVLOOP=1 \
BB_HEADER_TIMEOUT=3 \
BB_REQUEST_TIMEOUT=30 \
BB_MAX_CONNECTIONS=1000 \
BB_ACCESS_LOG=0 \
python app.py --cert /etc/ssl/site.pem --key /etc/ssl/site.key
```

`BB_ACCESS_LOG=0` assumes a separate log aggregator is consuming structured logs;
without one, leave it at `1` so the access log keeps going to stderr.

### §10.5  Advanced deployment: Unix sockets, fd inheritance, and TOML config

#### Unix domain socket binding

When BlackBull runs behind a local reverse proxy (nginx, Caddy), using an
`AF_UNIX` socket eliminates TCP overhead and avoids exposing a port on
`0.0.0.0`:

```bash
# CLI
blackbull myapp:app --bind unix:/run/blackbull.sock

# nginx upstream
upstream blackbull { server unix:/run/blackbull.sock; }
```

The socket file is created with mode `0660` so a reverse proxy running
in the same group can connect without a `chmod`.  A leftover socket file
at the path is removed automatically before bind; BlackBull refuses to
unlink a regular file or directory at that path (safety check).

TCP-only socket options (`SO_REUSEPORT`, `TCP_USER_TIMEOUT`,
`IPV6_V6ONLY`) are skipped for `AF_UNIX` sockets — they carry no
meaning on a domain socket.

#### fd inheritance — systemd socket activation

Systemd can pre-bind the port as root and then start BlackBull
unprivileged, handing the bound socket as an open file descriptor:

```ini
# /etc/systemd/system/blackbull.socket
[Socket]
ListenStream = 443

[Install]
WantedBy = sockets.target
```

```ini
# /etc/systemd/system/blackbull.service
[Service]
User            = www-data
ExecStart       = blackbull myapp:app --bind fd://3
```

BlackBull validates the `$LISTEN_PID` / `$LISTEN_FDS` environment
variables according to the `sd_listen_fds(3)` protocol:

* `LISTEN_PID` must equal the current process PID — if it points
  elsewhere BlackBull refuses to adopt the fd (prevents accidentally
  stealing another process's socket).
* `LISTEN_FDS` defines the valid fd window `[3, 3 + LISTEN_FDS)`.
  Fds outside that window are rejected.

When neither variable is set (non-systemd handoff, tests) BlackBull
accepts the fd unconditionally.

Benefits of systemd socket activation:
- Server can bind port 443 without running as root.
- Zero-downtime restarts: systemd keeps the socket open while the old
  service stops and the new one starts.
- Lazy activation: the socket is ready before BlackBull starts; the
  first connection wakes the service.

#### TOML configuration file

The `--config` flag loads a TOML file whose keys mirror the `BB_*`
environment variables.  CLI flags and environment variables always take
precedence over the file (env vars are the source of truth in
containers / PaaS).

```bash
blackbull myapp:app --config blackbull.toml
```

Example `blackbull.toml`:

```toml
[server]
workers             = 4
stream_queue_depth  = 64
ws_queue_depth      = 256
socket_backlog      = 1024
socket_reuseport    = true
keep_alive_timeout  = 60.0
frame_yield_every   = 8

[tls]
cert = "/etc/ssl/myapp.pem"
key  = "/etc/ssl/myapp.key"

[limits]
max_connections  = 1000
request_timeout  = 30.0
header_timeout   = 5.0

[logging]
access_log   = true
async_logging = true
```

**Priority order** (highest first):

1. CLI flags (`--workers`, `--certfile`, …)
2. Environment variables (`BB_WORKERS`, …)
3. `blackbull.toml` (lowest — sets env vars with `setdefault`)

The `[tls] cert` and `[tls] key` keys map to `--certfile` and
`--keyfile` respectively; they apply only when neither CLI flag was
passed.  All other keys set the corresponding `BB_*` environment
variable before `get_settings()` is called.

---

## §11  Complete Example — SimpleTaskManager Skeleton

The following skeleton demonstrates every concept from §1-10 and can be expanded
into a full REST + HTML task management application.

```python
import asyncio
import json
import secrets
from http import HTTPMethod, HTTPStatus

from blackbull import BlackBull, JSONResponse, Response, read_body

app = BlackBull()
SESSIONS: dict[str, str] = {}   # token → username

# ── Per-route middleware (JSON body parsing only) ──────────────────────────────

async def json_body_mw(scope, receive, send, call_next):
    """Parse request body as JSON; inject scope['json']."""
    try:
        scope['json'] = json.loads(await read_body(receive))
    except (json.JSONDecodeError, ValueError):
        await send(JSONResponse({'error': 'Invalid JSON'},
                                status=HTTPStatus.BAD_REQUEST))
        return
    await call_next(scope, receive, send)

# ── Custom exception for auth failures ────────────────────────────────────────

class Unauthorized(Exception):
    pass

# ── Lifespan ──────────────────────────────────────────────────────────────────

@app.on_startup
async def startup():
    print('Server ready. Default user: admin / admin')
    # e.g.: await db.init()

# ── Cross-cutting concerns via event hooks ────────────────────────────────────

@app.on_error(Unauthorized)
async def handle_unauthorized(scope, receive, send):
    await send(JSONResponse({'error': 'Unauthorized'}, status=HTTPStatus.UNAUTHORIZED))


@app.intercept('before_handler')
async def require_auth(event):
    """Validate Bearer token for protected routes; raise Unauthorized to abort."""
    path = event.detail['path']
    if not (path.startswith('/tasks') or path == '/logout'):
        return
    scope = event.detail['scope']
    auth = scope['headers'].get(b'authorization', b'')
    token = auth[7:].decode() if auth.startswith(b'Bearer ') else ''
    user = SESSIONS.get(token)
    if not user:
        raise Unauthorized('Unauthorized')
    scope['user'] = user
    scope['token'] = token


@app.on('request_received')
async def log_request(event):
    print(f"→ {event.detail['method']} {event.detail['path']}")


@app.on('request_completed')
async def log_response(event):
    print(f"  → {event.detail['status']} ({event.detail['duration_ms']:.1f} ms)")

# ── Public routes ─────────────────────────────────────────────────────────────

@app.route(methods=[HTTPMethod.GET], path='/')
async def index(scope, receive, send):
    await send(Response(b'<h1>Login page</h1>'))


@app.route(methods=[HTTPMethod.GET], path='/app')
async def app_page(scope, receive, send):
    await send(Response(b'<h1>Task Manager</h1>'))  # serve index.html


@app.route(methods=[HTTPMethod.POST], path='/register',
           middlewares=[json_body_mw])
async def register(scope, receive, send):
    data = scope['json']
    username = str(data.get('username', '')).strip()
    password = str(data.get('password', ''))
    if not username or not password:
        await send(JSONResponse({'error': 'username and password required'},
                                status=HTTPStatus.BAD_REQUEST))
        return
    # ... create user in DB, return 409 if duplicate ...
    token = secrets.token_urlsafe(32)
    SESSIONS[token] = username
    await send(JSONResponse({'ok': True, 'token': token}))


@app.route(methods=[HTTPMethod.POST], path='/login',
           middlewares=[json_body_mw])
async def login(scope, receive, send):
    data = scope['json']
    username = str(data.get('username', '')).strip()
    password = str(data.get('password', ''))
    # ... verify_user(username, password) ...
    token = secrets.token_urlsafe(32)
    SESSIONS[token] = username
    await send(JSONResponse({'ok': True, 'token': token}))

# ── Protected API routes (guarded by require_auth interceptor above) ──────────

@app.route(methods=[HTTPMethod.GET], path='/tasks')
async def list_tasks(scope, receive, send):
    user = scope['user']
    tasks = []  # replace with: await db.get_tasks(user)
    await send(JSONResponse(tasks))


@app.route(methods=[HTTPMethod.POST], path='/tasks',
           middlewares=[json_body_mw])
async def create_task(scope, receive, send):
    title = str(scope['json'].get('title', '')).strip()
    if not title:
        await send(JSONResponse({'error': 'title required'},
                                status=HTTPStatus.BAD_REQUEST))
        return
    task = {'id': 1, 'title': title, 'completed': False}  # replace with DB call
    await send(JSONResponse(task, status=HTTPStatus.CREATED))


@app.route(methods=[HTTPMethod.PUT], path='/tasks/{task_id}',
           middlewares=[json_body_mw])
async def update_task(scope, receive, send):
    task_id = scope['path_params']['task_id']
    # ... db.update_task(scope['user'], int(task_id), ...) ...
    await send(JSONResponse({'id': task_id, 'title': 'updated'}))


@app.route(methods=[HTTPMethod.DELETE], path='/tasks/{task_id}')
async def delete_task(scope, receive, send):
    task_id = scope['path_params']['task_id']
    # ... db.delete_task(scope['user'], int(task_id)) ...
    await send(JSONResponse({'ok': True}))


@app.route(methods=[HTTPMethod.POST], path='/logout')
async def logout(scope, receive, send):
    SESSIONS.pop(scope.get('token', ''), None)
    await send(JSONResponse({'ok': True}))

# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8000)
    parser.add_argument('--cert', default=None)
    parser.add_argument('--key',  default=None)
    args = parser.parse_args()
    asyncio.run(app.run(port=args.port, certfile=args.cert, keyfile=args.key))
```

The working implementation of this skeleton is
[`examples/SimpleTaskManager/app.py`](https://github.com/TOKUJI/BlackBull/blob/master/examples/SimpleTaskManager/app.py).
The same application written without middleware or event hooks — auth and
JSON parsing inlined into every handler via helper calls — is in
[`examples/SimpleTaskManager/app-simple.py`](https://github.com/TOKUJI/BlackBull/blob/master/examples/SimpleTaskManager/app-simple.py).
Compare them to see why factoring cross-cutting concerns into middleware and
event observers pays off as the route count and feature set grow.

---

## §12  Testing

BlackBull's `app` is a plain ASGI callable, so it can be tested without a running
server.

### 12.1  Unit testing a handler directly

Construct a minimal scope dict and fake `receive`/`send` callables:

```python
# test_handlers.py
import asyncio
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
async def test_ping():
    responses = []

    async def fake_send(body, status=HTTPStatus.OK, headers=[]):
        responses.append({'body': body, 'status': status})

    await app(make_scope(), fake_receive, fake_send)
    assert responses[0]['status'] == HTTPStatus.OK
    assert b'"pong"' in responses[0]['body']
```

### 12.2  Integration testing with `httpx` (recommended)

[`httpx`](https://www.python-httpx.org/) ships an `ASGITransport` adapter that
drives the app over an in-process connection — no open port needed:

```bash
pip install -e '.[testing]'   # installs httpx, pytest-asyncio, typeguard, etc.
```

```python
# test_integration.py
import pytest
import httpx
from myapp import app   # your BlackBull instance

@pytest.mark.asyncio
async def test_register_and_list_tasks():
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url='http://test',
    ) as client:
        # Register
        r = await client.post('/register',
                              json={'username': 'alice', 'password': 'secret'})
        assert r.status_code == 200
        token = r.json()['token']

        # Create a task
        r = await client.post('/tasks',
                              json={'title': 'Buy milk'},
                              headers={'Authorization': f'Bearer {token}'})
        assert r.status_code == 201

        # List tasks
        r = await client.get('/tasks',
                             headers={'Authorization': f'Bearer {token}'})
        assert r.json()[0]['title'] == 'Buy milk'
```

BlackBull's own `pytest.ini` sets `asyncio_mode = strict`, which requires every
async test to carry `@pytest.mark.asyncio` explicitly.  Match this in application
test suites for consistent behaviour:

### 12.3  Testing middleware in isolation

Middleware is just an async function — test it by passing stub callables:

```python
@pytest.mark.asyncio
async def test_auth_mw_rejects_missing_token():
    from myapp import auth_mw
    from blackbull.server.headers import Headers

    scope = {'type': 'http', 'method': 'GET', 'path': '/tasks',
             'headers': Headers([]), 'state': {}}

    responses = []
    async def fake_send(body, status=200, headers=[]):
        responses.append(status)

    call_next_called = False
    async def fake_call_next(scope, receive, send):
        nonlocal call_next_called
        call_next_called = True

    await auth_mw(scope, None, fake_send, fake_call_next)

    assert not call_next_called          # short-circuited
    assert responses[0] == HTTPStatus.UNAUTHORIZED
```

---

## §13  Common Middleware Recipes

Reusable middleware patterns that most applications need.

### 13.1  CORS

BlackBull ships a built-in `CORS` middleware that handles preflight `OPTIONS` requests
and adds the required `Access-Control-*` headers to actual cross-origin responses.

```python
from blackbull import BlackBull, CORS

app = BlackBull()
app.use(CORS(
    allow_origins=['https://myapp.example.com'],
    allow_methods=['GET', 'POST', 'OPTIONS'],
    allow_headers=['Authorization', 'Content-Type'],
    allow_credentials=True,
    max_age=3600,
))
```

**Parameters:**

| Parameter | Type | Default | Notes |
|---|---|---|---|
| `allow_origins` | `list[str] \| str` | `'*'` | Explicit origin strings, or `['*']` for wildcard |
| `allow_methods` | `list[str]` | `['GET','POST','HEAD','OPTIONS']` | Methods allowed in preflight |
| `allow_headers` | `list[str] \| str` | `'*'` | Request headers allowed; `['*']` permits all |
| `allow_credentials` | `bool` | `False` | Emit `Access-Control-Allow-Credentials: true` |
| `expose_headers` | `list[str]` | `[]` | Response headers the browser JS may read |
| `max_age` | `int \| None` | `600` | Preflight cache seconds; `None` omits the header |

`allow_credentials=True` cannot be combined with `allow_origins=['*']` — the CORS spec
forbids it.  List explicit origins instead.

Apply to specific route groups rather than globally when only some routes need CORS:

```python
api = app.group(middlewares=[CORS(allow_origins=['https://myapp.example.com'])])

@api.route(path='/items')
async def list_items(): ...
```

### 13.2  Request ID

Attach a unique ID to every request for distributed tracing:

```python
import uuid

async def request_id_mw(scope, receive, send, call_next):
    req_id = (scope['headers'].get(b'x-request-id', b'')
              or uuid.uuid4().hex.encode())
    scope['request_id'] = req_id.decode() if isinstance(req_id, bytes) else req_id

    _send = send
    async def tagged_send(body, status=200, headers=[]):
        await _send(body, status,
                    list(headers) + [(b'x-request-id', scope['request_id'].encode())])

    await call_next(scope, receive, tagged_send)
```

### 13.3  Rate limiting (token bucket, in-process)

```python
import time
from collections import defaultdict

_buckets: dict[str, tuple[float, int]] = defaultdict(lambda: (time.monotonic(), 0))
RATE_LIMIT = 60   # requests per minute per IP

async def rate_limit_mw(scope, receive, send, call_next):
    ip = (scope.get('client') or ['unknown'])[0]
    now = time.monotonic()
    window_start, count = _buckets[ip]

    if now - window_start > 60:       # new window
        _buckets[ip] = (now, 1)
    elif count >= RATE_LIMIT:
        await send(JSONResponse({'error': 'rate limit exceeded'},
                                status=HTTPStatus.TOO_MANY_REQUESTS))
        return
    else:
        _buckets[ip] = (window_start, count + 1)

    await call_next(scope, receive, send)
```

---

## §14  Logging

BlackBull uses two separate logger hierarchies, each with a distinct purpose.

### 14.1  Access log — `blackbull.access`

For every completed HTTP/1.1 request the server emits one `INFO` record on
the `blackbull.access` logger.  The record format is:

```
{client_ip} "{method} {path} HTTP/{version}" {status} {bytes} {duration}ms
```

Example line:
```
203.0.113.42 "POST /tasks HTTP/1.1" 201 87 3ms
```

By default the logger has no handlers attached, so nothing is printed.
Enable it the same way as any Python logger:

```python
import logging

# To stdout
handler = logging.StreamHandler()
handler.setFormatter(logging.Formatter('%(asctime)s %(message)s'))
logging.getLogger('blackbull.access').addHandler(handler)
logging.getLogger('blackbull.access').setLevel(logging.INFO)
```

To a rotating file:

```python
from logging.handlers import RotatingFileHandler

fh = RotatingFileHandler('access.log', maxBytes=10_000_000, backupCount=5)
fh.setFormatter(logging.Formatter('%(message)s'))
logging.getLogger('blackbull.access').addHandler(fh)
logging.getLogger('blackbull.access').setLevel(logging.INFO)
```

The record is emitted after the response completes — even if the app raises
an unhandled exception (the log line is written in a `finally` block, so
the status field may remain `'-'` when the app never sent a response).

#### Named fields in the LogRecord

Every access log record carries the following named attributes, available in
a custom `logging.Formatter` format string:

| Attribute | Type | Example |
|---|---|---|
| `%(client_ip)s` | str | `203.0.113.42` |
| `%(method)s` | str | `POST` |
| `%(path)s` | str | `/tasks` |
| `%(http_version)s` | str | `1.1` |
| `%(status)s` | int or `'-'` | `201` |
| `%(response_bytes)d` | int | `87` |
| `%(duration_ms).1f` | float | `3.4` |

Custom format example:

```python
fmt = ('%(asctime)s %(client_ip)s "%(method)s %(path)s" '
       '%(status)s %(response_bytes)d %(duration_ms).0fms')
logging.getLogger('blackbull.access').handlers[0].setFormatter(
    logging.Formatter(fmt)
)
```

### 14.2  Extending the access log record from middleware

The `AccessLogRecord` for the current request is stored at
`scope['state']['access_log']`.  Middleware that runs before the handler can
attach extra attributes and read them back in a custom formatter or a
post-processing handler:

```python
import uuid

async def request_id_mw(scope, receive, send, call_next):
    req_id = uuid.uuid4().hex
    scope['request_id'] = req_id
    # Attach to the access log record so it appears in log output
    record = scope['state'].get('access_log')
    if record:
        record.request_id = req_id   # arbitrary extra attribute
    await call_next(scope, receive, send)
```

A custom `logging.Filter` can then surface the attribute:

```python
class AccessLogFilter(logging.Filter):
    def filter(self, record):
        record.request_id = getattr(record, 'request_id', '-')
        return True

handler = logging.StreamHandler()
handler.addFilter(AccessLogFilter())
handler.setFormatter(logging.Formatter(
    '%(message)s req_id=%(request_id)s'
))
logging.getLogger('blackbull.access').addHandler(handler)
```

### 14.3  Debug / framework log — `blackbull`

Internal framework events (frame parsing, HPACK, routing decisions, TLS
handshake) are logged on the `blackbull` logger and its children
(`blackbull.server.server`, `blackbull.protocol.frame`, …) at `DEBUG` level.

Enable for development:

```python
import logging
logging.basicConfig(level=logging.DEBUG)
# or target just the server layer:
logging.getLogger('blackbull.server').setLevel(logging.DEBUG)
```

This is separate from the access log so that production deployments can
enable access logging without flooding logs with internal debug output.

#### `@log` decorator

The `@log` decorator from `blackbull.logger` annotates a function so that
its call arguments are logged at `DEBUG` level using the caller module's
logger:

```python
from blackbull.logger import log

@log
async def my_fn(x, y):
    ...
# logs: my_fn((x_val, y_val), {}) at DEBUG level
```

**Zero-overhead at non-DEBUG level.**  The check runs at decoration time
(import), not on every call.  When the module logger is not enabled for
`DEBUG` at import time, the decorator returns the original function
unwrapped — there is no extra call frame or level-check overhead in
production.

The trade-off: setting the log level to `DEBUG` *after* modules have already
been imported will not activate `@log` logging for already-decorated
functions.  Configure `DEBUG` level before importing framework modules, or
restart the process.

### 14.4  Forwarding access logs to a remote server

`logging.Handler.emit()` is synchronous.  Calling a blocking HTTP request
directly from `emit` would stall the asyncio event loop.  The solution is
the standard library's `QueueHandler` + `QueueListener` pair: the handler
enqueues records in O(1) without blocking, and a background thread drains
the queue and calls the real (blocking) HTTP handler.

A complete two-process example is provided in
[`examples/LoggingExample/`](https://github.com/TOKUJI/BlackBull/tree/master/examples/LoggingExample/):

| File | Role |
|---|---|
| `web_server.py` | BlackBull hello-world with `JsonHTTPHandler` + `QueueListener` |
| `log_server.py` | `http.server` that receives JSON records and inserts them into SQLite |

**Start order:**

```bash
# Terminal 1
python examples/LoggingExample/log_server.py   # listens on :9000

# Terminal 2
python examples/LoggingExample/web_server.py   # listens on :8000

# Make some requests
curl http://localhost:8000/
curl http://localhost:8000/tasks

# Inspect the database
sqlite3 examples/LoggingExample/logs.db \
    "SELECT client_ip, method, path, status, duration_ms FROM access_logs;"
```

**Key classes:**

`JsonHTTPHandler` (in `web_server.py`) is a `logging.Handler` subclass that
serialises a `LogRecord` — including the named access-log fields added via
`extra=` — as a JSON POST body:

```python
class JsonHTTPHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        payload = json.dumps({
            'created':        record.created,
            'level':          record.levelname,
            'message':        self.format(record),
            'client_ip':      getattr(record, 'client_ip', '-'),
            'method':         getattr(record, 'method',    '-'),
            'path':           getattr(record, 'path',      '-'),
            'status':         getattr(record, 'status',    '-'),
            'response_bytes': getattr(record, 'response_bytes', 0),
            'duration_ms':    getattr(record, 'duration_ms',   0.0),
        }).encode()
        conn = http.client.HTTPConnection(self.host)
        conn.request('POST', self.url, payload,
                     {'Content-Type': 'application/json'})
        conn.getresponse()
        conn.close()
```

The non-blocking wiring:

```python
import queue
from logging.handlers import QueueHandler, QueueListener

_log_queue    = queue.Queue(-1)          # unbounded
_json_handler = JsonHTTPHandler('localhost:9000')
_listener     = QueueListener(_log_queue, _json_handler,
                               respect_handler_level=True)

_access_logger = logging.getLogger('example.access')
_access_logger.addHandler(QueueHandler(_log_queue))
_access_logger.setLevel(logging.INFO)


@app.on_startup
async def start_log_listener():
    _listener.start()


@app.on_shutdown
async def stop_log_listener():
    _listener.stop()


@app.on('request_received')
async def log_request(event):
    _access_logger.info('%s %s', event.detail['method'], event.detail['path'],
                        extra={'client_ip': event.detail['client_ip'] or '-',
                               'method': event.detail['method'],
                               'path': event.detail['path'],
                               'status': '-', 'response_bytes': 0, 'duration_ms': 0.0})


@app.on('request_completed')
async def log_response(event):
    d = event.detail
    _access_logger.info('%s %s → %s (%.1f ms)',
                        d['method'], d['path'], d['status'], d['duration_ms'],
                        extra={'client_ip': d['client_ip'] or '-',
                               'method': d['method'], 'path': d['path'],
                               'status': d['status'],
                               'response_bytes': d['response_bytes'],
                               'duration_ms': d['duration_ms']})
```

`QueueHandler.emit()` puts the record in the queue and returns immediately.
`QueueListener` runs in a daemon thread and calls `JsonHTTPHandler.emit()`
there — the blocking HTTP call never touches the event-loop thread.

`@app.on_startup` / `@app.on_shutdown` tie the listener lifecycle to the server,
so the background thread starts only when the server is ready and is flushed and
joined cleanly before the process exits.

### 14.6  What is not yet implemented

- HTTP/2 access logging: per-stream entry using the same `AccessLogRecord`
  and `_make_capturing_send` helper in `HTTP2Handler`.
- WebSocket access logging: connection-level entry (client IP, path, close
  code, duration).

---

## §15  HTTP/2 Protocol Internals

### 15.1  Stream state machine (RFC 7540 §5.1)

Every HTTP/2 request lives on a numbered stream.  BlackBull tracks each
stream's lifecycle with a minimal state machine:

```
IDLE ──HEADERS──► OPEN ──DATA+END_STREAM──► CLOSED
                 │
                 └─HEADERS+END_STREAM──► HALF_CLOSED_REMOTE ──DATA+END_STREAM──► CLOSED
```

| State | Meaning |
|---|---|
| `IDLE` | Stream allocated but no frames seen yet |
| `OPEN` | HEADERS received, body may follow |
| `HALF_CLOSED_REMOTE` | HEADERS+END_STREAM received; client will send no more frames on this stream |
| `CLOSED` | Full request received |

State transitions happen in `server.py`'s `HTTP2Handler.run()` loop:

```python
# HEADERS frame
stream.on_headers_received(end_stream=bool(frame.end_stream))

# DATA frame — reject if stream is already done
if stream.state in (StreamState.HALF_CLOSED_REMOTE, StreamState.CLOSED):
    await self.send_frame(
        self.factory.rst_stream(stream.identifier, ErrorCodes.STREAM_CLOSED))
else:
    stream.on_data_received(end_stream=bool(frame.end_stream))
    ...
```

### 15.2  Priority: RFC 7540 vs RFC 9218

**RFC 7540 §5.3 (legacy PRIORITY frames)** defined a dependency tree with
weights 1–256.  Very few clients ever sent these frames, and the overhead
was considered not worth it — RFC 9218 supersedes the mechanism.

**RFC 9218 (Extensible Prioritization Scheme)** replaces the dependency tree
with two simpler fields sent in a `PRIORITY_UPDATE` frame (type `0x10`):

| Field | Values | Meaning |
|---|---|---|
| `urgency` | 0 (highest) – 7 (lowest) | relative importance |
| `incremental` | `?0` / `?1` | whether the server may interleave responses |

BlackBull's stance: **receive but do not schedule**.  Priority signals are
accepted, logged at DEBUG, and discarded.  This is valid per RFC 9218 §2:
"A server that does not implement prioritization MUST ignore this frame."

Implementation details:

- `FrameTypes.PRIORITY_UPDATE = b'\x10'` — frame type registered in the
  `FrameTypes` enum so the parser does not raise `ValueError`.
- `PriorityUpdate` frame class — parses `prioritized_stream_id` (4 bytes)
  and `priority_field` (ASCII string, e.g. `"u=3, i"`).
- `PriorityUpdateResponder` — no-op responder that logs the hint at DEBUG
  level via `response.py`'s `ResponderFactory`.

**Weight off-by-one (RFC 7540 §6.3)**: the wire format stores `weight − 1`
(range 0–255 → logical weight 1–256).  BlackBull adds 1 on parse so
`stream.weight` always holds the logical value.

### 15.3  Flow control

HTTP/2 has two independent flow-control windows:

- **Connection-level**: shared across all streams on one connection.
- **Stream-level**: per-stream budget.

`HTTP2Sender._write()` blocks on `asyncio.Event` when the window is
exhausted.  `WindowUpdateResponder.respond()` credits the relevant sender(s)
and sets the event to unblock waiting writes.  No user code needs to interact
with flow control directly.

### 15.4  Reading priority hints in handlers

Every HTTP/2 request scope contains an `http2_priority` key populated by
BlackBull before your handler is called:

```python
scope['http2_priority']  # → {'urgency': int, 'incremental': bool}
```

| Key | Type | Default | Meaning |
|---|---|---|---|
| `urgency` | `int` 0–7 | `3` | 0 = most urgent, 7 = least urgent |
| `incremental` | `bool` | `False` | Client accepts interleaved partial responses |

BlackBull resolves the value in this order (first wins):

1. `PRIORITY_UPDATE` frame (type `0x10`) received from the client — either
   before or after the HEADERS frame.
2. `priority` HTTP header in the request (e.g. `priority: u=1, i`).
3. RFC 9218 §4.1 defaults: `urgency=3`, `incremental=False`.

For HTTP/1.1 requests the key is absent.  Always use `.get()` or supply
a default so your code works across both protocols.

#### Minimal handler example

```python
_DEFAULT_PRIORITY = {'urgency': 3, 'incremental': False}

@app.route(path='/search')
async def search(scope, receive, send):
    hint = scope.get('http2_priority', _DEFAULT_PRIORITY)
    if hint['urgency'] <= 2:
        # High-urgency: return cached / pre-computed result immediately.
        result = get_cached_result()
    else:
        # Normal / background: run the full search.
        result = await run_full_search()
    await send(JSONResponse(result))
```

#### Interceptor example

The pattern scales cleanly to a `before_handler` interceptor so every route
benefits without repeating the check:

```python
@app.intercept('before_handler')
async def handle_priority(event):
    scope = event.detail['scope']
    hint = scope.get('http2_priority', {'urgency': 3, 'incremental': False})
    urgency = hint['urgency']
    scope['_priority_urgency'] = urgency   # pass downstream to handler

    if urgency <= 1:
        logger.info('HIGH-PRIORITY u=%d: %s %s',
                    urgency, event.detail['method'], event.detail['path'])
```

See `examples/SimpleTaskManager/app.py` for a complete integration and
`examples/PriorityExample/` for a dedicated server + client pair that
demonstrates all three endpoints and the `priority` header fallback.

#### Testing the feature with curl

```bash
# Default priority (urgency=3)
curl --http2 -k https://localhost:8443/priority-echo

# High urgency
curl --http2 -k -H 'priority: u=1' https://localhost:8443/priority-echo

# Background prefetch, incremental
curl --http2 -k -H 'priority: u=6, i' https://localhost:8443/priority-echo
```

> **Note** — curl converts the `priority` header to a `PRIORITY_UPDATE`
> frame when talking HTTP/2 over TLS.  httpx (Python) sends it as a plain
> header; BlackBull parses both forms.

### 15.5  HTTP/2 server push

Server push lets the server proactively send a resource to the client before
the client asks for it.  A typical use case is pushing a CSS file alongside
the HTML page that references it, saving one round-trip.

#### ASGI event

The app signals a push by calling `send` with an `http.response.push` event
before the final `http.response.body`:

```python
@app.route(path='/')
async def index(scope, receive, send):
    # Push a stylesheet before sending the HTML.
    await send({
        'type': 'http.response.push',
        'path': '/static/style.css',
        'headers': [],               # regular headers only; pseudo-headers are added automatically
    })
    await send({'type': 'http.response.start', 'status': 200,
                'headers': [(b'content-type', b'text/html')]})
    await send({'type': 'http.response.body',
                'body': b'<html>...</html>'})
```

`path` must be a plain string (percent-encoding decoded).  Pseudo-headers
(`:method`, `:scheme`, `:authority`) are filled in automatically by the
server; do not include them in `headers`.

#### What BlackBull does

1. Allocates the next even stream ID for the pushed resource (RFC 7540 §5.1.1
   — server-initiated streams are always even: 2, 4, 6, …).
2. Sends a `PUSH_PROMISE` frame on the **parent stream** (the one that
   triggered the push).  The frame contains the synthetic request headers
   (`GET /static/style.css`) the client can use to match its cache.
3. Creates a synthetic scope (`type='http'`, `method='GET'`,
   `path='/static/style.css'`) and dispatches it to your app as a new
   `asyncio.Task` on the promised stream.  Your app handles the pushed
   request exactly like a normal GET — the same route, same middleware,
   same response cycle.

#### Checking for support

The server advertises push support in `scope['extensions']`:

```python
if 'http.response.push' in scope.get('extensions', {}):
    await send({'type': 'http.response.push', 'path': '/logo.png', 'headers': []})
```

For HTTP/1.1 requests `scope['extensions']` does not contain
`'http.response.push'`, so the guard above makes the same handler work on
both protocols.

#### Limitations

- Clients can disable server push by sending `SETTINGS_ENABLE_PUSH=0`.
  BlackBull does not currently check this setting; if the client rejects the
  push it will send a `RST_STREAM` which BlackBull logs and ignores.
- Pushed resources should be cacheable.  Pushing non-cacheable content wastes
  bandwidth and may confuse browsers.

---

## §16  Global Middleware and Static File Serving

### §16.1  Global middleware — `app.use()`

Global middlewares run before routing for every non-lifespan request.

```python
app.use(my_middleware)
```

The middleware signature is the same as route middleware:

```python
async def my_middleware(scope, receive, send, call_next):
    # pre-processing
    await call_next(scope, receive, send)
    # post-processing
```

Multiple `use()` calls build a chain: the first registered middleware is the
outermost (runs first).  A middleware that does not call `call_next`
short-circuits the chain — no route handler is invoked.

Lifespan events (`scope['type'] == 'lifespan'`) bypass the global middleware
chain entirely.

### §16.2  Static file serving — `app.static()`

`app.static(url_prefix, root_dir)` registers a `StaticFiles` global
middleware that serves files from `root_dir` for paths that start with
`url_prefix`.

```python
from blackbull import BlackBull

app = BlackBull()
app.static('/assets', 'public/assets')
app.static('/images', 'public/images')

@app.route(path='/')
async def index(scope, receive, send):
    ...
```

A request to `/assets/style.css` is intercepted by the global middleware
before routing and served from `public/assets/style.css`.  Requests that do
not match the prefix fall through to the route handlers normally.

#### Standalone usage

`StaticFiles` can also be used as a standalone ASGI app (useful in tests or
when mounting without BlackBull):

```python
from blackbull.middleware.static import StaticFiles

app = StaticFiles(directory='public')
# app(scope, receive, send)  — 3-argument ASGI
```

#### Environment gate — `BLACKBULL_ENV`

Set the `BLACKBULL_ENV` environment variable to control serving behaviour:

| Value | Effect |
|---|---|
| `production` | Always return 404; static files are never served |
| `development` (default) | Serve files normally |
| `test` | Serve files normally |

```bash
BLACKBULL_ENV=production python app.py   # static routes return 404
BLACKBULL_ENV=development python app.py  # static files served
```

#### Range requests — RFC 7233

`StaticFiles` supports `Range` requests out of the box:

```
GET /assets/video.mp4 HTTP/1.1
Range: bytes=0-1023
```

Response:

```
HTTP/1.1 206 Partial Content
Content-Range: bytes 0-1023/4096000
Content-Length: 1024
```

Unsatisfiable ranges return `416 Range Not Satisfiable`.

#### Security

- **Path traversal**: URL-encoded paths are decoded with `urllib.parse.unquote`
  before resolution.  Any resolved path that escapes the configured root
  directory returns `400 Bad Request`.
- **Directory listing**: Requests for bare directories return `404`; no
  directory listing is ever served.

#### Inspecting registered roots

```python
app.static('/a', 'public/a')
app.static('/b', 'public/b')
print(app._static_roots)
# [('/a', PosixPath('/abs/public/a')), ('/b', PosixPath('/abs/public/b'))]
```

---

## §17  Streaming Responses

### `StreamingResponse`

Use `StreamingResponse` to push an async generator to the client without
buffering the whole body in memory.  The class calls `send` directly, so it
works as a nested ASGI app:

```python
import asyncio
from blackbull import BlackBull, StreamingResponse

app = BlackBull()

async def countdown():
    for i in range(5, 0, -1):
        yield f'{i}\n'.encode()
        await asyncio.sleep(1)
    yield b'done\n'

@app.route(path='/stream')
async def handler(scope, receive, send):
    await StreamingResponse(countdown())(scope, receive, send)
```

`StreamingResponse.__init__` accepts:

| parameter | default | description |
|---|---|---|
| `content` | — | `AsyncIterator` of `bytes` or `str` chunks |
| `status` | `200` | HTTP status code |
| `headers` | `[]` | extra header tuples `(bytes, bytes)` |
| `media_type` | `'text/plain'` | `Content-Type` value (injected if absent from `headers`) |

`str` chunks are encoded to UTF-8 automatically.

### How HTTP/1.1 delivers streaming responses

When `more_body=True` appears on the first `http.response.body` event,
`HTTP1Sender` adds `Transfer-Encoding: chunked` to the response headers and
formats each body event as a hex-length chunk:

```
5\r\n
hello\r\n
5\r\n
world\r\n
0\r\n
\r\n
```

The terminal `0\r\n\r\n` is written automatically when `more_body=False` arrives
(unless `trailers=True` was set in `http.response.start`, in which case the
`http.response.trailers` handler writes it).

HTTP/2 is unaffected — DATA frames carry explicit length and `END_STREAM` maps
to `more_body=False`.

### Writing streaming-safe middleware

Any middleware that wraps the `send` callable and collects body parts will
silently buffer a streaming response, defeating `more_body=True`.

For function-based middleware, the safest approach is to pass body events
through immediately rather than accumulating them:

```python
async def prefix_mw(scope, receive, send, call_next):
    captured_start = None

    async def capturing_send(event):
        nonlocal captured_start
        if event.get('type') == 'http.response.start':
            captured_start = event
        elif event.get('type') == 'http.response.body':
            if event.get('more_body'):
                # Streaming response — pass through without buffering
                await send(captured_start)
                captured_start = None
                await send(event)
            else:
                # Non-streaming — transform the body
                body = b'[prefix] ' + event.get('body', b'')
                await send(captured_start)
                await send({**event, 'body': body})
        else:
            await send(event)

    await call_next(scope, receive, capturing_send)
```

For most use cases (header injection, logging) the middleware does not touch
the body at all and streaming safety is not a concern.

---

## §18  Environment Variables

Every runtime knob is read from an environment variable on startup.  Defaults
are conservative for development; raise the limits and tune the sockets in
production.  Source of truth: [`blackbull/env.py`](https://github.com/TOKUJI/BlackBull/blob/master/blackbull/env.py).

### 18.1  Runtime and processes

| Variable | Default | Controls |
|---|---|---|
| `BLACKBULL_ENV` | `development` | `production` \| `development` \| `test`.  In `production`, `StaticFiles` declines to serve files (production should sit behind nginx/Caddy for static assets). |
| `BB_WORKERS` | `1` | Pre-fork worker count.  `0` resolves to `os.cpu_count()`.  Each worker runs its own asyncio event loop; combine with `BB_SOCKET_REUSEPORT=1` so the kernel load-balances accepts across workers. |
| `BB_UVLOOP` | `0` | Install `uvloop`'s asyncio policy at startup.  Requires `pip install "blackbull[speed]"`; falls back to the standard loop with a warning when uvloop is missing.  Typical win is 1.5–2× throughput on HTTP/2 hot paths. |

### 18.2  Connection limits and timeouts

| Variable | Default | Controls |
|---|---|---|
| `BB_MAX_CONNECTIONS` | `500` | Maximum simultaneous TCP connections **per worker**.  New connections beyond the cap are rejected before TLS / handshake to keep memory and FD use bounded. |
| `BB_REQUEST_TIMEOUT` | `0` (off) | Per-HTTP/2-stream deadline in seconds.  When the deadline elapses the stream is forcibly cancelled with `RST_STREAM CANCEL`.  Use a positive value (e.g. `30`) in production to evict stalled handlers from stream slots. |
| `BB_HEADER_TIMEOUT` | `10.0` | Seconds an HTTP/1.1 client has to deliver the complete header block (request-line + headers + `CRLFCRLF`).  Primary slowloris defence — without it an attacker can hold a connection open indefinitely by dripping bytes.  Server answers `408 Request Timeout` and closes.  `0` disables. |
| `BB_HEADER_MAX_LINE` | `8192` | Maximum bytes in a single HTTP/1.1 request-line or header line.  Matches Apache `LimitRequestLine` / nginx `large_client_header_buffers`.  Exceeded → `431 Request Header Fields Too Large`. |
| `BB_HEADER_MAX_TOTAL` | `65536` | Maximum total bytes in the entire HTTP/1.1 header block.  Exceeded → `431`. |
| `BB_STREAM_QUEUE_DEPTH` | `64` | `asyncio.Queue` depth for HTTP/2 per-stream request-body events.  Caps memory growth when an ASGI handler is slower than the client uploading data. |
| `BB_WS_QUEUE_DEPTH` | `256` | `asyncio.Queue` depth for inbound WebSocket events per connection. |

### 18.3  Socket tuning

| Variable | Default | Controls |
|---|---|---|
| `BB_SOCKET_BACKLOG` | `1024` | `listen()` backlog depth.  Reduces silent connection drops during burst traffic.  Linux caps the effective value at `net.core.somaxconn`. |
| `BB_SOCKET_REUSEPORT` | `1` | When supported by the OS (Linux, modern BSDs), bind each worker to its own listening socket so the kernel hashes incoming connections across workers — eliminates the thundering-herd accept pattern.  No effect with one worker. |
| `BB_SOCKET_SNDBUF` | `262144` | `SO_SNDBUF` (bytes) on each accepted socket.  Linux doubles the requested value internally.  Larger helps throughput for responses ≥ 64 kB.  `0` keeps the kernel default. |
| `BB_SOCKET_RCVBUF` | `262144` | `SO_RCVBUF` (bytes) on each accepted socket.  Same doubling rule.  `0` keeps the kernel default. |

### 18.4  Logging

| Variable | Default | Controls |
|---|---|---|
| `BB_ACCESS_LOG` | `1` | Emit one record on the `blackbull.access` logger per completed request.  Set to `0` in production when a separate log aggregator already consumes structured logs and the per-request formatting cost is undesirable. |
| `BB_ASYNC_LOGGING` | `1` | Install a `QueueHandler` on the `blackbull` logger so `logger.debug/info` calls from the event loop are non-blocking.  See §14.3 for the rationale. |

### 18.5  HTTP/2 internals

| Variable | Default | Controls |
|---|---|---|
| `BB_H2_INITIAL_WINDOW_SIZE` | `1048576` (1 MiB) | Per-stream flow-control window advertised in the server's initial `SETTINGS` frame.  Larger lets peers send more data per stream before waiting for `WINDOW_UPDATE`. |
| `BB_H2_CONNECTION_WINDOW_SIZE` | `4194304` (4 MiB) | Connection-level flow-control window advertised via an initial `WINDOW_UPDATE` on stream 0.  Must be ≥ 65535 (the RFC default); smaller values are silently ignored. |
| `BB_H2_MAX_CONCURRENT_STREAMS` | `100` | `SETTINGS_MAX_CONCURRENT_STREAMS` (RFC 9113 §6.5.2 id `0x3`).  Streams beyond the cap receive `RST_STREAM REFUSED_STREAM` and are not dispatched. |
| `BB_H2_ACTIVE_STREAMS` | `20` | Per-connection `asyncio.Semaphore` cap on stream handlers actually running concurrently, under multi-worker.  Prevents one high-mux connection from saturating a single event loop.  `0` disables (no cap beyond `BB_H2_MAX_CONCURRENT_STREAMS`). |
| `BB_H2_ACTIVE_STREAMS_1W` | `20` | Same as above, but used when `BB_WORKERS=1`. |
| `BB_FRAME_YIELD_EVERY` | `8` | Number of stream tasks spawned per connection before the frame loop inserts `await asyncio.sleep(0)`.  Caps the maximum synchronous run between yields under burst traffic; reduces p99 tail latency.  `0` disables the cooperative yield (legacy behaviour). |

### 18.6  WebSocket

| Variable | Default | Controls |
|---|---|---|
| `BB_WS_PERMESSAGE_DEFLATE` | `1` | Negotiate `permessage-deflate` (RFC 7692) on the inbound handshake when the peer offers it.  Matches modern browsers and major client libraries (Node `ws`, Python `websockets`, aiohttp). |
| `BB_H2_ENABLE_WEBSOCKET` | `0` | Advertise `SETTINGS_ENABLE_CONNECT_PROTOCOL=1` (RFC 8441 §3) so peers may bootstrap WebSocket over HTTP/2 via Extended CONNECT.  Off by default — this path has fewer conformance tests than the HTTP/1.1 Upgrade path and few clients use it (Cloudflare's edge stack is the main consumer). |

### 18.7  Compression

| Variable | Default | Controls |
|---|---|---|
| `BB_COMPRESSION_MIN_SIZE` | `100` | Minimum body size in bytes below which the `Compression` middleware skips compression entirely. |
| `BB_COMPRESSION_EXECUTOR_THRESHOLD` | `65536` (64 KiB) | Body size above which compression is offloaded to a thread-pool executor so the event loop stays responsive during the (CPU-bound) compress call.  `0` always compresses on the event loop. |

### 18.8  Sessions

| Variable | Default | Controls |
|---|---|---|
| `BB_SESSION_SECRET` | *(unset)* | HMAC secret used by the `Session` middleware (§4.4) to sign cookies.  Either pass `secret=` to the constructor or set this env var; if neither is set, construction raises (no insecure default).  Generate one with `python -c "import secrets; print(secrets.token_urlsafe(32))"`. |

---

## §19  Conformance

BlackBull is exercised against three published RFC conformance suites in
addition to the in-tree pytest tests under `tests/conformance/`.  None of the
external suites runs in CI yet — they are local harnesses kept under
`bench/conformance/`.  Re-run them after any protocol-level change and inspect
the report before claiming RFC compliance.

### 19.1  HTTP/1.1 — `tests/conformance/http1/`

In-tree pytest suite, runs as part of `pytest`.  Covers RFC 9110 (HTTP
Semantics) and RFC 9112 (HTTP/1.1 message framing), including:

- request-line syntax, header folding, chunked transfer encoding;
- HEAD / GET disagreement (RFC 9110 §9.3);
- 1xx / 204 / 304 body suppression (RFC 9110 §15);
- pipelining with-and-without bodies;
- the `408` / `431` paths from the abuse defences in §10.4.

```bash
pytest tests/conformance/http1/ -q
```

### 19.2  HTTP/2 — `h2spec` (RFC 9113 + RFC 7541)

[h2spec](https://github.com/summerwind/h2spec) is the de-facto external
conformance suite for HTTP/2 and HPACK; ~146 numbered cases covering frame
format, stream state, flow control, error codes, and header-block decoding.

**Install** (one-time):

```bash
curl -L -sS https://github.com/summerwind/h2spec/releases/download/v2.6.0/h2spec_linux_amd64.tar.gz \
    | tar -xz -C ~/.local/bin h2spec
chmod +x ~/.local/bin/h2spec
```

**Run** against a locally-running TLS server on `:8443`:

```bash
# Start any BlackBull HTTPS server, then:
bash bench/conformance/h2spec_run.sh                 # full suite (~2-5 min)
bash bench/conformance/h2spec_run.sh hpack          # HPACK section only
bash bench/conformance/h2spec_run.sh http2/6.5      # specific section
```

Output is teed to `bench/conformance/results/h2spec_<timestamp>.{txt,xml}`
(both gitignored).  The XML is JUnit-format and machine-readable; the TXT
ends with a `N tests, P passed, S skipped, F failed` line you can grep for
the headline number.

In-tree pytest tests under `tests/conformance/http2/` cover BlackBull-specific
behaviour h2spec does not exercise (RFC 8441 Extended CONNECT, CONTINUATION
boundary cases, server-response shapes), and run in normal `pytest` runs.

### 19.3  WebSocket — Autobahn|Testsuite (RFC 6455 + RFC 7692)

[Autobahn|Testsuite](https://github.com/crossbario/autobahn-testsuite) is the
de-facto external conformance suite for WebSocket; ~500 numbered cases over
framing, control frames, UTF-8 validation, close codes, fragmentation, and
permessage-deflate.

The harness drives the suite from a Docker image against a plaintext
WebSocket echo server.  Docker is required.

**Run:**

```bash
# Terminal 1 — start the echo server BlackBull provides for the test
python bench/conformance/autobahn_app.py --port 9001

# Terminal 2 — run Autobahn against it
bash bench/conformance/autobahn_run.sh               # full fuzzingclient run
CASES='1.*' bash bench/conformance/autobahn_run.sh   # subset (e.g. all of §1.x)
```

Reports land in `bench/conformance/results/autobahn_<timestamp>/` with an
HTML index — open `index.html` in a browser for the case-by-case breakdown.

### 19.4  Filing a non-conformance

If a conformance run regresses (a case that previously passed starts failing),
re-run the latest harness, attach the failing case's verbatim transcript to
the report, and file under the appropriate RFC bucket in `README.md`'s Todo
section:

- HTTP/2 regressions → P1 / P2 with the §x.y.z citation from RFC 9113.
- HTTP/1.1 regressions → P1 with the §x.y citation from RFC 9112 / 9110.
- WebSocket regressions → P2 with the case ID from the Autobahn report.

RFC 8441 (WebSocket over HTTP/2) does not yet have a dedicated external
harness; the in-tree pytest tests under `tests/conformance/http2/test_rfc8441.py`
are the current source of truth.  Bringing up a real h2spec-style harness for
it is tracked in `README.md` Todo P2.

---

## §20  OpenAPI and Swagger UI

A single call publishes a machine-readable spec and an interactive UI:

```python
from blackbull import BlackBull

app = BlackBull()

@app.route(path='/items/{item_id:int}')
async def get_item(item_id: int):
    """Get one item.

    Returns the item with the given numeric id.
    """
    return {'id': item_id}

app.enable_openapi(title='Items API', version='1.0.0')
```

Browse to:

* `http://localhost:8000/openapi.json` — OpenAPI 3.1 JSON
* `http://localhost:8000/docs` — Swagger UI (loads from CDN)

Both routes are GET-only and self-documenting: the spec describes every
HTTP route the app exposes but *excludes itself* and the docs page.

### 20.1  What ends up in the spec

| Source | Mapped to |
|---|---|
| Route template (`/items/{item_id:int}`) | OpenAPI path (`/items/{item_id}`) with a `parameters` entry |
| Path-param converter (`int` / `str` / `uuid` / `path`) | Path-parameter `schema` (see §3.5) |
| HTTP methods on the route | Operation objects under the path |
| Handler docstring — first line | Operation `summary` |
| Handler docstring — rest | Operation `description` |
| HTTP-only (no `scheme=Scheme.websocket`) | Included.  WebSocket routes are skipped. |
| `POST` / `PUT` / `PATCH` route | A placeholder `requestBody: {type: object}` — see §20.3. |

### 20.2  `app.enable_openapi(...)` parameters

| Parameter | Default | Notes |
|---|---|---|
| `title` | `'BlackBull API'` | Spec `info.title` and the Swagger UI page title. |
| `version` | `'0.1.0'` | Spec `info.version`. |
| `description` | `None` | Spec `info.description` (Markdown — Swagger UI renders it). |
| `spec_path` | `'/openapi.json'` | Route that returns the JSON.  Regenerated on every request. |
| `docs_path` | `'/docs'` | Route that returns the Swagger UI host page.  Pass `None` to skip the UI and serve only the JSON. |

Call `enable_openapi()` once, **after** the rest of your routes are
registered.  Routes added later will not appear in the spec returned by
already-issued requests (the spec is rebuilt on each request, but new
registrations after the validator has frozen the router will raise).

### 20.3  Schemas from dataclasses

Annotate a handler parameter with a Python `dataclass` and the spec gains a
real request-body schema; annotate the return type and the spec gains a
real response schema.  No extra dependency — `dataclasses` is in the
standard library.

```python
from dataclasses import dataclass, field

@dataclass
class CreateTask:
    title: str
    completed: bool = False
    tags: list[str] = field(default_factory=list)

@dataclass
class Task:
    id: int
    title: str
    completed: bool

@app.route(methods=HTTPMethod.POST, path='/tasks')
async def create_task(body: CreateTask) -> Task:
    ...
```

Produces, in the spec:

```json
"/tasks": {
  "post": {
    "requestBody": {
      "content": {"application/json": {"schema": {
        "type": "object",
        "title": "CreateTask",
        "properties": {
          "title":     {"type": "string"},
          "completed": {"type": "boolean", "default": false},
          "tags":      {"type": "array", "items": {"type": "string"}, "default": []}
        },
        "required": ["title"]
      }}}
    },
    "responses": {
      "200": {
        "description": "OK",
        "content": {"application/json": {"schema": {
          "type": "object", "title": "Task",
          "properties": {
            "id":        {"type": "integer"},
            "title":     {"type": "string"},
            "completed": {"type": "boolean"}
          },
          "required": ["id", "title", "completed"]
        }}}
      }
    }
  }
}
```

Supported in field annotations and return types:

| Python annotation | OpenAPI schema |
|---|---|
| `str` / `int` / `float` / `bool` / `bytes` | `{"type": "string"}` etc. |
| `T \| None`, `Optional[T]` | `{"anyOf": [<T>, {"type": "null"}]}` |
| `T \| U` (PEP 604 union) | `{"anyOf": [<T>, <U>]}` |
| `list[T]`, `tuple[T, ...]` | `{"type": "array", "items": <T>}` |
| `dict[str, V]` | `{"type": "object", "additionalProperties": <V>}` |
| Nested `@dataclass` | recursive object schema |
| Field default | `default:` attached when JSON-serializable |
| Field without default | added to `required:` |

Anything not in this list (TypedDict, NamedTuple, generic dataclasses with
TypeVars, Pydantic models, etc.) falls through to `{}` — OpenAPI 3.1 treats
that as "no constraint", which is the right default rather than a wrong
guess.

### 20.4  Body deserialization

When a simplified handler's parameter is annotated with a dataclass, the
router reads the request body, parses it as JSON, and constructs an
instance for you.  No `read_body` / `json.loads` boilerplate in the
handler:

```python
@dataclass
class CreateTask:
    title: str
    completed: bool = False
    tags: list[str] = field(default_factory=list)

@app.route(methods=HTTPMethod.POST, path='/tasks')
async def create_task(body: CreateTask) -> Task:
    new = Task(id=next_id(), title=body.title, completed=body.completed)
    ...
    return new
```

The annotation drives detection — the parameter name does not have to be
`body`.  ``async def create_task(item: CreateTask): ...`` works the same
way.  A handler may have at most one body parameter (a literal
``body: bytes`` parameter and a dataclass-typed parameter both consume the
request body); the router rejects two-body signatures at registration.

Coercion rules mirror the schema synthesis in §20.3:

| JSON shape | Constructs |
|---|---|
| `{"field": ...}` matching a `@dataclass` | the dataclass with that field populated |
| nested `{...}` inside a field typed as another `@dataclass` | recursive construction |
| array inside a field typed `list[T]` / `tuple[T, ...]` | each element coerced to ``T`` |
| `null` inside a field typed `T \| None` | ``None`` |
| primitive into `T \| U` | first union branch that constructs cleanly (dataclass branches tried first) |

Unknown JSON keys raise ``TypeError`` rather than being silently dropped —
a client typo like ``{"titel": ...}`` should surface, not vanish.

Handlers may also **return** a dataclass (or a list of dataclasses).  The
adapter serializes it via ``dataclasses.asdict`` recursively, so the same
``Task`` model works on both ends of the wire.

**Errors** propagate to the framework's error router:

* Malformed JSON → ``json.JSONDecodeError``
* Missing required field / unknown field / type mismatch → ``TypeError``

Register handlers via ``@app.on_error(json.JSONDecodeError)`` and
``@app.on_error(TypeError)`` to convert them to 400 / 422 responses if
the default 500 isn't what you want.

> **No external model library required.**  This works with the standard
> library's `dataclasses`.  Pydantic, attrs, msgspec, etc. are not
> supported in v3 — if you want one of those, parse the body yourself
> from a ``body: bytes`` parameter.

### 20.5  What is still on the future list

* **Security schemes.**  Auth is application-defined here, so no global
  `securitySchemes` are emitted.  Add them post-hoc by editing the spec
  returned by `generate_spec()` and serving the result yourself.
* **Tags / grouping.**  Operations are flat under their path; tag-based
  grouping in Swagger UI requires manual annotation today.
* **Status-code variants.**  Every operation emits a single `200: OK`
  response.  Distinguishing `201 Created` for POST, `204 No Content` for
  DELETE, etc. needs more cues than the return annotation alone.
