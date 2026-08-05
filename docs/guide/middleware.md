# Middleware

Middleware wraps a handler with cross-cutting concerns — logging,
auth, body parsing, response shaping — without changing the
handler's signature.  The shape is the same as Starlette /
Quart / ASGI 3.0 generally, with one BlackBull convenience
(`@as_middleware`) layered on top.

## Writing a middleware

```python
import time

async def logging_mw(conn, receive, send, call_next):
    t0 = time.monotonic()
    await call_next(conn, receive, send)
    elapsed = (time.monotonic() - t0) * 1000
    print(f"{conn.method} {conn.path}  {elapsed:.1f} ms")
```

Signature: `async def mw(conn, receive, send, call_next)`.

- Call `await call_next(conn, receive, send)` to pass control to the
  next layer.
- The legacy parameter name `inner` is accepted as an alias for
  `call_next`.
- Sending a response without calling `call_next` **short-circuits**
  all inner layers.

Middleware functions keep the full `(conn, receive, send, call_next)`
shape; the simplified handler form does **not** apply to them.

### The first parameter's *name* picks the request form

BlackBull's own server threads a typed
[`Connection`](requests-and-responses.md) end-to-end — `conn.method`,
`conn.path`, `conn.headers.get(b'accept')`.  A middleware written against
plain ASGI wants the scope dict instead, so `@as_middleware` reads the
name you gave the parameter:

| First parameter named | Receives | `send` wrapper observes |
|---|---|---|
| anything but `scope` (`conn`, `connection`, …) | `Connection` | `NativeResponse` on the HTTP path |
| `scope` | a real ASGI scope `dict` | ASGI event dicts |

```python
@as_middleware
async def asgi_style_mw(scope, receive, send, call_next):
    # A genuine scope dict: subscripting works.
    print(scope['method'], scope['path'])
    await call_next(scope, receive, send)
```

The word `scope` means a genuine ASGI scope dict everywhere in BlackBull
and never a `Connection` — which is why the name is enough to declare
your intent.  The dict lives across your frame only: `call_next` always
passes the `Connection` down, so a middleware below yours is unaffected
by the one above it.  The choice is made once, from the signature, when
the decorator runs — a native middleware pays nothing for the feature.

Undecorated middleware is not adapted either way and always receives the
`Connection`.

## Typing the message channel

The events crossing `receive` and `send` follow the ASGI 3.0 wire contract
on the **ASGI lanes** — WebSocket and the external-host edge
(`BlackBull(asgi=True)` under uvicorn) — as plain ASGI dicts.  On
BlackBull's own **HTTP path (HTTP/1.1 and HTTP/2)** the response side
carries `NativeResponse` objects instead: one message may hold any
combination of a header arm, body chunks, and trailers, and its *presence*
semantics are `is not None` (never truthiness — an empty body is a real
body).

BlackBull also ships a set of `TypedDict` *declarations* for the ASGI event
shapes, so a type checker can tell which keys are legal on which event:

```python
from blackbull import (ASGIReceiveCallable, ASGISendCallable, ASGISendEvent,
                       as_middleware)

@as_middleware                       # required — it is what adapts the edges
async def add_header_mw(scope, receive: ASGIReceiveCallable,
                        send: ASGISendCallable, call_next):
    async def wrapped(event: ASGISendEvent) -> None:
        if event['type'] == 'http.response.start':
            # Narrowed here: pyright/mypy know `status` and `headers` exist
            # on this branch, and that `body` does not.
            event = {**event,
                     'headers': list(event.get('headers', [])) + [(b'x-custom', b'1')]}
        await send(event)
    await call_next(scope, receive, wrapped)
```

These declarations describe the **ASGI** form, so they fit a middleware
that asked for one — decorated and with its first parameter named
`scope`.  Name it `conn` and the events are `NativeResponse` objects, for
which `ASGISendEvent` is the wrong annotation.

`ASGISendEvent` and `ASGIReceiveEvent` are unions discriminated on the
`type` key, so comparing or `match`-ing against it narrows the union to a
single member.  A checker then rejects the classic mistake this guards —
passing a `Response` object where an event dict belongs, which fails at
runtime on `event['type']`.

The individual shapes (`HTTPResponseStartEvent`, `WebSocketReceiveEvent`, …)
are importable from `blackbull.asgi` if you want to name one directly.
Everything here is annotation-only: nothing is validated or converted at
runtime, and unannotated middleware keeps working exactly as before.

!!! note "Native path: the send channel carries `NativeResponse`"
    On BlackBull's own HTTP path (HTTP/1.1 and HTTP/2), the response events
    a middleware's inner `send` wrapper observes are `NativeResponse`
    objects — not dicts.  A middleware that subscripts `event['type']`
    must instead branch on the object's arms: `event.header is not None`
    (header arm, with `event.status` / `event.header.get(b'name')`),
    `event.body is not None` (body chunk, with `event.more_body`), and
    `event.trailers is not None`.  The `@as_middleware` decorator
    guarantees this single native representation for the wrappers it
    normalises; only on the WebSocket / external-host lanes do the same
    wrappers see plain dicts.  Use
    [`blackbull.testing.native`](testing.md) to exercise the native path.

## The `@as_middleware` decorator

Route handlers can return `Response` / `JSONResponse` objects
instead of calling `send(...)` with raw ASGI events.  A middleware
that wraps `send` would otherwise have to handle both forms.
Decorate the middleware with `@as_middleware` and the inner
wrapper sees a single native representation: `NativeResponse` on
the HTTP path (HTTP/1.1 and HTTP/2), plain ASGI dicts on the
WebSocket / external-host lanes —
`Response` objects are converted for you, never leaking through:

```python
from blackbull import as_middleware
from blackbull.native import NativeResponse

@as_middleware
async def add_header_mw(conn, receive, send, call_next):
    async def wrapped(event):
        if isinstance(event, NativeResponse):
            if event.header is not None:
                # header arm — append zero-copy; visible to the sender
                event.header.append(b'x-custom', b'1')
        else:
            # WebSocket / external-host lanes still carry ASGI dicts
            if event['type'] == 'http.response.start':
                event = {**event,
                         'headers': list(event.get('headers', []))
                         + [(b'x-custom', b'1')]}
        await send(event)
    await call_next(conn, receive, wrapped)
```

On the native path a complete response is **one object, one `send`**
(header + body together), while a streamed response is a header object
followed by body-chunk objects.  Remember the presence contract: test
`event.header is not None` / `event.body is not None`, never truthiness.

If you would rather not branch at all, name the first parameter `scope`
and every event is an ASGI dict — see
[the parameter-name rule](#the-first-parameters-name-picks-the-request-form)
above.

`@as_middleware` also works on classes (it wraps `__call__`).  All
of BlackBull's built-in middleware uses the class form:

```python
@as_middleware
class TimingMiddleware:
    def __init__(self, threshold_ms: float = 100.0):
        self._threshold = threshold_ms
    async def __call__(self, conn, receive, send, call_next):
        ...
```

The name rule reads `__call__`'s first parameter after `self`, so a class
middleware opts into the ASGI form the same way a function does.

Omit the decorator when you need raw `send` arguments — e.g.
middleware used in a deployment that never registers simplified
handlers.

!!! tip "Observation vs. interception"
    Middleware in BlackBull is sugar over the
    `@app.intercept('before_handler')` hook.  For purely
    observational concerns (logging, metrics, tracing) prefer
    `@app.on(...)` instead — middleware forces every observer to
    run on the request critical path, and one slow observer can
    degrade every response.  See [Events](events.md).

## Attaching middleware to a route

```python
@app.route(path='/protected', middlewares=[auth_mw, logging_mw])
async def handler(scope, receive, send):
    ...
```

The list is **outer-to-inner**: the first entry runs first on the
way in, last on the way out.

```
            ┌─ auth_mw ──────────────────────────────────┐
  request → │  ┌─ logging_mw ─────────────────────────┐  │ → response
            │  │  ┌─ handler ─┐                       │  │
            │  │  │  (runs)   │                       │  │
            │  │  └───────────┘                       │  │
            │  └──────────────────────────────────────┘  │
            └────────────────────────────────────────────┘
```

`auth_mw` runs first; it either short-circuits or delegates to
`logging_mw`, which then delegates to `handler`.  Post-handler code
(after `await call_next(...)`) runs in reverse order: `logging_mw`
post → `auth_mw` post.

For routes that share a middleware prefix, use
[Route Groups](routing.md#route-groups).

## Global middleware

`app.use(mw)` registers a middleware that wraps **every** route:

```python
from blackbull import BlackBull
from blackbull.middleware.compression import Compression

app = BlackBull()
app.use(Compression())            # applies to all routes
```

Global middleware run **outside** per-route middleware.  The
effective order at request time is:

```
global mw → route-group mw → per-route mw → handler
```

Each layer can short-circuit (return without calling `call_next`)
to skip everything below it.

## Built-in middleware

### `websocket`

Consumes the initial `websocket.connect` event and sends
`websocket.accept` so the inner handler can skip the boilerplate:

```python
from blackbull.middleware import websocket
from blackbull.utils import Scheme

@app.route(path='/chat', scheme=Scheme.websocket, middlewares=[websocket])
async def chat(scope, receive, send):
    while True:
        event = await receive()
        if event['type'] == 'websocket.disconnect':
            break
        await send({'type': 'websocket.send', 'text': event.get('text', '')})
```

Works with the high-level `WebSocket` handler form too — it records the
completed handshake on the connection, so the object adopts it rather than
waiting for a `websocket.connect` that is already gone.  See
[WebSockets](websockets.md#the-websocket-middleware) for the exact
semantics, including what happens if the handler also calls `accept()` or
closes the connection itself.

### `Compression` / `compress`

Compresses HTTP response bodies using the codec the client prefers
(brotli > zstd > gzip, based on `Accept-Encoding`):

```python
from blackbull.middleware import compress
from blackbull.middleware.compression import Compression

@app.route(path='/data', middlewares=[compress])
async def data_handler(scope, receive, send):
    await send(Response(large_payload))

# Or with a higher size threshold:
@app.route(path='/large', middlewares=[Compression(min_size=4096)])
async def large(scope, receive, send):
    ...
```

Brotli and zstandard are optional extras:

```bash
pip install 'blackbull[compression]'
```

The default `min_size` is 100 bytes — responses smaller than that
pass through uncompressed.

Every compressed response carries `Vary: Accept-Encoding` (folded into
any existing `Vary`) so a shared cache never replays an encoded body to
a client that sent `identity` / no `Accept-Encoding` (RFC 9110 §12.5.5).
A response that is *compressible but not compressed* — below `min_size`,
or served straight from disk — carries it too, for the same reason.

Large files that [`StaticFiles`](#staticfiles) hands to the sender as a
path (`http.response.pathsend`, so the kernel can `sendfile` them) are
served uncompressed: the middleware never sees those bytes.  Pre-compress
them on disk instead — `StaticFiles` serves a `.br` / `.gz` sibling when
the client accepts it, which costs no CPU per request.

### Sessions — moved to `blackbull-session`

Signed-cookie sessions live in the standalone
[`blackbull-session`](https://github.com/TOKUJI/blackbull-session)
package, following the [`init_app(app)`](extensions.md) extension
convention.  The in-tree `blackbull.middleware.Session` was
deprecated in 0.38 and **removed in 0.54.0**.

Migration is a one-line swap:

```python
# pip install blackbull-session
from blackbull_session import SessionExtension

SessionExtension(app)                        # reads BB_SESSION_SECRET
SessionExtension(app, secret=b'<long-random-bytes>')   # explicit secret

@app.route(path='/whoami')
async def whoami(conn, receive, send):
    await send(Response(conn.state['session'].get('user', 'anonymous')))
```

Handlers read and write the session through `conn.state['session']`;
before; see the `blackbull-session` README for cookie attributes,
secret resolution, and session-clearing semantics.

### `Cache`

Per-worker in-memory response cache for `GET` and `HEAD`.
Captures the handler's response on the first hit, stores it under
`(method, path, query_string)`, and replays it directly on
subsequent matching requests until the entry expires.

```python
from blackbull.middleware.cache import Cache

app.use(Cache(max_age=600))   # 10-minute TTL

@app.route(path='/feed')
async def feed(scope, receive, send):
    items = await fetch_news()           # expensive
    body = render(items).encode()
    await send({'type': 'http.response.start', 'status': 200,
                'headers': [(b'content-type', b'text/html')]})
    await send({'type': 'http.response.body', 'body': body})
```

A weak ETag (`W/"<sha256-prefix>"`) is generated automatically when
the handler doesn't supply one.  Subsequent requests with a
matching `If-None-Match` header receive `304 Not Modified` with no
body.

Standard `Cache-Control` directives are honoured.  Responses
carrying `no-store`, `private`, or `no-cache` pass through
unstored.  Requests with `Cache-Control: no-store` bypass the
cache too.

The cache is **variant-aware**: when a stored response carries a
`Vary` header (e.g. `Vary: Accept-Encoding` behind the `Compression`
middleware), the varied request-header values are folded into the
cache key, so a brotli variant is never replayed to an `identity`
client.  A response with `Vary: *` is passed through unstored
(RFC 9110 §12.5.5).

Constructor arguments:

| Argument               | Default                | Notes                                                                  |
|------------------------|------------------------|------------------------------------------------------------------------|
| `max_age`              | `300`                  | TTL in seconds when the response does not specify its own.            |
| `max_entries`          | `1024`                 | LRU cap on cached responses.                                           |
| `cacheable_methods`    | `{'GET', 'HEAD'}`      | Methods eligible for caching.                                          |
| `cacheable_statuses`   | `{200, 203, 300, 301, 308, 404, 410, 414, 451}` | Status codes eligible for caching. |
| `cache_authenticated`  | `False`                | When `False`, requests with `Authorization` bypass the cache (RFC 9111 §3.5). |
| `generate_etag`        | `True`                 | Auto-generate `ETag` when the handler omits it.                        |

Limitations:

- **Per-worker.**  Multi-worker deployments hold a separate cache
  in each process.
- **No cross-restart persistence.**  In-memory only.
- **No explicit invalidation API.**  Wait for TTL or restart the
  worker.
- **Streaming responses** (any `more_body=True` chunk) are
  forwarded straight through without caching.

### `CORS`

Handles preflight `OPTIONS` requests and adds the required
`Access-Control-*` headers to actual cross-origin responses.

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

| Parameter | Type | Default | Notes |
|---|---|---|---|
| `allow_origins` | `list[str] \| str` | `'*'` | Explicit origin strings, or `'*'` for wildcard |
| `allow_methods` | `list[str]` | `['GET','POST','HEAD','OPTIONS']` | Methods allowed in preflight |
| `allow_headers` | `list[str] \| str` | `'*'` | Request headers allowed |
| `allow_credentials` | `bool` | `False` | Emit `Access-Control-Allow-Credentials: true` |
| `expose_headers` | `list[str]` | `[]` | Response headers the browser JS may read |
| `max_age` | `int \| None` | `600` | Preflight cache seconds; `None` omits the header |

`allow_credentials=True` cannot be combined with
`allow_origins=['*']` — the CORS spec forbids it.  List explicit
origins instead.

Apply to specific route groups rather than globally when only some
routes need CORS:

```python
api = app.group(middlewares=[CORS(allow_origins=['https://myapp.example.com'])])

@api.route(path='/items')
async def list_items(): ...
```

### `StaticFiles`

Serves files from a directory under a URL prefix.  See
[Static files](static-files.md) for the configuration surface
(URL prefix, range requests, in-memory cache, PROD-mode passthrough).

## Recipes

### Request ID

Attach a unique ID to every request for distributed tracing:

```python
import uuid
from blackbull import as_middleware
from blackbull.native import NativeResponse

@as_middleware
async def request_id_mw(conn, receive, send, call_next):
    req_id = (conn.headers.get(b'x-request-id', b'')
              or uuid.uuid4().hex.encode())
    conn.state['request_id'] = req_id.decode()

    async def tagged_send(event):
        if isinstance(event, NativeResponse) and event.header is not None:
            event.header.append(b'x-request-id', req_id)
        await send(event)

    await call_next(conn, receive, tagged_send)
```

Inner layers and the handler read it back as
`conn.state['request_id']`.

### Rate limiting (token bucket, in-process)

```python
import time
from collections import defaultdict
from http import HTTPStatus
from blackbull import JSONResponse

_buckets: dict[str, tuple[float, int]] = defaultdict(lambda: (time.monotonic(), 0))
RATE_LIMIT = 60   # requests per minute per IP

async def rate_limit_mw(conn, receive, send, call_next):
    ip = (conn.client or ['unknown'])[0]
    now = time.monotonic()
    window_start, count = _buckets[ip]

    if now - window_start > 60:
        _buckets[ip] = (now, 1)
    elif count >= RATE_LIMIT:
        await send(JSONResponse({'error': 'rate limit exceeded'},
                                status=HTTPStatus.TOO_MANY_REQUESTS))
        return
    else:
        _buckets[ip] = (window_start, count + 1)

    await call_next(conn, receive, send)
```

Per-IP in-process limiting is fine for a single-worker deployment.
For multi-worker or multi-host setups, use a shared store (Redis,
Memcached) so the bucket survives across workers.

### Post-response middleware (inspect / modify the response)

The middleware shape `(conn, receive, send, call_next)` runs
*around* the handler — code before `await call_next(...)` sees the
request, code after sees that the handler returned but **not** what
the handler sent.  Response status, headers, and body all flow
through `send`, not through `call_next`'s return value.

To inspect or modify the response, wrap `send` and forward each
event yourself:

```python
from blackbull import as_middleware
from blackbull.native import NativeResponse

@as_middleware
async def log_status_mw(conn, receive, send, call_next):
    captured_status = None

    async def intercepting_send(event):
        nonlocal captured_status
        if isinstance(event, NativeResponse) and event.header is not None:
            captured_status = event.status
        await send(event)

    await call_next(conn, receive, intercepting_send)
    print(f'{conn.method} {conn.path} → {captured_status}')
```

The handler now sends to `intercepting_send`, which records the
status off the header arm and forwards every event to the outer
`send` unchanged.  Once `call_next` returns, you have the captured
value.

The pattern generalises to any modification:

| Goal | Where to act in `intercepting_send` |
|---|---|
| Add a response header | `event.header.append(...)` on the header arm before forwarding |
| Compute a checksum / size | accumulate `event.body` while `event.body is not None`, finalise when `more_body` is false |
| Replace the body | buffer body parts; on the terminal body, emit your replacement and skip the original |
| Short-circuit a status code | on the header arm, decide whether to forward as-is or synthesise a different response |

`Compression` ([`blackbull/middleware/compression.py`](https://github.com/TOKUJI/BlackBull/blob/master/blackbull/middleware/compression.py))
is the reference implementation: it buffers body parts, compresses
the joined payload, and emits replacement headers + body when the
handler finishes.

!!! note "Streaming responses"
    `intercepting_send` receives every body event, including chunks
    sent with `more_body=True`.  If your goal is to inspect the
    *complete* response, either buffer until `more_body` is false (as
    `Compression` does for non-streaming payloads) or fall back to
    pass-through when streaming is detected — buffering an unbounded
    stream defeats the point.

### Passing values to inner layers

`conn.state` is the per-request scratch dict every layer shares:

```python
@as_middleware
async def auth_mw(conn, receive, send, call_next):
    auth = conn.headers.get(b'authorization', b'')
    token = auth[7:].decode() if auth.startswith(b'Bearer ') else ''
    user = SESSIONS.get(token)
    if not user:
        await send(JSONResponse({'error': 'Unauthorized'},
                                status=HTTPStatus.UNAUTHORIZED))
        return                       # short-circuit: inner layers never run
    conn.state['user'] = user        # available to all inner layers
    conn.state['token'] = token
    await call_next(conn, receive, send)
```

!!! warning "`state`, not a top-level key"
    Use `conn.state[...]`.  Setting an attribute or a top-level scope
    key on the request object does **not** reach inner layers: the
    scope form a `scope`-declaring middleware receives is built for
    that frame, and `call_next` passes the `Connection` down.  Only
    `state` (and `extensions`) are shared by reference across the
    whole chain, which is why they are the injection points.

The path-parameter dict `conn.path_params` and the framework error
keys (`conn.state['error_status']`, etc.) are populated by the
framework — see [Error handling](error-handling.md).

## Next

- [Error handling](error-handling.md) — custom error handlers,
  the DEV-mode traceback page.
- [Events](events.md) — `@app.on` / `@app.intercept` for
  observational and interceptive hooks.
- [Static files](static-files.md) — `StaticFiles` middleware
  configuration.
