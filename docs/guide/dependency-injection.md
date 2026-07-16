# Dependency injection

`Depends` (since v0.56.0) lets a route handler *declare* a per-request
resource — a database connection, an HTTP client, a config view — and have
the framework construct it before the handler runs and tear it down after
the response has been sent.

```python
from blackbull import BlackBull, Depends

app = BlackBull()

async def get_db():                    # a "provider": async generator
    conn = await pool.acquire()
    try:
        yield conn                     # ← injected value
    finally:
        await pool.release(conn)      # ← runs after the response is sent

@app.route(path='/items/{id:int}')
async def get_item(id: int, db=Depends(get_db)):
    return await db.fetch_item(id)
```

Use `Depends(provider)` as the parameter's **default value**.  Simplified
handlers only — middleware and full `(scope, receive, send)` handlers are
unchanged, the same rule as every other simplified-model feature.

## Provider forms

| Provider | Injected value | Cleanup |
|---|---|---|
| `async def p(): yield v` | `v` (must yield exactly once) | code after `yield` / `finally`, after the response is sent |
| `async def p(): return v` | `v` | none |
| `def p(): return v` | `v` | none |

Providers take **no parameters**.  A provider that itself declares
`Depends` (a nested dependency) is a registration-time `TypeError` —
compose inside the provider body instead (call or close over the other
provider).  Sync generator providers are rejected; use an async generator.

## Lifetimes

| Lifetime | Mechanism |
|---|---|
| per-request | `Depends(provider)` — fresh value each request |
| per-parameter | `Depends(provider, use_cache=False)` |
| per-app | `@app.on('startup')` / `AppConfig`; a provider closes over it |

By default (`use_cache=True`), two parameters of one handler naming the
*same* provider share a single instance for that request:

```python
async def audit(a=Depends(get_db), b=Depends(get_db)):
    assert a is b          # one acquire, one release
```

There is no app-scoped container: an application-lifetime singleton is an
object you create at startup and *capture* in a provider —

```python
engine: Engine | None = None

@app.on('startup')
async def boot(event):
    global engine
    engine = await create_engine(dsn)

async def get_conn():
    async with engine.connect() as conn:   # engine: app-scoped, conn: per-request
        yield conn
```

## Ordering and errors

- Providers resolve in signature order; cleanup runs LIFO (last provider
  up, first down), the `AsyncExitStack` discipline.
- Cleanup runs **after the response bytes are sent** — a client can hold
  the full response while the DB connection is already back in the pool.
  (FastAPI behaves the same way.)
- A handler exception unwinds the stack: every provider's `finally` runs,
  then the exception routes through [error handling](error-handling.md)
  as usual (500, or the registered `error_handler`).
- A provider exception before `yield` aborts the request the same way —
  the handler never runs.

## Zero cost when unused

Everything is resolved at **registration time**: a handler that declares
no `Depends` parameter compiles to exactly the wrapper it compiled to
before this feature existed — no per-request stack, no empty dependency
loop, no reflection.  (FastAPI, by contrast, runs `solve_dependencies()`
and enters two `AsyncExitStack`s on every request even with no
dependencies declared.)  Handlers that do use `Depends` pay only for what
they declared.

`Depends` parameters are not request inputs, so they are excluded from the
generated [OpenAPI](openapi.md) spec.
