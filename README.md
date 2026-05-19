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

- [x] RFC 8441 — WebSocket over HTTP/2 (Extended CONNECT). Currently WebSocket requires HTTP/1.1; when TLS is active the browser negotiates HTTP/2 via ALPN and WebSocket upgrade is blocked.

### P3 — Features and enhancements

- [x] Worker processes — pre-fork **multiprocessing** (not threads); each worker runs its own asyncio event loop. `BB_WORKERS=N` or `0` for cpu_count. SO_REUSEPORT gives each worker its own kernel accept queue.

### P4 — Application framework

- [x] Route lookup cache — internal per-worker LRU cache (transparent, no user API)
- [ ] Response/application caching middleware — cache-control, ETags, user-configurable TTL
- [x] Cookie-based session middleware (signed cookie) — `SessionMiddleware`; HMAC-SHA256, `BB_SESSION_SECRET` or explicit secret
- [ ] OpenAPI / interactive API docs (Swagger UI)
- [ ] beartype for startup type checking on route handlers
