# BlackBull

BlackBull is a Python ASGI 3.0 web framework supporting HTTP/1.1, HTTP/2, and WebSocket.

## Quick Start

```python
import asyncio
from blackbull import BlackBull, Response

app = BlackBull()

@app.route(path='/')
async def hello(scope, receive, send):
    await send(Response(b'Hello, world!'))

if __name__ == '__main__':
    asyncio.run(app.run(port=8000))
```

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

- [x] `make_sender` (HTTP/1.1): serialize ASGI event dict → HTTP/1.1 wire bytes — replaced by `HTTP1Sender` in `sender.py`
- [x] `WebsocketHandler.receive()`: emit `{"type": "websocket.connect"}` on the first call before reading any frame
- [x] `middlewares.py` `websocket()`: check `msg.get('type') != 'websocket.connect'` instead of exact dict equality
- [x] WebSocket Ping (opcode `0x9`): immediately reply with Pong (opcode `0xA`)
- [x] WebSocket: reject unmasked client frames (MUST per RFC 6455 §5.1)
- [x] HTTP/2 `CONTINUATION` frame: concatenate header blocks until `END_HEADERS` flag is set on `HEADERS`
- [x] HTTP/2: normalize header names to lowercase after HPACK decode (RFC 7540 §8.1.2)
- [x] HTTP/2 `DATA` frame: `HTTP2Handler.run()` dropped the `DATA` case during CONTINUATION refactoring — restore so DATA frames call the ASGI app (regression)
- [x] HTTP/2 `GOAWAY` frame: `ResponderFactory` has no `GoAwayResponder` handler — receiving GOAWAY raises `KeyError` (server-side); sending GOAWAY on graceful shutdown is also missing
- [ ] WebSocket §5.2: send CLOSE frame (1002) and fail connection on unknown opcode (currently logs warning and delivers as websocket.receive)
- [ ] WebSocket §7.2: send CLOSE frame (1002) before TCP teardown for protocol violations (currently exception propagates without CLOSE)
- [ ] WebSocket: explicit writer.close() on protocol violations (currently relies on outer finally block)

### P2 — Important protocol features

- [x] HTTP/1.1 Keep-Alive: loop over multiple requests on a single connection
- [x] HTTP/1.1: handle duplicate header names as a list (multi-value headers, RFC 7230 §3.2.2)
- [x] HTTP/2 flow control: check window size before sending `DATA`; issue `WINDOW_UPDATE` after consuming received data
- [x] HTTP/2 `MAX_CONCURRENT_STREAMS`: send `RST_STREAM(REFUSED_STREAM)` when the limit is exceeded
- [x] ASGI lifespan scope: dispatch `lifespan.startup` / `lifespan.shutdown` events on server start/stop
- [x] ASGI `http.disconnect` receive event: detect client disconnect and return `{"type": "http.disconnect"}`
- [x] Timeout handling: wrap header and body reads with `asyncio.wait_for`
- [x] HTTP/1.1 `100 Continue`: send interim response when request includes `Expect: 100-continue`
- [x] HTTP/2 `GOAWAY`: send on graceful shutdown (with last processed stream ID) and on protocol errors

### P3 — Features and enhancements

- [x] HTTP/1.1 responses: automatically add `Content-Length` and `Date` headers
- [x] ASGI lifespan startup/shutdown hooks: `@app.on_startup` / `@app.on_shutdown`
- [x] Path parameter injection into `scope['path_params']` for middleware chains
- [x] Middleware `call_next` convention: Starlette/FastAPI compatible; `inner` still accepted
- [x] `middlewares` parameter on `@app.route()` for per-route middleware
- [x] Route groups: `app.group(middlewares=[...])` (Axum `Router::layer()` equivalent)
- [x] `scope['root_path']`: reverse-proxy mount-path prefix
- [x] WebSocket `Sec-WebSocket-Version: 13` validation
- [x] WebSocket subprotocol negotiation (`Sec-WebSocket-Protocol`): `app.available_ws_protocols` registry; server picks the first client-offered match and includes it in the 101 response
- [x] HTTP response compression: gzip / br / zstd based on `Accept-Encoding`
- [x] Static file serving middleware with `Range` / `206 Partial Content`
- [x] mTLS: client certificate authentication
- [x] HTTP/2 server push: `http.response.push` ASGI event → `PUSH_PROMISE` frame (even promised stream ID) + synthetic GET request dispatched to the app on the promised stream; `scope['extensions']['http.response.push']` advertised on all HTTP/2 scopes
- [x] HTTP/2 stream state machine (RFC 7540 §5.1): IDLE → OPEN/HALF_CLOSED_REMOTE → CLOSED transitions via `on_headers_received()` / `on_data_received()`; DATA on closed stream triggers RST_STREAM
- [x] HTTP/2 priority: RFC 9218 minimum compliance — PRIORITY frames stored (weight corrected per §6.3), PRIORITY_UPDATE (0x10) received without error; no active scheduling
- [x] WebSocket per-message deflate (RFC 7692)
- [x] WebSocket fragmentation: merge continuation frames; detect protocol violations (orphan CONTINUATION, nested opener, fragmented control frame) via `FragmentAssembler`
- [x] Streaming request body (`more_body=True`)
- [x] Streaming response (`StreamingResponse` / chunked transfer)
- [x] ASGI `http.response.trailers`
- [x] HTTP/1.1 access logging: one `blackbull.access` INFO entry per request (client IP, method, path, status, bytes, duration) via `AccessLogRecord`; stored in `scope['state']['access_log']` for middleware extension
- [x] HTTP/2 access logging: per-stream entry using `_make_capturing_send` on the per-stream sender in `HTTP2Handler`
- [x] WebSocket access logging: connection-level entry (client IP, path, close code, duration) via a `_make_capturing_receive` counterpart
- [ ] Worker processes / multiprocessing
- [x] Global middleware: `app.use(mw)`
- [ ] URL reverse lookup (`url_for(name, **params)`)
- [ ] Path parameter type converters (`int`, `uuid`, `path`, …)
- [ ] Built-in CORS middleware
- [x] Built-in request-logging: covered by server-level `AccessLogRecord` (unconditional, extensible from middleware via `scope['state']['access_log']`)

### P4 — Application framework

- [ ] Caching
- [ ] Built-in session middleware (server-side sessions)
- [ ] OpenAPI / interactive API docs (Swagger UI)
