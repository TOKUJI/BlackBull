# CLAUDE.md — BlackBull

## Project overview

BlackBull is a Python ASGI 3.0 web framework built from scratch.
It handles HTTP/1.1, HTTP/2, and WebSocket at the protocol level.
The codebase is a personal learning project, so correctness over the wire matters more than API stability.

---

## Package layout

```
blackbull/              # The package's root directory
  middleware/
  server/               # ASGIServer and HTTP1/2/WebSocketHandler
  protocol/             # HTTP/2 and other protocols
  client/               # Experimental async HTTP/1.1 and HTTP/2 client
examples/               # Example applications. See also docs/guide.md
docs/                   # Docs for users
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
- Short-circuit by returning without calling `call_next`

## Event API

`@app.on` / `@app.intercept` are now core to the framework.

```python
@app.on('request_received')          # fire-and-forget; exceptions are isolated
async def log_it(event): ...

@app.intercept('before_handler')     # synchronous; exceptions propagate to emitter
async def auth(scope, receive, send, call_next):
    ...
    await call_next(scope, receive, send)
```

## Logging

```python
from blackbull.logger import log

@log
async def my_fn(x, y):   # logs call at DEBUG level using the caller module's logger
    ...
```

Framework internals use two separate logger hierarchies:
- `blackbull.*` — DEBUG-level protocol/routing/TLS events
- `blackbull.access` — INFO-level access log (one record per completed request)

