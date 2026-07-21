# Your First App

The [Hello World](hello-world.md) used the full ASGI
`(scope, receive, send)` triplet so you could see what BlackBull
gives every handler.  Most real handlers don't need most of that.
BlackBull detects this at route-registration time and lets you
omit whatever you aren't using.

## Simplified handlers

Drop the parameters you don't use; return the response value
instead of calling `send`:

```python
from blackbull import BlackBull

app = BlackBull()

@app.route(path='/')
async def hello():
    return "Hello, world!"
```

BlackBull wraps the return value into a response automatically.
No `send` call, no `Response` object, no `scope` boilerplate.

## Path parameters

Declare parameters whose names match `{name}` segments in the path;
BlackBull injects the captured value:

```python
@app.route(path='/tasks/{task_id}')
async def get_task(task_id):       # str by default
    return f"Task {task_id}"
```

Add a type annotation and BlackBull will coerce the captured
string for you:

```python
@app.route(path='/tasks/{task_id}')
async def get_task(task_id: int):  # str captured, int() applied
    return {"id": task_id}
```

For URL patterns that should *only* match a specific type (so
`/tasks/abc` returns 404 instead of being routed in and then
failing to convert), use a path converter:

```python
@app.route(path='/tasks/{task_id:int}')
async def get_task(task_id: int):  # router enforces int → no 500s on bad input
    return {"id": task_id}
```

## Request body

Name a parameter `body` to receive the complete request body as
`bytes`.  BlackBull reads all chunks before calling the handler:

```python
import json
from http import HTTPMethod

@app.route(path='/echo', methods=[HTTPMethod.POST])
async def echo(body: bytes):
    data = json.loads(body)
    return data          # dict → JSONResponse automatically
```

## The `Connection` object

Annotate a parameter with `Connection` (or just name it `conn`/`request`)
to receive a context object bundling the common reads — headers,
cookies, client address, and the body helpers:

```python
from blackbull import Connection

@app.route(path='/items/{item_id:int}', methods=[HTTPMethod.POST])
async def update_item(item_id: int, conn: Connection):
    lang = conn.headers.get(b'accept-language', b'en').decode()
    data = await conn.json()
    return {"id": item_id, "lang": lang, "data": data}
```

!!! note "`Request` is the old name"
    Before v0.59.2 this object was called `Request`.  `blackbull.Request`
    still works as a deprecated alias (removal no earlier than
    2027-08-01); new code should use `Connection`.

See [Requests and responses](../guide/requests-and-responses.md)
for the full surface.

## The `scope` dict

Name a parameter `scope` to receive the full scope dict alongside
other simplified parameters:

```python
@app.route(path='/items/{item_id:int}')
async def get_item(item_id: int, scope):
    lang = scope['headers'].get(b'accept-language', b'en').decode()
    return {"id": item_id, "lang": lang}
```

## Return value mapping

What you return determines what BlackBull sends:

| Return type | Response sent | Content-Type |
|---|---|---|
| `str` | `Response(value.encode())` | `text/html; charset=utf-8` |
| `bytes` | `Response(value)` | `text/html; charset=utf-8` |
| `dict` or `list` | `JSONResponse(value)` | `application/json` |
| `Response` / `JSONResponse` | Passed through as-is | (whatever the response sets) |
| `None` | Nothing sent | Handler called `send` directly, or intentionally empty |

For finer control — custom status codes, extra headers, streaming
bodies — return a `Response` / `JSONResponse` / `StreamingResponse`
explicitly.  See the [Guide](../guide/index.md) for details.

## A small worked example

A tiny in-memory task tracker:

```python
from http import HTTPMethod, HTTPStatus
from blackbull import BlackBull, Response

app = BlackBull()
_tasks: dict[int, dict] = {}
_next_id = 1


@app.route(path='/tasks', methods=[HTTPMethod.GET])
async def list_tasks():
    return list(_tasks.values())                # list → JSONResponse


@app.route(path='/tasks', methods=[HTTPMethod.POST])
async def create_task(body: bytes):
    global _next_id
    import json
    payload = json.loads(body)
    task = {'id': _next_id, 'title': payload['title'], 'done': False}
    _tasks[_next_id] = task
    _next_id += 1
    return task                                  # dict → JSONResponse


@app.route(path='/tasks/{task_id:int}', methods=[HTTPMethod.GET])
async def get_task(task_id: int):
    task = _tasks.get(task_id)
    if task is None:
        return Response(b'not found', status=HTTPStatus.NOT_FOUND,
                        content_type='text/plain')
    return task


if __name__ == '__main__':
    app.run(port=8000)
```

Run it:

```bash
$ python tasks.py
$ curl -X POST -d '{"title":"buy milk"}' localhost:8000/tasks
{"id": 1, "title": "buy milk", "done": false}

$ curl localhost:8000/tasks/1
{"id": 1, "title": "buy milk", "done": false}

$ curl localhost:8000/tasks
[{"id": 1, "title": "buy milk", "done": false}]
```

## When to use the full triplet

The simplified form covers most needs, but not all:

- **WebSocket handlers** always receive the full
  `(scope, receive, send)` triplet.
- **Middleware** functions keep the `(scope, receive, send, call_next)`
  shape; the simplified adaptation does not apply to them.
- **Streaming uploads / long polling** need to call `receive()` in
  a loop, so the full form is clearer.

The [Guide](../guide/index.md) covers each of these in turn.

## Next

- [Guide](../guide/index.md) — routing, middleware, WebSockets,
  HTTP/2, static files, error handling, configuration, logging,
  and more.
