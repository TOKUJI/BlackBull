# BlackBull Developer Guide

BlackBull is a Python ASGI 3.0 web framework supporting HTTP/1.1, HTTP/2, and WebSocket.

---

## §0  Prerequisites & Installation

**Python 3.10+** is required.

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
- A session store — implement session middleware using a dict, Redis, or a signed cookie

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

Every handler is an `async` function receiving `(scope, receive, send)`.  Pass a
`Response` or `JSONResponse` directly to `send` — BlackBull unwraps it automatically.

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

Use `{name}` segments in the path string.  Parameters are always strings and are
available in `scope['path_params']`:

```python
@app.route(path='/tasks/{task_id}')
async def get_task(scope, receive, send):
    task_id = scope['path_params']['task_id']   # str
    await send(Response(task_id.encode()))
```

`{name}` matches `[a-zA-Z0-9_\-\.\~]+`.  For other patterns supply a compiled
regex with named groups; the captured values are always injected into
`scope['path_params']` — the same place as `{name}` parameters:

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

BlackBull validates `Sec-WebSocket-Version: 13` and supports per-message deflate
(RFC 7692) automatically when the client negotiates it.

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

### 3.4  Detecting client disconnection

When the remote side closes the connection, `receive()` returns
`{'type': 'http.disconnect'}`.  This is useful for long-polling and
server-sent events (SSE) to avoid writing to a closed socket.

> **Note — streaming responses**: A `StreamingResponse` helper is **not yet
> implemented**.  To push multiple body chunks (e.g. SSE), use the raw ASGI
> event-dict API with `more_body=True`.  Passing `Response(b'')` to `send` ends
> the connection immediately with `Content-Length: 0` and cannot be followed by
> additional chunks.

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
To change the threshold, construct `CompressionMiddleware` directly:

```python
from blackbull.middleware.compression import CompressionMiddleware

compress_big = CompressionMiddleware(min_size=4096)

@app.route(path='/large', middlewares=[compress_big])
async def large(scope, receive, send):
    ...
```

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

### 6.4  Parsing cookies

```python
from blackbull import parse_cookies

cookies: dict[str, str] = parse_cookies(scope)
session = cookies.get('session', '')
```

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

## §9  Lifespan Hooks

```python
@app.on_startup
async def startup():
    await db.connect()
    print('Database ready')

@app.on_shutdown
async def shutdown():
    await db.disconnect()
```

- `on_startup` fires after the server socket is bound, before accepting connections.
- `on_shutdown` fires when the server receives a stop signal (e.g. `Ctrl-C`).
- Multiple hooks can be registered; they run in registration order.

---

## §10  Running the Server

```python
import asyncio

# HTTP/1.1
asyncio.run(app.run(port=8000))

# HTTPS + HTTP/2 (negotiated via TLS ALPN)
asyncio.run(app.run(port=8443, certfile='cert.pem', keyfile='key.pem'))

# Development: hot-reload when source files change
asyncio.run(app.run(port=8000, debug=True))
```

`app.run()` signature:

```python
async def run(port=0, certfile=None, keyfile=None, debug=False)
```

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

**Minimal nginx config (HTTP/2, proxy to BlackBull on port 8000):**

```nginx
server {
    listen 443 ssl http2;
    server_name example.com;

    ssl_certificate     /etc/letsencrypt/live/example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/example.com/privkey.pem;

    location / {
        proxy_pass         http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header   Host $host;
        proxy_set_header   X-Forwarded-For $proxy_add_x_forwarded_for;
    }
}
```

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

# ── Middleware ────────────────────────────────────────────────────────────────

async def error_mw(scope, receive, send, call_next):
    """Catch unhandled exceptions and return JSON 500."""
    try:
        await call_next(scope, receive, send)
    except Exception as exc:
        await send(JSONResponse({'error': str(exc)},
                                status=HTTPStatus.INTERNAL_SERVER_ERROR))


async def logging_mw(scope, receive, send, call_next):
    import time
    t0 = time.monotonic()
    print(f"→ {scope.get('method')} {scope.get('path')}")
    await call_next(scope, receive, send)
    print(f"  {(time.monotonic() - t0) * 1000:.1f} ms")


async def auth_mw(scope, receive, send, call_next):
    """Validate Bearer token; inject scope['user'] and scope['token']."""
    auth = scope['headers'].get(b'authorization', b'')
    token = auth[7:].decode() if auth.startswith(b'Bearer ') else ''
    user = SESSIONS.get(token)
    if not user:
        await send(JSONResponse({'error': 'Unauthorized'},
                                status=HTTPStatus.UNAUTHORIZED))
        return
    scope['user'] = user
    scope['token'] = token
    await call_next(scope, receive, send)


async def json_body_mw(scope, receive, send, call_next):
    """Parse request body as JSON; inject scope['json']."""
    try:
        scope['json'] = json.loads(await read_body(receive))
    except (json.JSONDecodeError, ValueError):
        await send(JSONResponse({'error': 'Invalid JSON'},
                                status=HTTPStatus.BAD_REQUEST))
        return
    await call_next(scope, receive, send)

# ── Route groups ──────────────────────────────────────────────────────────────

public = app.group(middlewares=[error_mw, logging_mw])
api    = app.group(middlewares=[error_mw, logging_mw, auth_mw])

# ── Lifespan ──────────────────────────────────────────────────────────────────

@app.on_startup
async def startup():
    print('Server ready. Default user: admin / admin')
    # e.g.: await db.init()

# ── Public routes ─────────────────────────────────────────────────────────────

@public.route(methods=[HTTPMethod.GET], path='/')
async def index(scope, receive, send):
    await send(Response(b'<h1>Login page</h1>'))


@public.route(methods=[HTTPMethod.GET], path='/app')
async def app_page(scope, receive, send):
    await send(Response(b'<h1>Task Manager</h1>'))  # serve index.html


@public.route(methods=[HTTPMethod.POST], path='/register',
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


@public.route(methods=[HTTPMethod.POST], path='/login',
              middlewares=[json_body_mw])
async def login(scope, receive, send):
    data = scope['json']
    username = str(data.get('username', '')).strip()
    password = str(data.get('password', ''))
    # ... verify_user(username, password) ...
    token = secrets.token_urlsafe(32)
    SESSIONS[token] = username
    await send(JSONResponse({'ok': True, 'token': token}))

# ── Protected API routes ──────────────────────────────────────────────────────

@api.route(methods=[HTTPMethod.GET], path='/tasks')
async def list_tasks(scope, receive, send):
    user = scope['user']
    tasks = []  # replace with: await db.get_tasks(user)
    await send(JSONResponse(tasks))


@api.route(methods=[HTTPMethod.POST], path='/tasks',
           middlewares=[json_body_mw])
async def create_task(scope, receive, send):
    title = str(scope['json'].get('title', '')).strip()
    if not title:
        await send(JSONResponse({'error': 'title required'},
                                status=HTTPStatus.BAD_REQUEST))
        return
    task = {'id': 1, 'title': title, 'completed': False}  # replace with DB call
    await send(JSONResponse(task, status=HTTPStatus.CREATED))


@api.route(methods=[HTTPMethod.PUT], path='/tasks/{task_id}',
           middlewares=[json_body_mw])
async def update_task(scope, receive, send):
    task_id = scope['path_params']['task_id']
    # ... db.update_task(scope['user'], int(task_id), ...) ...
    await send(JSONResponse({'id': task_id, 'title': 'updated'}))


@api.route(methods=[HTTPMethod.DELETE], path='/tasks/{task_id}')
async def delete_task(scope, receive, send):
    task_id = scope['path_params']['task_id']
    # ... db.delete_task(scope['user'], int(task_id)) ...
    await send(JSONResponse({'ok': True}))


@api.route(methods=[HTTPMethod.POST], path='/logout')
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
[`examples/SimpleTaskManager/app.py`](../examples/SimpleTaskManager/app.py).

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
pip install httpx pytest-asyncio
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

`pytest.ini` or `pyproject.toml` to configure asyncio mode:

```ini
[pytest]
asyncio_mode = auto
```

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

Browsers send a pre-flight `OPTIONS` request before cross-origin fetch calls.
BlackBull has no built-in CORS middleware, but one is straightforward to write:

```python
from http import HTTPStatus
from blackbull import Response

ALLOWED_ORIGINS = {
    'http://localhost:3000',
    'https://myapp.example.com',
}

async def cors_mw(scope, receive, send, call_next):
    origin = scope['headers'].get(b'origin', b'').decode()
    allowed = origin in ALLOWED_ORIGINS

    # Pre-flight request
    if scope['method'] == 'OPTIONS':
        hdrs = [
            (b'access-control-allow-methods', b'GET,POST,PUT,DELETE,OPTIONS'),
            (b'access-control-allow-headers', b'Authorization,Content-Type'),
            (b'access-control-max-age',        b'3600'),
        ]
        if allowed:
            hdrs.append((b'access-control-allow-origin', origin.encode()))
        await send(Response(b'', status=HTTPStatus.NO_CONTENT, headers=hdrs))
        return

    # Wrap send to inject CORS header on every response
    _send = send
    async def cors_send(body, status=HTTPStatus.OK, headers=[]):
        hdrs = list(headers)
        if allowed:
            hdrs.append((b'access-control-allow-origin', origin.encode()))
        await _send(body, status, hdrs)

    await call_next(scope, receive, cors_send)
```

Apply it to every route via a group:

```python
app_group = app.group(middlewares=[cors_mw, error_mw, logging_mw])
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

### 14.4  Forwarding access logs to a remote server

`logging.Handler.emit()` is synchronous.  Calling a blocking HTTP request
directly from `emit` would stall the asyncio event loop.  The solution is
the standard library's `QueueHandler` + `QueueListener` pair: the handler
enqueues records in O(1) without blocking, and a background thread drains
the queue and calls the real (blocking) HTTP handler.

A complete two-process example is provided in
[`examples/LoggingExample/`](../examples/LoggingExample/):

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
_listener.start()

logging.getLogger('blackbull.access').addHandler(QueueHandler(_log_queue))
logging.getLogger('blackbull.access').setLevel(logging.INFO)
```

`QueueHandler.emit()` puts the record in the queue and returns immediately.
`QueueListener` runs in a daemon thread and calls `JsonHTTPHandler.emit()`
there — the blocking HTTP call never touches the event-loop thread.

Call `_listener.stop()` on shutdown to flush the queue and join the thread.

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
- `Respond2PriorityUpdate` — no-op responder that logs the hint at DEBUG
  level via `response.py`'s `RespondFactory`.

**Weight off-by-one (RFC 7540 §6.3)**: the wire format stores `weight − 1`
(range 0–255 → logical weight 1–256).  BlackBull adds 1 on parse so
`stream.weight` always holds the logical value.

### 15.3  Flow control

HTTP/2 has two independent flow-control windows:

- **Connection-level**: shared across all streams on one connection.
- **Stream-level**: per-stream budget.

`HTTP2Sender._write()` blocks on `asyncio.Event` when the window is
exhausted.  `Respond2WindowUpdate.respond()` credits the relevant sender(s)
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

#### Middleware example

The pattern scales cleanly to middleware so every route in a group benefits
without repeating the check:

```python
async def priority_mw(scope, receive, send, call_next):
    hint = scope.get('http2_priority', {'urgency': 3, 'incremental': False})
    urgency = hint['urgency']
    scope['_priority_urgency'] = urgency   # pass downstream

    if urgency <= 1:
        logger.warning('HIGH-PRIORITY %s %s u=%d',
                       scope.get('method'), scope.get('path'), urgency)
    elif urgency >= 6:
        scope['_background'] = True        # suppress verbose logging

    await call_next(scope, receive, send)

api = app.group(middlewares=[priority_mw, auth_mw])
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
