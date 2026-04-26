# CLAUDE.md — BlackBull

## Project overview

BlackBull is a Python ASGI 3.0 web framework built from scratch.
It handles HTTP/1.1, HTTP/2, and WebSocket at the protocol level (no uvicorn/starlette underneath).
The codebase is a personal learning project, so correctness over the wire matters more than API stability.

## Development workflow

Follow these four steps for every non-trivial change:

### 1. Plan

Use plan mode (`/plan`) to design the change before touching any code.
A good plan names the files and line ranges that will change, explains the
approach, and notes any invariants that must be preserved (e.g. the ASGI
event contract, RFC requirements, typeguard compatibility).

### 2. Write tests first

Add or update tests in `tests/` before implementing.  Aim for tests that
describe the *observable behaviour* (ASGI events emitted / bytes on the wire),
not implementation internals.

- Protocol-level tests: use `AsyncMock` / fake readers+writers injected via
  `AbstractReader` / `AbstractWriter` ABCs — no live sockets needed.
- Integration tests: start `ASGIServer` in a background thread and use
  `httpx.AsyncClient(transport=httpx.ASGITransport(app=app))` for HTTP, or
  the `websockets` library for WebSocket.
- Mark async tests `@pytest.mark.asyncio` (or set `asyncio_mode = auto` in
  `pytest.ini`).

Run the new tests and confirm they fail before implementation:

```bash
pytest tests/<relevant_file>.py -q
```

### 3. Implement the code

Write the minimum code that makes the tests pass.  Do not add features,
refactoring, or abstractions beyond what the tests require.

After implementation, run the full suite to catch regressions:

```bash
pytest
```

### 4. Type checking

Open modified files in VSCode and resolve every Pylance red underline
(`python.analysis.typeCheckingMode: "basic"`).  Key rules:

- Header names and values are always `bytes`.
- Use `HeaderList` (= `Iterable[tuple[bytes, bytes]]`) for header sequences
  in function signatures.
- `Headers.__init__` accepts any `Iterable[tuple[bytes, bytes]]`; no need to
  call `list()` at call sites.
- Narrow `None` with `assert` or `is not None` before attribute access when
  Pylance cannot infer the type from control flow.
- The `--typeguard-packages=blackbull` flag in the VSCode test runner enforces
  type annotations at runtime; keep annotations accurate.

---

## Package layout

```
blackbull/
  BlackBull.py          # App class: routing, lifespan, error handlers
  router.py             # Route/group registration and matching
  response.py           # Response, JSONResponse, WebSocketResponse, cookie_header
  request.py            # read_body, parse_cookies
  utils.py              # Scheme enum, EventEmitter, helpers
  middleware/
    __init__.py         # exports: compress, websocket
    compression.py      # CompressionMiddleware (gzip/br/zstd)
    websocket.py        # websocket middleware (connect/accept boilerplate)
  server/
    server.py           # ASGIServer, HTTP1/2/WebSocketHandler, LifespanManager
    sender.py           # HTTP1Sender, HTTP2Sender, WebSocketSender + AbstractWriter
    recipient.py        # HTTP1Recipient, HTTP2Recipient, WebSocketRecipient + AbstractReader
    headers.py          # Headers class (multi-value, case-insensitive, bytes keys)
    parser.py           # HTTP/1.1 request line + header parser
    response.py         # RespondFactory (maps frame types to response handlers)
  protocol/
    frame.py            # HTTP/2 frame encoding/decoding (HPACK via h2 library)
    stream.py           # HTTP/2 stream state
    rsock.py            # Dual-stack socket helpers
examples/
  SimpleTaskManager/    # REST API + HTML UI, SQLite, Bearer auth
  ChatServer/           # WebSocket in three styles
docs/
  guide.md              # Full developer guide
tests/                  # pytest test suite (see Testing section)
```

## Running

```bash
pip install -e .                        # editable install (core deps)
pip install -e '.[compression]'         # add brotli + zstandard
pip install -e '.[testing]'             # add pytest + pytest-asyncio

python examples/helloworld.py           # HTTP/1.1 on port 8000
python app.py --port 8443 --cert cert.pem --key key.pem   # HTTPS + HTTP/2
```

## Testing
- Always run pytest with a timeout: `--timeout=30` (requires pytest-timeout)
- Always use `timeout` command as a safety wrapper: `timeout 60 python -m pytest ...`
- If tests hang, kill and report what was last running rather than waiting
- Never run pytest without a timeout limit

```bash
pytest                   # run all tests (uses pytest.ini config)
pytest tests/test_websocket_handler.py -q   # single file
```

The VSCode test runner passes `--typeguard-packages=blackbull` which enables
runtime type checking via `typeguard`. Tests must pass both normally and with
typeguard active.

**Required test fixtures**: `tests/cert.pem` and `tests/key.pem` must exist.
Generate once with:
```bash
openssl req -x509 -newkey rsa:2048 -keyout tests/key.pem -out tests/cert.pem \
  -days 365 -nodes -subj '/CN=localhost'
```

`pytest.ini` writes logs to `test.log` at DEBUG level.

## Type checking

Pylance runs in `"python.analysis.typeCheckingMode": "basic"` (see `.vscode/settings.json`).
Fix pyright/Pylance errors in modified files; do not introduce new red underlines.

Key type aliases:
- `HeaderList = Iterable[tuple[bytes, bytes]]` — used in sender signatures
- `Headers` — multi-value header store; constructor accepts any `Iterable[tuple[bytes, bytes]]`
- All header names and values are **bytes**, not str

## Middleware convention

```python
async def my_mw(scope, receive, send, call_next):
    # pre-handler work
    await call_next(scope, receive, send)
    # post-handler work
```

- `call_next` is bound by `_register_chain` via `functools.partial`
- The legacy name `inner` is accepted as an alias
- Short-circuit by returning without calling `call_next`

## ASGI event types used

| Direction | Event type | Notes |
|---|---|---|
| receive | `http.request` | `body`, `more_body` |
| receive | `http.disconnect` | client closed |
| receive | `websocket.connect` | first receive() call |
| receive | `websocket.receive` | `text` or `bytes` |
| receive | `websocket.disconnect` | `code` |
| send | `http.response.start` | `status`, `headers` |
| send | `http.response.body` | `body`, `more_body` |
| send | `http.response.trailers` | `headers` (chunked trailer) |
| send | `websocket.accept` | |
| send | `websocket.send` | `text` or `bytes` |
| send | `websocket.close` | |

## Important implementation details

- **WebSocket frames**: server never masks outgoing frames (RFC 6455 §5.1); client frames must be masked or a `ValueError` is raised.
- **RSV1 = per-message deflate** (RFC 7692): `WebSocketRecipient` decompresses with `zlib.decompress(payload + b'\x00\x00\xff\xff', wbits=-15)`.
- **HTTP/2 flow control**: `HTTP2Sender._write` blocks on `asyncio.Event` when the window is exhausted; `window_update()` unblocks it.
- **Headers class**: always use bytes keys, e.g. `headers.get(b'content-type')`. The index is built lowercase.
- **AbstractReader / AbstractWriter**: protocol-agnostic ABCs. `AsyncioReader` / `AsyncioWriter` wrap asyncio streams. Tests inject mocks via the ABC.

## What is NOT implemented (P3/P4 todo)

Do not add these unless explicitly asked:
- Streaming response (`StreamingResponse` / chunked helper)
- Static file serving with `Range` / 206
- WebSocket subprotocol negotiation
- WebSocket fragmentation (continuation frames)
- HTTP/2 server push / stream state machine / priority
- Worker processes / multiprocessing
- Global middleware (`app.use(mw)`)
- URL reverse lookup, path parameter type converters
- Built-in CORS / request-logging middleware
- Session middleware, ORM, template engine, OpenAPI
