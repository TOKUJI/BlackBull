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

## Tool preferences

For code search and refactoring:

| Task | Tool | Why |
|---|---|---|
| Structural search / replace | `ast-grep` (`sg`) | Understands AST; no regex false positives |
| Plain-text grep | `rg` (ripgrep) | Fast, `.gitignore`-aware |
| File finding | `rg --files` or `rg -l` | One tool, less context switching |

- Prefer `rg` over `grep` / `find` for all text search.
- Prefer `ast-grep -p 'pattern' -l python` over `rg` when matching code
  structure (function defs, call sites, class hierarchies).
- Use `ast-grep -U` for automated structural replacements — it won't
  corrupt syntax like `sed` can.

## Simplified handler signatures

Route handlers may omit `conn`, `receive`, and `send`. The router detects this
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
`conn`/`connection` (the native `Connection`; `scope` is a deprecated alias),
`Request` (annotation `Request` under any name, or the bare name
`request` unannotated) — `request.body()`/`json()`/`text()` cache one drain of
`receive`, shared with a coexisting `body` param — `Depends(provider)` as a
default value (per-request provider injection, Sprint 74), and **query params**
as the fallback category: any leftover scalar param (`str`/`int`/`float`/`bool`,
optionally `| None`; unannotated → `str`) resolves from the query string, with
defaults → optional and missing-required/failed-coercion → 400. Classification
happens once at registration (`_handler_param_plan`, router.py); handlers using
neither new feature keep the pre-Sprint-74 wrapper (zero-overhead pin). Return
`str`, `bytes`, `dict`, `Response`, or `None`. Middleware functions and
WebSocket handlers always use the full `(scope, receive, send)` form — where,
for HTTP, `scope` is the native `Connection` (see below).

## Native `Connection` (Sprint 80 — BlackBull is no longer an ASGI framework)

BlackBull's own server threads a typed `Connection` (blackbull/connection.py)
end to end for **HTTP** — `app(conn, receive, send)`, and the middleware chain,
router, handlers, error handlers, and lifecycle-event `detail['conn']` all
receive that `Connection`. This holds for **HTTP/2 too** (Sprint 80 follow-up):
the H/2 actor threads the `Connection` directly — no per-stream ASGI scope dict,
no `from_scope` rebuild. There is no ASGI `scope` dict on the native hot path
(the `_LazyScope` bridge was removed). Read request fields as attributes:

| ASGI idiom (old) | Native `Connection` |
|---|---|
| `scope['type']` / `scope['method']` / `scope['path']` | `conn.type` / `conn.method` / `conn.path` |
| `scope['headers']` (list) | `conn.headers` (a `Headers`, `.get(b'name')`) |
| `scope['path_params']` | `conn.path_params` |
| `scope['state'][k]` (per-request grab-bag) | `conn.state[k]` |
| `scope['user'] = ...` (middleware injection) | `conn.state['user'] = ...` |
| `await read_body(receive)` | unchanged (or `await conn.body()`/`.json()`) |

The **WebSocket** path stays ASGI-scope-shaped (WS carries extras —
`subprotocols`, `_connection_id`, `_ws_*` — that are not `Connection` fields),
so a WS handler's `scope` is still a dict.

Two boundaries still convert to/from an ASGI scope dict:
- **External ASGI hosts** (uvicorn, `httpx.ASGITransport`/TestClient) call
  `app(scope, …)`; `BlackBull.__call__` does `Connection.from_scope(scope)` once
  and threads the Connection from there.
- **`BB_FORCE_ASGI_SCOPE=1`** — the compat lane. BlackBull's server emits a pure
  ASGI scope and the app round-trips it through `from_scope`. **A raw ASGI
  callable** (no `BlackBull` instance) mounted on BlackBull's server only works
  under this flag — otherwise it is handed a `Connection` and `scope['type']`
  raises. (Raw-ASGI-app support on the native path was removed.)

The send side is unchanged: handlers/middleware still `await send({...})` ASGI
response events; the sender consumes them.

## Middleware convention

```python
async def my_mw(conn, receive, send, call_next):   # `conn` is a Connection for HTTP
    # pre-handler work — read conn.headers, share via conn.state[...]
    await call_next(conn, receive, send)
    # post-handler work
```

- The first argument is named `conn` — BlackBull threads a `Connection`, not an
  ASGI scope. (The word "scope" is reserved for a genuine ASGI scope dict.)
- `call_next` is bound by `_register_chain` via `functools.partial`
- Short-circuit by returning without calling `call_next`
- Non-HTTP (WebSocket) still arrives as a scope dict — guard with
  `isinstance(conn, Connection)` if the middleware must handle both.

## Event API

`@app.on` / `@app.intercept` are now core to the framework.

```python
@app.on('request_received')          # fire-and-forget; exceptions are isolated
async def log_it(event): ...

@app.intercept('before_handler')     # synchronous; exceptions propagate to emitter
async def auth(event):               # every handler receives the Event
    if not valid(event.detail['conn']):   # the Connection (WS events: a scope dict)
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

