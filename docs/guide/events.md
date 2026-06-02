# Events

BlackBull exposes an event-driven hook API alongside the ASGI
request lifecycle.  Application code registers handlers with one
of two decorators and the framework dispatches events at well-
defined points in the application's lifetime.

## Two hook kinds

| Property | `@app.intercept(name)` | `@app.on(name)` |
| --- | --- | --- |
| Execution | Synchronous (awaited inline) | Asynchronous (fire-and-forget) |
| Order | Registration order, serial | Independent, no inter-handler order |
| Exceptions | Propagate to the emitter | Isolated, logged, never propagate |
| Can short-circuit / modify flow | Yes | No |
| Typical use | Auth, validation, rewriting | Logging, metrics, tracing |

The split is a deliberate defence.  Writing an authentication
check as an observer would silently let unauthorized requests
through — the request proceeds before the observer has finished,
and an observer cannot signal failure to the emitter.  Conversely,
putting slow telemetry into an interceptor blocks the request
path on every emission.

There is no third mode that "observes but blocks" or "intercepts
but is fire-and-forget" — pick one based on whether the hook is
allowed to affect the request.

## `@app.intercept` — synchronous interception

Register an interceptor for a named event:

```python
from blackbull import BlackBull, Event

app = BlackBull()

@app.intercept('app_startup')
async def warm_cache(event: Event):
    await cache.preload()
```

Interceptors are awaited in registration order.  An exception
raised by an interceptor:

- aborts the remaining interceptors for that event, and
- propagates to the code that called `emit`.

For lifespan events, an interceptor exception surfaces to the
ASGI server driving the lifespan protocol — typically aborting
startup.

## `@app.on` — fire-and-forget observation

Register an observer for a named event:

```python
@app.on('app_shutdown')
async def flush_metrics(event: Event):
    await metrics_client.flush()
```

Observers are scheduled with `asyncio.create_task` when the event
is emitted and run independently of the emitter.  They cannot
block the emitter, and their exceptions are caught, logged on the
`blackbull` logger at `ERROR`, and discarded — they cannot surface
to the emitter or to other observers.

Use observers for telemetry, audit logging, cache warming, and
other side-effects that the request path should not depend on.

## The `Event` object

Both decorators register handlers with the signature
`async def handler(event: Event)`.  `Event` is a frozen dataclass:

```python
from dataclasses import dataclass, field

@dataclass(frozen=True)
class Event:
    name: str
    detail: dict = field(default_factory=dict)
```

`name` identifies which event fired; `detail` carries event-specific
data.  The catalogue of events and their `detail` shape is below.

## Lifespan sugar

For lifespan events where the handler doesn't need the `Event`
object, two zero-argument sugar decorators are provided:

```python
@app.on_startup
async def init_db():
    await db.connect()

@app.on_shutdown
async def close_db():
    await db.disconnect()
```

These are equivalent to:

```python
@app.intercept('app_startup')
async def _wrapped_init_db(event: Event):
    await init_db()
```

Use the sugar form when your handler does not need the event
itself; reach for `@app.intercept('app_startup')` directly when
you want the consistency of writing every hook as
`(event: Event) -> None`.

## Event reference

| Event | Fires when | `detail` keys | Notes |
| --- | --- | --- | --- |
| `app_startup` | Server has bound its socket and is about to accept connections | *(empty)* | Sugar: `@app.on_startup` |
| `app_shutdown` | Server has received a stop signal and is about to exit | *(empty)* | Sugar: `@app.on_shutdown` |
| `request_received` | HTTP request headers parsed, before routing | `scope`, `client_ip`, `method`, `path`, `http_version`, `headers` | `@app.on` and `@app.intercept`; raise to abort |
| `before_handler` | Route matched, before handler dispatch | `scope`, `client_ip`, `method`, `path`, `http_version`, `headers` | `@app.on` and `@app.intercept`; raise to abort |
| `after_handler` | Handler returned normally | `scope`, `client_ip`, `method`, `path`, `http_version` | Observation only |
| `request_completed` | HTTP request finished — response fully sent (or failed before that) | `scope`, `client_ip`, `method`, `path`, `http_version`, `status`, `response_bytes`, `duration_ms` | Observation only; not fired if client disconnected or for WebSocket |
| `request_disconnected` | HTTP client closed connection before response complete | `scope`, `client_ip`, `method`, `path`, `http_version` | Observation only; mutually exclusive with `request_completed` |
| `websocket_message` | WebSocket message fully received and reassembled, before the handler reads it | `scope`, `text`, `bytes` | Observation only |
| `websocket_connected` | `websocket.accept` sent | `connection_id`, `path`, `client_ip`, `subprotocol` | Observation only |
| `websocket_disconnected` | WebSocket connection closed | `connection_id`, `code` | Observation only |

Request flow:

```
request_received → routing → before_handler → handler → after_handler → request_completed
                                                            ↑
                                                         (or) request_disconnected
```

### `request_received` — earliest gate

Fires after the request line and headers are parsed but before
routing or handler dispatch.  Fires for both HTTP/1.1 and HTTP/2
(once per stream).  WebSocket connections never fire this event.

```python
@app.intercept('request_received')
async def require_api_key(event):
    headers = event.detail['scope']['headers']
    if headers.get(b'x-api-key') != b'secret':
        raise PermissionError('missing or invalid API key')

@app.on('request_received')
async def record_hit(event):
    metrics.increment('http.requests', tags={'path': event.detail['path']})
```

Use `@app.intercept('request_received')` for early gates (auth,
rate limiting); use `@app.on('request_received')` for passive
observation.

### `request_completed` — access-log-shaped

Fires once per HTTP request after the response has been sent (or
after the request has otherwise concluded — for example, after a
404 or after an unhandled exception in the handler).  Fires from
the same site as the `blackbull.access` log record.

| Key | Type | Description |
| --- | --- | --- |
| `scope` | `dict` | The ASGI scope dict for the request |
| `client_ip` | `str` | Remote address (`'-'` when unavailable) |
| `method` | `str` | HTTP method |
| `path` | `str` | Request path |
| `http_version` | `str` | Protocol version (e.g. `'1.1'`, `'2'`) |
| `status` | `int` \| `str` | Response status code, or `'-'` if not sent |
| `response_bytes` | `int` | Total body bytes written to the wire |
| `duration_ms` | `float` | Wall-clock duration from first byte received to response complete |

```python
@app.on('request_completed')
async def log_request(event: Event):
    d = event.detail
    print(f"{d['method']} {d['path']} → {d['status']} ({d['duration_ms']:.1f}ms)")
```

**Observation only.**  The response has already been sent by the
time this fires; nothing an interceptor does can change what the
client received.

### `request_disconnected` — client gave up

Fires when an HTTP client closes the connection before the
response has been fully sent.  **Mutually exclusive** with
`request_completed`: a request that disconnected does not also
fire `request_completed`.

The event fires when the server detects the disconnect via the
application's `receive()` call — specifically when `receive()`
returns `{'type': 'http.disconnect'}`.  Long-polling or SSE
handlers that call `receive()` to watch for disconnect trigger
this event the moment the client closes.

For HTTP/2, when the underlying TCP connection closes while
streams are still in flight (the common case for SSE), the
framework injects an `http.disconnect` event into every active
stream's receive channel.

WebSocket connections never fire `request_disconnected` — use
`websocket_disconnected` instead.

### `websocket_message` — observation only

Fires once per fully reassembled WebSocket message, inside the
server read loop, *before* the message is delivered to the
application's `receive()` call.  Observers see every message the
server accepted — including ones the handler never reads.

```python
@app.on('websocket_message')
async def log_message(event: Event):
    if event.detail['text'] is not None:
        print(f"[ws] text on {event.detail['scope']['path']}: "
              f"{event.detail['text']!r}")
    else:
        print(f"[ws] binary on {event.detail['scope']['path']}: "
              f"{len(event.detail['bytes'])} bytes")
```

The message has already been read off the wire by the time the
event fires, so interceptors cannot suppress delivery — use
`@app.on` only.

### `websocket_connected` / `websocket_disconnected`

`websocket_connected` fires once per connection, immediately after
the server sends the `websocket.accept` event.
`websocket_disconnected` fires when the server detects the close,
whether the client or the handler closed it.

Both carry a `connection_id` (UUID string) that is stable for the
lifetime of the connection — correlate `connected` and
`disconnected` records to compute connection duration.

```python
@app.on('websocket_connected')
async def on_connected(event):
    logger.info('WS connected id=%s path=%s',
                event.detail['connection_id'], event.detail['path'])

@app.on('websocket_disconnected')
async def on_disconnected(event):
    logger.info('WS disconnected id=%s code=%s',
                event.detail['connection_id'], event.detail['code'])
```

Both are **observation only** — the connection lifecycle is
driven by the ASGI handler, not by interceptors.

## Exception handling

Restated:

- **Interceptor raises** → remaining interceptors for that event
  do not run; the exception propagates to the emitter (the
  framework code that called `emit`).  For lifespan events, this
  typically aborts startup or shutdown.
- **Observer raises** → the exception is caught and logged at
  `ERROR` on the `blackbull` logger; other observers for the same
  event continue to run; the emitter never sees the exception.

There is no built-in re-emission of failures as a separate
`error` event.  If you want one, register a wrapper observer
that emits an application-defined event in its own `except`
block.

## Observer task lifecycle at shutdown

Observers run as detached `asyncio.Task`s, so in-flight observers
can outlive the request that triggered them.  At `app_shutdown`,
BlackBull waits up to `observer_shutdown_timeout` seconds (default
5, configurable via `BlackBull(observer_shutdown_timeout=...)`)
for still-running observer tasks to finish.  Tasks still running
after the timeout are cancelled and a `WARNING` is logged on the
`blackbull` logger naming the unfinished coroutine.

Observers therefore have a *conditional* fire-and-forget
guarantee: during normal request processing they do not block the
emitter, but during shutdown they are expected to finish promptly.
If you have observation work that legitimately takes longer than
a few seconds (sending events to a remote analytics endpoint, for
example), enqueue the work from the observer into your own queue
rather than performing it inline.

## Next

- [Middleware](middleware.md) — for hooks that need to wrap the
  full request/response (rather than fire at lifecycle points).
- [Logging](logging.md) — access log, framework loggers,
  `request_completed` integration.
