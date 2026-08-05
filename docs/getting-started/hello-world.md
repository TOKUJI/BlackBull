# Hello World

The minimal BlackBull app — full ASGI 3.0 form.

```python title="myapp.py"
from blackbull import BlackBull, Response

app = BlackBull()

@app.route(path='/')
async def hello(conn, receive, send):
    await send(Response(b'Hello, world!'))

if __name__ == '__main__':
    app.run(port=8000)
```

Run it:

```bash
python myapp.py
```

Hit it:

```bash
$ curl localhost:8000/
Hello, world!
```

That's a complete server: an HTTP/1.1 listener bound on
`127.0.0.1:8000` with one route registered.  No external server
process and no separate framework package — `BlackBull` is both.

## The full triplet

Every HTTP handler receives three arguments:

| Argument | Type | Role |
|---|---|---|
| `conn` | `Connection` | The parsed request — method, path, headers, query string, … |
| `receive` | `async callable` | Reads request body events from the client |
| `send` | `async callable` | Writes the response back to the client |

`send` accepts a `Response` object (as above), a `NativeResponse`, or
raw ASGI event dicts; all three work and can be mixed in the same
handler.

!!! note "`Connection`, not a scope dict"
    BlackBull threads a typed `Connection` end to end.  It is **not**
    subscriptable — `conn['headers']` raises `TypeError`.  An ASGI
    scope dict appears only where BlackBull meets something that speaks
    ASGI: an external host such as uvicorn, `BB_FORCE_ASGI_SCOPE=1`, or
    a middleware that asks for one by naming its first parameter
    `scope` (see [Middleware](../guide/middleware.md)).

### What's on the `Connection`

| Attribute | Type | Notes |
|---|---|---|
| `conn.type` | `str` | `'http'` or `'websocket'` |
| `conn.method` | `str` | `'GET'`, `'POST'`, … |
| `conn.path` | `str` | URL path, e.g. `'/tasks/42'` |
| `conn.headers` | `Headers` | Case-insensitive multi-valued header store |
| `conn.query_string` | `bytes` | Raw query string, parse with `urllib.parse.parse_qs` |
| `conn.path_params` | `dict` | Values captured from `{name}` segments |
| `conn.state` | `dict` | Per-request scratch space, shared by every layer |
| `await conn.body()` | `bytes` | The complete request body |
| `conn.stream()` | async iterator | The body one chunk at a time |

Middleware passes values to inner layers through `conn.state` —
typical additions are `conn.state['user']` (auth result) and
`conn.state['json']` (parsed body).  Setting an attribute or a
top-level key instead does **not** reach the handler.

## What `Response` does

`Response(b'Hello, world!')` constructs a response object with a
sensible default `Content-Type` (`text/html; charset=utf-8`) and
sets `Content-Length` from the body.  Pass `content_type=` to
override:

```python
return Response(b'{"ok": true}', content_type='application/json')
```

For JSON specifically, `JSONResponse` does the `json.dumps` for you:

```python
from blackbull import JSONResponse

@app.route(path='/health')
async def health(conn, receive, send):
    await send(JSONResponse({'status': 'ok'}))
```

## When you don't need the triplet

Most handlers only use `conn`, or nothing at all.  BlackBull
detects this at registration time and lets you drop the boilerplate:

```python
@app.route(path='/')
async def hello():
    return "Hello, world!"
```

That's the simplified form — see [Your First App](first-app.md)
for the full pattern, including path params, body parameters, and
return-value type mapping.

## Next

- [Your First App](first-app.md) — simplified handlers + a small
  worked example.
