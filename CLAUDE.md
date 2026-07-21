# CLAUDE.md — BlackBull

## Project overview

BlackBull is a Python ASGI 3.0 web framework with pure-Python
HTTP/1.1, HTTP/2, and WebSocket implementations at the protocol level.
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

@app.route(path='/tasks/{task_id:int}')
async def get_task(task_id: int):  # path param coerced to int
    return {"id": task_id}         # dict → JSONResponse

@app.route(path='/echo', methods=[HTTPMethod.POST])
async def echo(body: bytes):       # full body buffered automatically
    return body

@app.route(path='/inspect', methods=[HTTPMethod.POST])
async def inspect(request: Request):   # opt-in context object
    return {'ua': request.headers.get(b'user-agent').decode(),
            'data': await request.json()}

@app.route(path='/search')
async def search(q: str, page: int = 1, db=Depends(get_db)):
    ...   # q/page from the query string; db from the provider, torn down after send
```

Supported parameters: named path params (coerced to annotation type), `body: bytes`,
`scope`, `Request` (annotation `Request` under any name, or the bare name
`request` unannotated) — `request.body()`/`json()`/`text()` cache one drain of
`receive`, shared with a coexisting `body` param — `Depends(provider)` as a
default value (per-request provider injection, Sprint 74), and **query params**
as the fallback category: any leftover scalar param (`str`/`int`/`float`/`bool`,
optionally `| None`; unannotated → `str`) resolves from the query string, with
defaults → optional and missing-required/failed-coercion → 400. Classification
happens once at registration (`_handler_param_plan`, router.py); handlers using
neither new feature keep the pre-Sprint-74 wrapper (zero-overhead pin). Return
`str`, `bytes`, `dict`, `Response`, or `None`. Middleware functions and
WebSocket handlers always use the full `(scope, receive, send)` form.

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
async def auth(event):               # every handler receives the Event
    if not valid(event.detail['scope']):
        raise PermissionError('denied')
```

The four request-lifecycle events fire exactly once per request under any
transport (own server, uvicorn, TestClient): `request_received`,
`before_handler`, and `after_handler` are emitted from `BlackBull._dispatch`
(Sprint 64); `request_completed` from `BlackBull.__call__` after the global
middleware chain returns, so its wire fields reflect what a buffering
`app.use` middleware (e.g. `Compression`) actually sent (issue #145).

## Logging

```python
from blackbull.logger import log

@log
async def my_fn(x, y):   # logs call at DEBUG level using the caller module's logger
    ...
```

`@log` checks the module logger level **at decoration time** (import).  When
DEBUG is not enabled, the decorator is a zero-cost no-op — the original
function is returned unwrapped.  Raising the log level to DEBUG after import
will not activate logging for already-decorated functions.

Framework internals use two separate logger hierarchies:
- `blackbull.*` — DEBUG-level protocol/routing/TLS events
- `blackbull.access` — INFO-level access log (one record per completed request)

---

## Working docs map (`.claude/` + `CLAUDE_DEV.md`)

This file is the only doc auto-loaded every session. The docs below are **not**
loaded automatically — open the relevant one when the trigger applies. Do not
duplicate their content here; link, don't copy.

| When you are… | Read |
|---|---|
| Doing any framework change (workflow, testing, type rules) | `CLAUDE_DEV.md` |
| Writing/adjusting tests | `.claude/patterns/testing.md` |
| Running benchmarks or profiling | `.claude/patterns/benchmarking.md` + the `bench-compare` skill |
| Cutting a release / sprint close | `.claude/patterns/release.md`, then the `sprint-close` skill |
| Reasoning about actors / events | `.claude/design/actor-model.md`, `.claude/design/event-catalogue.md` |
| Checking a known gotcha before acting | `.claude/patterns/cautions.md` |
| Picking/triaging what to build next | `.claude/planning/proposals/INDEX.md` |
| Reading a point-in-time design | `.claude/planning/designs/` |

**Skills** (invocable, harness-surfaced): `sprint-close`, `bench-compare`,
`pre-release-docs`, `update-roadmap`, `create-test`, `type-check`, `add-event`,
`new-http2-frame`, `protocol-handler`, `httparena-bench`, `run-http11probe`.

### Doc lifecycle (so docs don't rot)

Every doc under `.claude/planning/` carries a status line near the top:
`**Status**: active | shipped vX.Y.0 | superseded-by <file> | archived <date>`.
When a proposal/design ships or dies, move it to `.claude/planning/archives/`
(that pruning is a step in the `sprint-close` skill). `.claude/` is git-ignored,
so deletions are **not** recoverable — prune deliberately, archive when unsure.

