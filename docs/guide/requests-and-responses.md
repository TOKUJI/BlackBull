# Requests and responses

How to read what the client sent, build the response, stream a body
back, and detect when the client has gone away.

## The `Request` object

The easiest way to read the request is the opt-in `Request` context
object.  Declare a parameter annotated `Request` (any name) — or a
parameter named `request` with no annotation — and the router injects
one, the same way it injects path params and `body`:

```python
from blackbull import BlackBull, Request

app = BlackBull()

@app.route(path='/users/{uid}', methods=[HTTPMethod.POST])
async def show(uid: int, request: Request):
    token = request.headers.get(b'authorization')   # Headers view
    who   = request.client                          # (host, port) or None
    lang  = request.cookies.get('lang', 'en')       # dict[str, str]
    data  = await request.json()                    # parsed once, cached
    return {'uid': uid, 'lang': lang, 'data': data}
```

Read-side surface:

| member | value |
|---|---|
| `request.method` / `request.path` / `request.scheme` | `str` (`path` is **percent-decoded** — see below) |
| `request.client` | `(host, port)` tuple, or `None` |
| `request.headers` | case-insensitive [`Headers`](#reading-request-headers) view |
| `request.cookies` | `dict[str, str]`, parsed once (all protocols) |
| `await request.body()` | complete body as `bytes`, buffered once and cached |
| `await request.json()` | parsed JSON, or `None` on empty/invalid body |
| `await request.text(encoding='utf-8')` | body decoded as text (`errors='replace'`) |
| `request.scope` | the raw ASGI scope dict (escape hatch) |

`body()` drains the receive channel at most once; `json()` and
`text()` share the same cache, and a handler that also declares
`body: bytes` (or a dataclass body parameter) receives the same
cached bytes — the body is never read twice.  Handlers that don't
declare a `Request` pay nothing: injection is decided when the route
is registered, and the raw `(scope, receive, send)` form is
unaffected.

!!! note "`path` is percent-decoded (since v0.53.0)"
    `request.path` (and the underlying `scope['path']`) is the
    **percent-decoded** request target with the query string removed —
    `/files/a%2Fb` arrives as `/files/a/b`, matching the ASGI spec and
    uvicorn.  The **un**decoded bytes are available as
    `scope['raw_path']` (`bytes`, query excluded) for the rare consumer
    — reverse proxies, WAFs, cache-key / signature verification — that
    must reproduce the exact received byte sequence.  RFC 3986 `;`
    parameters are preserved in both (`/cart;sid=abc` stays intact).
    Before v0.53.0 `path` was not decoded; code that re-decoded it
    itself should drop that step.

The sections below cover the same reads on the raw ASGI surface —
useful inside middleware (which always uses the full form) and for
streaming bodies chunk by chunk.

## Reading the request body

```python
from blackbull import read_body

@app.route(path='/echo', methods=[HTTPMethod.POST])
async def echo(scope, receive, send):
    raw: bytes = await read_body(receive)
    await send(Response(raw))
```

`read_body` reads all body chunks until `more_body=False` and
returns a single `bytes` object.  The stream is consumed — call
at most once per request (typically inside a middleware, not the
handler itself).

For streaming uploads, call `receive()` directly — each call
returns one chunk with `more_body=True` until the final chunk
arrives with `more_body=False`.  `read_body` is a convenience
wrapper that buffers all chunks before returning.

### `read_json` and `read_text`

For the two most common body shapes, `read_json` and `read_text`
wrap `read_body` so the handler skips the parse/decode boilerplate:

```python
from http import HTTPStatus
from blackbull import read_json, read_text, JSONResponse

@app.route(path='/api/things', methods=[HTTPMethod.POST])
async def create_thing(scope, receive, send):
    data = await read_json(receive)        # dict | list | … | None
    if data is None:
        await send(JSONResponse({'error': 'invalid JSON'},
                                status=HTTPStatus.BAD_REQUEST))
        return
    await send(JSONResponse({'created': data}))

@app.route(path='/api/note', methods=[HTTPMethod.POST])
async def note(scope, receive, send):
    text = await read_text(receive)        # str (errors='replace')
    await send(Response(text))
```

`read_json` returns `None` when the body is empty, not valid JSON, or
not decodable — treat `None` as a client error.  `read_text` never
raises on malformed bytes (undecodable bytes become U+FFFD); pass
`encoding=` for non-UTF-8 payloads.  Both consume the stream, so call
at most once per request, exactly like `read_body`.

### Recommended JSON-body middleware

```python
import json
from http import HTTPStatus
from blackbull import read_body, JSONResponse

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

## Reading request headers

`scope['headers']` is a `Headers` object — case-insensitive, ordered,
multi-valued.

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

Header names are stored lowercase.

### `get` vs `getlist`

Use `.get(name)` for headers that appear at most once
(`content-type`, `authorization`, `host`).  Use `.getlist(name)`
for headers that may repeat:

| Header | Why it can repeat |
|---|---|
| `accept`, `accept-encoding` | Clients may send multiple preference lines |
| `set-cookie` | Servers send one `Set-Cookie` field per cookie |
| `cookie` (HTTP/2) | RFC 7540 §8.1.2.5 requires one field per cookie pair |

`.getlist` returns `list[tuple[bytes, bytes]]` — the full
`(name, value)` pairs in insertion order, or `[]` if the header
is absent.

!!! note "Why `cookie` needs `getlist` on HTTP/2"
    HTTP/1.1 combines all cookies into a single
    `Cookie: a=1; b=2` field.  HTTP/2 sends each cookie as a
    separate header field to enable HPACK compression of
    individual values.  Calling `.get(b'cookie')` on an HTTP/2
    scope silently discards all but the first cookie.
    `parse_cookies` (below) handles this correctly for all
    protocols.

## Reading cookies

```python
from blackbull import parse_cookies

cookies: dict[str, str] = parse_cookies(scope)
session = cookies.get('session', '')
```

`parse_cookies(scope)` returns a `dict[str, str]` of cookie name
→ value from the current request.  The result is identical
across HTTP/1.1, HTTP/2, and WebSocket scopes — you don't need
to know how the client delivered the cookies.

## Query parameters

Declare them as handler parameters (since v0.56.0).  Any simplified-handler
parameter that is not a path param, `body`, `scope`, `Request`, a dataclass
body, or [`Depends`](dependency-injection.md) resolves from the query
string, coerced to its annotation:

```python
@app.route(path='/search')
async def search(q: str, page: int = 1, exact: bool = False):
    ...   # /search?q=bull&page=2 → q='bull', page=2, exact=False
```

- **Types**: `str` (default for unannotated params), `int`, `float`, and
  `bool` (`1/true/yes/on` and `0/false/no/off`, case-insensitive).
  `T | None` of a supported scalar also works.  Anything else — containers,
  models — is a registration-time `TypeError`; drop to `Request` or the raw
  scope for those.
- **Required vs optional**: a parameter with a default is optional; without
  one, a request missing the key is answered with **400**.  A value that
  fails coercion (`?page=abc` for `page: int`) is also a 400 — client
  errors never surface as 500s.
- **Repeated keys** (`?tag=a&tag=b`): the last occurrence wins.  For
  list-valued keys, parse the raw query string as shown below.
- **Precedence**: a parameter that matches a `{placeholder}` in the path is
  always a path param — the path value shadows any same-named query key
  (declaring a default on a path param draws a registration-time warning,
  since that usually means a query param was intended).
- **OpenAPI**: query params appear in the generated spec (`in: query`,
  schema from the annotation, `required` from default-presence) — see
  [OpenAPI](openapi.md).

Coercion and required-ness are resolved when the route is registered, not
per request — handlers that declare no query params keep the exact adapted
form they had before.

### Raw query strings

For repeated keys, unusual encodings, or full control,
`scope['query_string']` contains the raw query string as bytes.
Parse it with the standard library:

```python
from urllib.parse import parse_qs, parse_qsl

# parse_qs: each key maps to a list of values (handles ?tag=a&tag=b correctly)
params = parse_qs(scope['query_string'].decode())
page   = int(params.get('page', ['1'])[0])
tags   = params.get('tag', [])             # ['a', 'b'] for ?tag=a&tag=b

# parse_qsl: flat list of (key, value) pairs preserving order
pairs = parse_qsl(scope['query_string'].decode())
```

For convenience, wrap this in a helper:

```python
def qp(scope) -> dict[str, str]:
    """Return first value for each query parameter key."""
    return {k: v[0]
            for k, v in parse_qs(scope['query_string'].decode()).items()}

@app.route(path='/tasks')
async def list_tasks(scope, receive, send):
    p = qp(scope)
    done = p.get('done', 'false').lower() == 'true'
    await send(JSONResponse({'done_filter': done}))
```

## Form data

HTML forms with `enctype="application/x-www-form-urlencoded"` (the
default) send key=value pairs in the body.  Read and parse with
`read_body` + `parse_qs`:

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

Multipart file uploads (`multipart/form-data`) are not yet
supported by a built-in helper.  Use the `python-multipart`
package to parse the body manually.

## Responses

### `Response`

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

Default `content_type` is `'text/html; charset=utf-8'`.  Override
via the `content_type` parameter:

```python
Response(b'plain text', content_type='text/plain; charset=utf-8')
```

### `JSONResponse`

```python
from blackbull import JSONResponse

await send(JSONResponse({'ok': True}))
await send(JSONResponse({'error': 'Bad request'}, status=HTTPStatus.BAD_REQUEST))
await send(JSONResponse({'id': 1, 'title': 'Buy milk'}, status=HTTPStatus.CREATED))
```

`Content-Type` is set to `application/json` automatically.

### `RedirectResponse`

```python
from blackbull import RedirectResponse

await send(RedirectResponse('/new-url'))                                    # 302 Found
await send(RedirectResponse('/permanent', status=HTTPStatus.MOVED_PERMANENTLY))  # 301
return RedirectResponse('/login', status=HTTPStatus.SEE_OTHER)             # 303
```

Sets the `Location` header from the URL and an empty body. The default
status is `302 Found` — the safer general-purpose default, since it does
not ask the client to preserve the original request method. The URL must be
ASCII (RFC 9110 §10.2.2 — `Location` is a URI-reference); percent-encode
non-ASCII URLs before passing them in. Extra `headers=` are merged in.

### Custom response headers

Both `Response` and `JSONResponse` accept `headers=[(bytes, bytes), ...]`:

```python
await send(JSONResponse({'ok': True}, headers=[
    (b'x-request-id', b'abc123'),
    (b'cache-control', b'no-store'),
]))
```

### `Set-Cookie` helper

```python
from blackbull import cookie_header

hdr = cookie_header('session', token, http_only=True)
# → (b'set-cookie', b'session=TOKEN; Path=/; HttpOnly; SameSite=Lax')

await send(JSONResponse({'ok': True}, headers=[hdr]))
```

Signature: `cookie_header(name, value, path='/', http_only=True)`.

!!! note "Cookies vs. tokens for SPA clients"
    Browsers may not reliably forward `HttpOnly` cookies set by a
    `fetch()` response on the next page navigation.  For
    single-page apps, store the session token in `sessionStorage`
    and send it as `Authorization: Bearer <token>` instead.

## HTTP trailers

HTTP/1.1 chunked responses can carry trailing headers after the
body.  Use the `http.response.trailers` event after the last
`http.response.body` chunk:

```python
@app.route(path='/chunked')
async def chunked(scope, receive, send):
    await send({
        'type': 'http.response.start',
        'status': 200,
        'headers': [
            (b'content-type',      b'text/plain'),
            (b'transfer-encoding', b'chunked'),
            (b'trailer',           b'x-checksum'),
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

## WebSocket frames

For WebSocket routes (`scheme=Scheme.websocket`), use
`WebSocketResponse` to dispatch on payload type automatically:

```python
from blackbull import WebSocketResponse

await send(WebSocketResponse('hello'))           # str  → text frame
await send(WebSocketResponse(b'\x00\x01'))       # bytes → binary frame
await send(WebSocketResponse({'type': 'msg'}))   # other → JSON-serialised text frame
```

See [WebSockets](websockets.md) for the full WebSocket surface.

## Streaming responses

Use `StreamingResponse` to push an async generator to the client
without buffering the whole body in memory.  The class calls
`send` directly, so it works as a nested ASGI app:

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

When `more_body=True` appears on the first `http.response.body`
event, the HTTP/1.1 sender adds `Transfer-Encoding: chunked` to
the response headers and formats each body event as a hex-length
chunk:

```
5\r\n
hello\r\n
5\r\n
world\r\n
0\r\n
\r\n
```

The terminal `0\r\n\r\n` is written automatically when
`more_body=False` arrives (unless `trailers=True` was set in
`http.response.start`, in which case the
`http.response.trailers` handler writes it).

HTTP/2 is unaffected — DATA frames carry explicit length and
`END_STREAM` maps to `more_body=False`.

### Writing streaming-safe middleware

Any middleware that wraps the `send` callable and collects body
parts will silently buffer a streaming response, defeating
`more_body=True`.

For function-based middleware, the safest approach is to pass
body events through immediately rather than accumulating them:

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

For most use cases (header injection, logging) the middleware
does not touch the body at all and streaming safety is not a
concern.

## Detecting client disconnection

When the remote side closes the connection, `receive()` returns
`{'type': 'http.disconnect'}`.  This is useful for long-polling
and server-sent events (SSE) — your handler can check whether
the client is still there before writing more.

```python
@app.route(path='/events')
async def sse(scope, receive, send):
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
            'more_body': True,
        })
    await send({'type': 'http.response.body', 'body': b'', 'more_body': False})
```

For most streaming use cases, prefer `StreamingResponse` (above)
— it wraps an async generator and emits `more_body=True` on every
chunk automatically.  The raw event pattern is convenient when
each chunk needs custom shaping (e.g. SSE event framing).

Passing `Response(b'')` to `send` ends the connection immediately
with `Content-Length: 0` and cannot be followed by additional
chunks.

## Next

- [Routing](routing.md) — `@app.route`, path parameters, route groups.
- [Middleware](middleware.md) — wrapping handlers with cross-cutting concerns.
- [Static files](static-files.md) — serving file-system content
  under a URL prefix.
