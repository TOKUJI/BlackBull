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
