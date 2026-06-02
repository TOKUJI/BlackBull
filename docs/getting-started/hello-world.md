# Hello World

The minimal BlackBull app — full ASGI 3.0 form.

```python title="myapp.py"
from blackbull import BlackBull, Response

app = BlackBull()

@app.route(path='/')
async def hello(scope, receive, send):
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
process (no `uvicorn`, no `gunicorn`) and no separate framework
package — `BlackBull` is both.

## The ASGI triplet

Every HTTP handler receives three arguments:

| Argument | Type | Role |
|---|---|---|
| `scope` | `dict` | Request metadata (method, path, headers, query string, …) |
| `receive` | `async callable` | Reads request body events from the client |
| `send` | `async callable` | Writes the response back to the client |

`send` accepts either a `Response` object (as above) or raw ASGI
event dicts; both forms work and can be mixed in the same handler.

### Common scope keys

| Key | Type | Notes |
|---|---|---|
| `scope['type']` | `str` | `'http'` or `'websocket'` |
| `scope['method']` | `str` | `'GET'`, `'POST'`, … |
| `scope['path']` | `str` | URL path, e.g. `'/tasks/42'` |
| `scope['headers']` | `Headers` | Case-insensitive multi-valued header store |
| `scope['query_string']` | `bytes` | Raw query string, parse with `urllib.parse.parse_qs` |
| `scope['path_params']` | `dict` | Values captured from `{name}` segments |
| `scope['state']` | `dict` | Framework-managed per-request scratch space |

Middleware may add more keys — typical custom additions are
`scope['user']` (auth result), `scope['json']` (parsed body).

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
async def health(scope, receive, send):
    await send(JSONResponse({'status': 'ok'}))
```

## When you don't need the triplet

Most handlers only use `scope`, or nothing at all.  BlackBull
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
