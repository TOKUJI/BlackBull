# BlackBull

BlackBull is a Python ASGI 3.0 web framework supporting HTTP/1.1, HTTP/2, and WebSocket.

## Quick Start

```python
import asyncio
from blackbull import BlackBull, Response

app = BlackBull()

@app.route(path='/')
async def hello():
    return "Hello, world!"

if __name__ == '__main__':
    asyncio.run(app.run(port=8000))
```

Handlers also accept `(scope, receive, send)` for full ASGI control.

**Full documentation:** [`docs/guide.md`](docs/guide.md)

## Examples

| Example | Demonstrates |
|---|---|
| [`examples/SimpleTaskManager/`](examples/SimpleTaskManager/) | REST API + HTML UI, middleware pipeline, route groups, SQLite, Bearer token auth |
| [`examples/ChatServer/`](examples/ChatServer/) | WebSocket, SSE, long polling — three implementation styles (raw ASGI → Flask-like → middleware) |
| [`examples/typed_routes_ok.py`](examples/typed_routes_ok.py) | `{param:converter}` syntax, `url_path_for` — validation passes |
| [`examples/typed_routes_fail.py`](examples/typed_routes_fail.py) | Same routes with annotation mismatch — `ConfigurationError` at startup |

## Running

```bash
pip install .
python app.py --port 8000                              # HTTP/1.1
python app.py --port 8443 --cert cert.pem --key key.pem  # HTTPS + HTTP/2
```

## Testing

```bash
pytest
```

---

## Todo

### P1 — Spec violations / breaks conformant ASGI apps

### P2 — Important protocol features

- [ ] RFC 8441 — WebSocket over HTTP/2 (Extended CONNECT). Currently WebSocket requires HTTP/1.1; when TLS is active the browser negotiates HTTP/2 via ALPN and WebSocket upgrade is blocked.

### P3 — Features and enhancements

- [ ] Worker processes / multiprocessing
- [ ] Built-in CORS middleware
 
### P4 — Application framework

- [ ] Caching
- [ ] Built-in session middleware (server-side sessions)
- [ ] OpenAPI / interactive API docs (Swagger UI)
- [ ] beartype for startup type checking on route handlers
