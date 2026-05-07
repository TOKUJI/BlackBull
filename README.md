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

- [ ] WebSocket §5.2: send CLOSE frame (1002) and fail connection on unknown opcode (currently logs warning and delivers as websocket.receive)
- [ ] WebSocket §7.2: send CLOSE frame (1002) before TCP teardown for protocol violations (currently exception propagates without CLOSE)
- [ ] WebSocket: explicit writer.close() on protocol violations (currently relies on outer finally block)

### P2 — Important protocol features


### P3 — Features and enhancements

- [ ] Worker processes / multiprocessing
- [ ] URL reverse lookup (`url_for(name, **params)`)
- [ ] Path parameter type converters (`int`, `uuid`, `path`, …)
- [ ] Built-in CORS middleware
 
### P4 — Application framework

- [ ] Caching
- [ ] Built-in session middleware (server-side sessions)
- [ ] OpenAPI / interactive API docs (Swagger UI)
