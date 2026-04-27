# CLAUDE.md — BlackBull

## Project overview

BlackBull is a Python ASGI 3.0 web framework built from scratch.
It handles HTTP/1.1, HTTP/2, and WebSocket at the protocol level (no uvicorn/starlette underneath).
The codebase is a personal learning project, so correctness over the wire matters more than API stability.

---

## Package layout

```
blackbull/
  app.py                # BlackBull app class: routing, lifespan, error handlers
  router.py             # Route/group registration and matching
  response.py           # Response, JSONResponse, StreamingResponse, WebSocketResponse, cookie_header
  request.py            # read_body, parse_cookies
  utils.py              # Scheme enum, EventEmitter (experimental), helpers
  logger.py             # @log decorator (auto-detects caller module via inspect.stack)
  env.py                # BLACKBULL_ENV environment gate (development/test/production)
  middleware/
    __init__.py         # exports: compress, websocket, StreamingAwareMiddleware
    base.py             # StreamingAwareMiddleware ABC for streaming-safe class middleware
    compression.py      # CompressionMiddleware (gzip/br/zstd)
    static.py           # StaticFiles global middleware (Range, path-traversal guard)
    websocket.py        # websocket middleware (connect/accept boilerplate)
  server/
    server.py           # ASGIServer, HTTP1/2/WebSocketHandler, LifespanManager
    sender.py           # HTTP1Sender, HTTP2Sender, WebSocketSender + AbstractWriter
    recipient.py        # HTTP1Recipient, HTTP2Recipient, WebSocketRecipient + AbstractReader
    headers.py          # Headers class (multi-value, case-insensitive, bytes keys)
    parser.py           # HTTP/1.1 request line + header parser
    response.py         # ResponderFactory (maps frame types to response handlers)
    watch.py            # Watcher (inotify-based hot-reload, Linux only)
  protocol/
    frame.py            # HTTP/2 frame encoding/decoding (HPACK via h2 library)
    stream.py           # HTTP/2 stream state machine
    rsock.py            # Dual-stack socket helpers
  client/               # Experimental async HTTP/1.1 and HTTP/2 client
examples/
  helloworld.py           # Minimal HTTP/1.1 server
  helloworld-simple.py    # Simplified handler signatures demo
  SimpleTaskManager/      # REST API + HTML UI, SQLite, Bearer auth
  ChatServer/             # WebSocket in three styles
  LoggingExample/         # Access log → SQLite via QueueHandler + QueueListener
  PriorityExample/        # HTTP/2 PRIORITY_UPDATE demo
docs/
  index.md              # MkDocs landing page
  guide.md              # Full developer guide (17 sections)
  gen_ref_pages.py      # MkDocs-gen-files script: auto-generates API reference pages
  stylesheets/
    extra.css           # Custom "experimental" admonition (amber flask icon)
tests/                  # pytest test suite
mkdocs.yml              # MkDocs configuration (mkdocs-material theme)
.github/workflows/
  docs.yml              # GitHub Actions: deploys docs to GitHub Pages on every push to master
```

## Running

```bash
pip install -e .                        # editable install (core deps)
pip install -e '.[compression]'         # add brotli + zstandard
pip install -e '.[testing]'             # add pytest + pytest-asyncio
pip install -e '.[docs]'                # add mkdocs-material + mkdocstrings

python examples/helloworld.py           # HTTP/1.1 on port 8000
python examples/helloworld-simple.py   # simplified handler form, port 8000
python app.py --port 8443 --cert cert.pem --key key.pem   # HTTPS + HTTP/2
```

## Simplified handler signatures

Route handlers may omit `scope`, `receive`, and `send`. The router detects this
at registration time and wraps the function automatically:

```python
@app.route(path='/')
async def hello():
    return "Hello, world!"         # str → Response

@app.route(path='/tasks/{task_id}')
async def get_task(task_id: int):  # path param coerced to int
    return {"id": task_id}         # dict → JSONResponse

@app.route(path='/echo', methods=[HTTPMethod.POST])
async def echo(body: bytes):       # full body buffered automatically
    return body
```

Supported parameters: named path params (coerced to annotation type), `body: bytes`,
`scope`. Return `str`, `bytes`, `dict`, `Response`, or `None`. Middleware functions
and WebSocket handlers always use the full `(scope, receive, send)` form.

## Middleware convention

```python
async def my_mw(scope, receive, send, call_next):
    # pre-handler work
    await call_next(scope, receive, send)
    # post-handler work
```

- `call_next` is bound by `_register_chain` via `functools.partial`
- The legacy name `inner` is accepted as an alias
- Short-circuit by returning without calling `call_next`
- Class-based middleware that wraps `send` must inherit `StreamingAwareMiddleware`
  (or a `UserWarning` is emitted at registration)

## Logging

```python
from blackbull.logger import log

@log
async def my_fn(x, y):   # logs call at DEBUG level using the caller module's logger
    ...
```

`@log` takes no arguments. It calls `inspect.stack()[1]` at decoration time to
find the enclosing module's `__name__` and binds to that logger automatically.

Framework internals use two separate logger hierarchies:
- `blackbull.*` — DEBUG-level protocol/routing/TLS events
- `blackbull.access` — INFO-level access log (one record per completed request)

## ASGI event types used

| Direction | Event type | Notes |
|---|---|---|
| receive | `http.request` | `body`, `more_body` |
| receive | `http.disconnect` | client closed |
| receive | `websocket.connect` | first receive() call |
| receive | `websocket.receive` | `text` or `bytes` |
| receive | `websocket.disconnect` | `code` |
| send | `http.response.start` | `status`, `headers` |
| send | `http.response.body` | `body`, `more_body` |
| send | `http.response.trailers` | `headers` (chunked trailer) |
| send | `http.response.push` | HTTP/2 server push (path, headers) |
| send | `websocket.accept` | |
| send | `websocket.send` | `text` or `bytes` |
| send | `websocket.close` | |
