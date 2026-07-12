# Middleware

Middleware wraps a handler with cross-cutting concerns — logging,
auth, body parsing, response shaping — without changing the
handler's signature.  The shape is the same as Starlette /
Quart / ASGI 3.0 generally, with one BlackBull convenience
(`@as_middleware`) layered on top.

## Writing a middleware

```python
import time

async def logging_mw(scope, receive, send, call_next):
    t0 = time.monotonic()
    await call_next(scope, receive, send)
    elapsed = (time.monotonic() - t0) * 1000
    print(f"{scope['method']} {scope['path']}  {elapsed:.1f} ms")
```

Signature: `async def mw(scope, receive, send, call_next)`.

- Call `await call_next(scope, receive, send)` to pass control to the
  next layer.
- The legacy parameter name `inner` is accepted as an alias for
  `call_next`.
- Sending a response without calling `call_next` **short-circuits**
  all inner layers.

Middleware functions keep the full `(scope, receive, send, call_next)`
shape; the simplified handler form does **not** apply to them.

## The `@as_middleware` decorator

Route handlers can return `Response` / `JSONResponse` objects
instead of calling `send(...)` with raw ASGI events.  A middleware
that wraps `send` would otherwise have to handle both forms.
Decorate the middleware with `@as_middleware` and the inner
wrapper sees only plain dict events — `Response` objects are
expanded into `http.response.start` + `http.response.body` for you:

```python
from blackbull import as_middleware

@as_middleware
async def add_header_mw(scope, receive, send, call_next):
    async def wrapped(event):
        if event['type'] == 'http.response.start':
            event = {**event,
                     'headers': list(event.get('headers', [])) + [(b'x-custom', b'1')]}
        await send(event)
    await call_next(scope, receive, wrapped)
```

`@as_middleware` also works on classes (it wraps `__call__`).  All
of BlackBull's built-in middleware uses the class form:

```python
@as_middleware
class TimingMiddleware:
    def __init__(self, threshold_ms: float = 100.0):
        self._threshold = threshold_ms
    async def __call__(self, scope, receive, send, call_next):
        ...
```

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

### `Session`

Signed-cookie sessions — session data lives entirely in a cookie
HMAC-signed by the server.  No server-side store, no database.
Trade-off: cookies are capped at ~4 KiB by browsers and you can't
revoke a session early without rotating the secret.

!!! warning "Moved to a separate package"
    Starting with BlackBull 0.38 the session layer lives in the
    standalone [`blackbull-session`](https://github.com/TOKUJI/blackbull-session)
    package, following the [`init_app(app)`](extensions.md)
    extension convention.  The in-tree
    `blackbull.middleware.Session` emits a `DeprecationWarning` and
    will be removed no earlier than BlackBull v0.41 (and not before
    2026-07-14).

```python
# pip install blackbull-session
from blackbull_session import SessionExtension

# Operator sets BB_SESSION_SECRET=<32-byte random> in the deployment env
SessionExtension(app)

# Or pass the secret explicitly (handy for tests):
SessionExtension(app, secret=b'<long-random-bytes>')

@app.route(path='/')
async def index(scope, receive, send):
    scope['session']['user'] = 'alice'           # any JSON-serializable value
    await send(Response('signed in'))

@app.route(path='/whoami')
async def whoami(scope, receive, send):
    await send(Response(scope['session'].get('user', 'anonymous')))
```

After construction the live extension is reachable at
`app.extensions['session']`.

`scope['session']` is a `dict` subclass that tracks whether you
mutated it.  The middleware only emits `Set-Cookie` when a request
handler changed the session — read-only handlers leave the response
cache-friendly.

Secret resolution:

- The constructor accepts `secret=` directly.
- When `secret` is `None`, the middleware reads `BB_SESSION_SECRET`
  from the environment.
- If neither is set, construction raises — there is no insecure
  default.  Generate one with:
  ```bash
  python -c "import secrets; print(secrets.token_urlsafe(32))"
  ```

Cookie attributes (all keyword arguments, with sensible defaults):

| Argument        | Default     | Notes                                                                  |
|-----------------|-------------|------------------------------------------------------------------------|
| `cookie_name`   | `'session'` | Name of the cookie carrying the payload.                              |
| `max_age`       | `None`      | Seconds the cookie is valid.  `None` ⇒ session cookie (until browser closes). |
| `secure`        | `True`      | Send the `Secure` attribute (cookie only over HTTPS).                  |
| `httponly`      | `True`      | Send the `HttpOnly` attribute (JS can't read the cookie).              |
| `samesite`      | `'Lax'`     | `'Strict'`, `'Lax'`, `'None'`, or `None` (omit).                       |
| `path`          | `'/'`       | Cookie `Path`.                                                         |

Clearing the session emits a tombstone cookie with `Max-Age=0`:

```python
@app.route(path='/logout')
async def logout(scope, receive, send):
    scope['session'].clear()
    await send(Response('signed out'))
```

A cookie whose signature fails to verify (tampering, wrong secret)
is silently dropped — the handler sees an empty session.

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

async def request_id_mw(scope, receive, send, call_next):
    req_id = (scope['headers'].get(b'x-request-id', b'')
              or uuid.uuid4().hex.encode())
    scope['request_id'] = (req_id.decode()
                           if isinstance(req_id, bytes) else req_id)

    _send = send
    async def tagged_send(body, status=200, headers=[]):
        await _send(body, status,
                    list(headers) + [(b'x-request-id', scope['request_id'].encode())])

    await call_next(scope, receive, tagged_send)
```

### Rate limiting (token bucket, in-process)

```python
import time
from collections import defaultdict
from http import HTTPStatus
from blackbull import JSONResponse

_buckets: dict[str, tuple[float, int]] = defaultdict(lambda: (time.monotonic(), 0))
RATE_LIMIT = 60   # requests per minute per IP

async def rate_limit_mw(scope, receive, send, call_next):
    ip = (scope.get('client') or ['unknown'])[0]
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

    await call_next(scope, receive, send)
```

Per-IP in-process limiting is fine for a single-worker deployment.
For multi-worker or multi-host setups, use a shared store (Redis,
Memcached) so the bucket survives across workers.

### Post-response middleware (inspect / modify the response)

The middleware shape `(scope, receive, send, call_next)` runs
*around* the handler — code before `await call_next(...)` sees the
request, code after sees that the handler returned but **not** what
the handler sent.  Response status, headers, and body all flow
through `send`, not through `call_next`'s return value.

To inspect or modify the response, wrap `send` and forward each
ASGI event yourself:

```python
async def log_status_mw(scope, receive, send, call_next):
    captured_status = None

    async def intercepting_send(event):
        nonlocal captured_status
        if event['type'] == 'http.response.start':
            captured_status = event['status']
        await send(event)

    await call_next(scope, receive, intercepting_send)
    print(f"{scope['method']} {scope['path']} → {captured_status}")
```

The handler now sends to `intercepting_send`, which records the
status off the `http.response.start` event and forwards every event
to the outer `send` unchanged.  Once `call_next` returns, you have
the captured value.

The pattern generalises to any modification:

| Goal | Where to act in `intercepting_send` |
|---|---|
| Add a response header | rewrite `event['headers']` on `http.response.start` before forwarding |
| Compute a checksum / size | accumulate `event['body']` on `http.response.body`, finalise when `more_body=False` |
| Replace the body | buffer body parts; on the final body event, emit your replacement and skip the original |
| Short-circuit a status code | on `http.response.start`, decide whether to forward as-is or synthesise a different response |

`Compression` ([`blackbull/middleware/compression.py`](https://github.com/TOKUJI/BlackBull/blob/master/blackbull/middleware/compression.py))
is the reference implementation: it buffers body parts, compresses
the joined payload, and emits replacement headers + body when the
handler finishes.

!!! note "Streaming responses"
    `intercepting_send` receives every body event, including chunks
    sent with `more_body=True`.  If your goal is to inspect the
    *complete* response, either buffer until `more_body=False` (as
    `Compression` does for non-streaming payloads) or fall back to
    pass-through when streaming is detected — buffering an unbounded
    stream defeats the point.

### Injecting values into scope

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

The path-parameter dict `scope['path_params']` and the framework
error keys (`scope['state']['error_status']`, etc.) are populated
by the framework — see [Error handling](error-handling.md).

## Next

- [Error handling](error-handling.md) — custom error handlers,
  the DEV-mode traceback page.
- [Events](events.md) — `@app.on` / `@app.intercept` for
  observational and interceptive hooks.
- [Static files](static-files.md) — `StaticFiles` middleware
  configuration.
