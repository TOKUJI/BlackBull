# Routing

`@app.route(...)` registers a handler under a method + path combination.
This page covers the route registration surface: HTTP routes, path
parameters (string and regex), typed routes with converters, route
groups, and a brief pointer to WebSocket routes.

For handler signatures (full ASGI triplet vs. simplified form) see
[Your First App](../getting-started/first-app.md).

## HTTP routes

```python
from http import HTTPMethod
from blackbull import BlackBull, Response

app = BlackBull()

@app.route(methods=[HTTPMethod.GET], path='/tasks')
async def list_tasks(scope, receive, send):
    await send(Response(b'[]'))
```

`methods` defaults to `[HTTPMethod.GET]`.  Pass a list to accept
multiple methods on the same handler:

```python
@app.route(methods=[HTTPMethod.GET, HTTPMethod.HEAD], path='/healthz')
async def healthz(scope, receive, send):
    await send(Response(b'ok'))
```

Any valid method token is accepted, as a string or an `HTTPMethod`
member — extension methods (`PROPFIND`, vendor tokens) route the same
way as the standard ones, and appear in the 405 `Allow` header when
another method hits the path.

### The QUERY method (RFC 10008)

QUERY — the first new standard HTTP method since PATCH — is a **safe,
idempotent, cacheable** request that carries a **request body**, closing
the "GET with a body" gap for complex read-only queries.  The stdlib
`http.HTTPMethod` enum has no `QUERY` member until Python 3.16, so
BlackBull exports the method as a plain-string constant:

```python
from blackbull import BlackBull, QUERY

app = BlackBull()

@app.route(path='/search', methods=[QUERY])
async def search(body: bytes):
    return run_query(body)          # the whole query rides in the body
```

Body access works exactly as for POST — `body: bytes`, `Connection.json()`,
or draining `receive` yourself.  By registering a QUERY handler you sign
up for the method's contract: the handler **must not** have side effects
the client could regret repeating (safe), and repeating the same request
must give the same result (idempotent) — caches and retrying clients
rely on it.

#### Declaring accepted media types

RFC 10008 lets a QUERY route advertise the request media types it
understands and enforce them.  Pass `accept_query=[...]` to `route()`:

```python
from blackbull import BlackBull, QUERY, UnprocessableQuery

@app.route(path='/search', methods=[QUERY],
           accept_query=['application/sql', 'text/plain'])
async def search(body: bytes):
    try:
        plan = compile_query(body)
    except UnknownField as e:
        raise UnprocessableQuery(str(e))   # → 422
    return run(plan)
```

With `accept_query` set, BlackBull:

- emits an **`Accept-Query`** response header — an
  [RFC 9651 Structured Field](structured-fields.md) list of those media
  types (`application/sql, text/plain`) — on the route's responses, so a
  client can discover what to send;
- **enforces the request `Content-Type`** on QUERY requests: a missing
  media type is answered **400**, an unaccepted one **415** (the 415 also
  carries `Accept-Query` so the client can correct).  The media-type
  match ignores parameters (`; charset=…`) and is case-insensitive.

Raise **`UnprocessableQuery`** from the handler for **422** when the media
type was accepted but the query itself is semantically invalid (an unknown
field, a violated constraint). All three statuses flow through the normal
[error-handling](error-handling.md) path, so custom error handlers apply.
Enforcement targets the QUERY method; other methods registered on the same
route still receive the `Accept-Query` header but are not Content-Type-gated.

Two more RFC 10008 notes:

- The response-cache rules (the cache key must incorporate the request
  content) bind *caches*, not origin servers — BlackBull ships no
  response cache, so nothing to configure.
- OpenAPI 3.1 has no `query` operation, so QUERY routes are not emitted
  in the [generated spec](openapi.md) (they are never faked as another
  operation).

## Path parameters

Use `{name}` segments in the path string.  Captured values are
available in `scope['path_params']` (and, in the simplified form,
injected as named arguments):

```python
@app.route(path='/tasks/{task_id}')
async def get_task(scope, receive, send):
    task_id = scope['path_params']['task_id']   # str (default converter)
    await send(Response(task_id.encode()))
```

`{name}` (no converter) matches `[^/]+` and injects a `str`.  Append
`:converter` to control both the regex and the injected Python type —
see [Typed routes](#typed-routes) below.

### Regex patterns

For fully custom patterns supply a compiled regex with named groups;
the captured values are injected into `scope['path_params']` — the
same place as `{name}` parameters:

```python
import re

@app.route(path=re.compile(r'^/items/(?P<id>\d+)$'))
async def get_item(scope, receive, send):
    item_id = scope['path_params']['id']
    await send(Response(item_id.encode()))
```

## Typed routes

Append `:converter` to a path parameter to control both the URL
pattern and the Python type injected into the handler:

| Syntax | Regex matched | Python type |
|---|---|---|
| `{name}` or `{name:str}` | `[^/]+` | `str` |
| `{id:int}` | `-?[0-9]+` | `int` |
| `{uid:uuid}` | UUID hex pattern | `uuid.UUID` |
| `{rest:path}` | `.+` (matches `/`) | `str` |

```python
import uuid
from http import HTTPMethod

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

Using `{id:int}` means a request to `/items/abc` returns 404 (the
router doesn't match) rather than being routed in and then failing
to convert.

### URL reverse lookup

Register a route with a `name=` keyword, then build its path from
parameters:

```python
@app.route(path='/items/{id:int}', methods=HTTPMethod.GET, name='item-detail')
async def get_item(id: int):
    return {'id': id}

app.url_path_for('item-detail', id=42)   # → '/items/42'
```

`url_path_for` raises `KeyError` for unknown names and `ValueError`
when required parameters are missing.

### Startup validation

`app.run()` and the ASGI lifespan `startup` event both call
`Router.validate()` before accepting connections.  Validation
checks:

- Every `{param:converter}` uses a known converter name.
- Every path parameter appears in the handler's signature.
- The converter's output type matches the handler's annotation
  (e.g. `{id:int}` with `id: str` is flagged as an error).

On failure, a `ConfigurationError` is raised (or sent as
`lifespan.startup.failed`) listing every violated route.  On
success, the router is **frozen** — further route registration
raises `RuntimeError`.

A passing example:

```python
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

if __name__ == '__main__':
    print(app.url_path_for('greet', name='Alice'))   # /greet/Alice
    app.run(port=8000)
```

A failing example:

```python
@app.route(path='/double/{n:int}', methods=HTTPMethod.GET)
async def double(n: str):      # converter is int, annotation is str
    return f'double of {n}'
```

Output before the server binds:

```
ConfigurationError: Route '/double/{n:int}' param 'n': converter 'int' yields 'int'
but annotation is <class 'str'>: int is not an instance of str
```

## Route groups

`app.group(middlewares=[...])` returns a `RouteGroup` whose
`.route()` method prepends the group's middlewares to every route
registered through it.

```python
public    = app.group(middlewares=[error_mw, logging_mw])
protected = app.group(middlewares=[error_mw, logging_mw, auth_mw])

@public.route(methods=[HTTPMethod.GET], path='/')
async def index(scope, receive, send):
    await send(Response(b'<h1>Login</h1>'))

@protected.route(methods=[HTTPMethod.GET], path='/tasks')
async def get_tasks(scope, receive, send):
    return []                               # → JSONResponse
```

Per-route `middlewares=[...]` are **appended after** the group
middlewares:

```python
# Effective chain: error_mw → logging_mw → auth_mw → json_body_mw → create_task
@protected.route(methods=[HTTPMethod.POST], path='/tasks',
                 middlewares=[json_body_mw])
async def create_task(scope, receive, send):
    ...
```

For middleware ordering semantics see [Middleware](middleware.md).

## WebSocket routes

```python
from blackbull.utils import Scheme

@app.route(path='/ws', scheme=Scheme.websocket)
async def ws_handler(scope, receive, send):
    ...
```

WebSocket handlers always receive the full
`(scope, receive, send)` triplet — the simplified form does not
apply.  See [WebSockets](websockets.md) for the handshake,
subprotocol negotiation, fragmented messages, `permessage-deflate`,
and the RFC 8441 (HTTP/2) transport.

## Lookup cache

Route resolution is backed by a per-worker LRU cache keyed on
`(path, method, scheme)`, so repeated requests to the same target skip
the trie traversal after the first hit.  The cache is cleared
automatically whenever a route is registered, so it never serves a stale
result during startup.

The bound is a constructor argument on the router:

```python
from blackbull import BlackBull

app = BlackBull(cache_max=4096)   # larger cache for high path cardinality
app = BlackBull(cache_max=0)      # disable the lookup cache entirely
```

The default is `2048` entries.  Raising it helps when the application
serves a very large number of distinct paths (the cache is a hit only
when the exact `(path, method, scheme)` recurs); `0` disables caching so
every request re-resolves through the trie.  Most applications never need
to touch this.

## Next

- [Middleware](middleware.md) — attaching per-route middleware,
  built-in middleware, common recipes.
- [Error handling](error-handling.md) — 404 / 405 / 500 defaults,
  `@app.on_error` for custom handlers.
- [Requests and responses](requests-and-responses.md) — reading
  the request body, streaming responses, detecting client
  disconnection.
- [WebSockets](websockets.md) — handshake, subprotocols,
  fragmented messages, RFC 8441.
