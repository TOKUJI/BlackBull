# Logging

BlackBull uses two separate logger hierarchies, each with a
distinct purpose:

| Logger | Level | What it carries |
|---|---|---|
| `blackbull.access` | `INFO` | One record per completed HTTP/1.1 request (access log) |
| `blackbull` (+ children) | `DEBUG` | Internal framework events (frame parsing, HPACK, routing decisions, TLS handshake) |

Both follow standard `logging` semantics — no handlers attached
by default, so nothing is printed until you opt in.

## Access log — `blackbull.access`

For every completed HTTP/1.1 request the server emits one `INFO`
record on the `blackbull.access` logger.  Default format:

```
{client_ip} "{method} {path} HTTP/{version}" {status} {bytes} {duration}ms
```

Example:

```
203.0.113.42 "POST /tasks HTTP/1.1" 201 87 3ms
```

Enable to stdout the same way as any Python logger:

```python
import logging

handler = logging.StreamHandler()
handler.setFormatter(logging.Formatter('%(asctime)s %(message)s'))
logging.getLogger('blackbull.access').addHandler(handler)
logging.getLogger('blackbull.access').setLevel(logging.INFO)
```

To a rotating file:

```python
from logging.handlers import RotatingFileHandler

fh = RotatingFileHandler('access.log', maxBytes=10_000_000, backupCount=5)
fh.setFormatter(logging.Formatter('%(message)s'))
logging.getLogger('blackbull.access').addHandler(fh)
logging.getLogger('blackbull.access').setLevel(logging.INFO)
```

The record is emitted in a `finally` block after the response
completes — even if the app raises an unhandled exception.  When
the app never sent a response, `status` is the literal `'-'`.

### Named fields in the LogRecord

Every access log record carries the following named attributes,
available in a custom `logging.Formatter` format string:

| Attribute | Type | Example |
|---|---|---|
| `%(client_ip)s` | `str` | `203.0.113.42` |
| `%(method)s` | `str` | `POST` |
| `%(path)s` | `str` | `/tasks` |
| `%(http_version)s` | `str` | `1.1` |
| `%(status)s` | `int` or `'-'` | `201` |
| `%(response_bytes)d` | `int` | `87` |
| `%(duration_ms).1f` | `float` | `3.4` |

Custom format:

```python
fmt = ('%(asctime)s %(client_ip)s "%(method)s %(path)s" '
       '%(status)s %(response_bytes)d %(duration_ms).0fms')
logging.getLogger('blackbull.access').handlers[0].setFormatter(
    logging.Formatter(fmt)
)
```

### Disabling the access log

Set the level above `INFO`, or set the environment variable
`BB_ACCESS_LOG=0` (which gates record formatting at the call
site — useful when running benchmarks that don't want logging
overhead).

## Extending the access log record from middleware

The `AccessLogRecord` for the current request is stored at
`scope['state']['access_log']`.  Middleware that runs before the
handler can attach extra attributes:

```python
import uuid

async def request_id_mw(scope, receive, send, call_next):
    req_id = uuid.uuid4().hex
    scope['request_id'] = req_id
    # Attach to the access log record so it appears in log output
    record = scope['state'].get('access_log')
    if record:
        record.request_id = req_id   # arbitrary extra attribute
    await call_next(scope, receive, send)
```

A custom `logging.Filter` can then surface the attribute:

```python
class AccessLogFilter(logging.Filter):
    def filter(self, record):
        record.request_id = getattr(record, 'request_id', '-')
        return True

handler = logging.StreamHandler()
handler.addFilter(AccessLogFilter())
handler.setFormatter(logging.Formatter(
    '%(message)s req_id=%(request_id)s'
))
logging.getLogger('blackbull.access').addHandler(handler)
```

## Framework debug log — `blackbull`

Internal framework events (frame parsing, HPACK, routing
decisions, TLS handshake) are logged on the `blackbull` logger
and its children (`blackbull.server.server`,
`blackbull.protocol.frame`, …) at `DEBUG` level.

Enable for development:

```python
import logging

logging.basicConfig(level=logging.DEBUG)
# or target just the server layer:
logging.getLogger('blackbull.server').setLevel(logging.DEBUG)
```

This is separate from the access log so that production
deployments can enable access logging without flooding logs with
internal debug output.

### `@log` decorator

The `@log` decorator from `blackbull.logger` annotates a function
so that its call arguments are logged at `DEBUG` level using the
caller module's logger:

```python
from blackbull.logger import log

@log
async def my_fn(x, y):
    ...
# logs: my_fn((x_val, y_val), {}) at DEBUG level
```

**Zero-overhead at non-DEBUG level.**  The check runs at
decoration time (import), not on every call.  When the module
logger is not enabled for `DEBUG` at import time, the decorator
returns the original function unwrapped — there is no extra call
frame or level-check overhead in production.

The trade-off: setting the log level to `DEBUG` *after* modules
have already been imported will not activate `@log` logging for
already-decorated functions.  Configure `DEBUG` level before
importing framework modules, or restart the process.

## Forwarding logs to a remote server

`logging.Handler.emit()` is synchronous.  Calling a blocking HTTP
request directly from `emit` would stall the asyncio event loop.
The solution is the standard library's `QueueHandler` +
`QueueListener` pair: the handler enqueues records in O(1) without
blocking, and a background thread drains the queue and calls the
real (blocking) HTTP handler.

A complete two-process example is provided in
[`examples/LoggingExample/`](https://github.com/TOKUJI/BlackBull/tree/master/examples/LoggingExample/):

| File | Role |
|---|---|
| `web_server.py` | BlackBull hello-world with `JsonHTTPHandler` + `QueueListener` |
| `log_server.py` | `http.server` that receives JSON records and inserts them into SQLite |

Start order:

```bash
# Terminal 1
python examples/LoggingExample/log_server.py   # listens on :9000

# Terminal 2
python examples/LoggingExample/web_server.py   # listens on :8000

# Make some requests
curl http://localhost:8000/
curl http://localhost:8000/tasks

# Inspect the database
sqlite3 examples/LoggingExample/logs.db \
    "SELECT client_ip, method, path, status, duration_ms FROM access_logs;"
```

The shape of the wiring:

```python
import queue, logging
from logging.handlers import QueueHandler, QueueListener

_log_queue    = queue.Queue(-1)          # unbounded
_json_handler = JsonHTTPHandler('localhost:9000')
_listener     = QueueListener(_log_queue, _json_handler,
                              respect_handler_level=True)

_access_logger = logging.getLogger('example.access')
_access_logger.addHandler(QueueHandler(_log_queue))
_access_logger.setLevel(logging.INFO)


@app.on_startup
async def start_log_listener():
    _listener.start()


@app.on_shutdown
async def stop_log_listener():
    _listener.stop()


@app.on('request_completed')
async def log_response(event):
    d = event.detail
    _access_logger.info('%s %s → %s (%.1f ms)',
                        d['method'], d['path'], d['status'], d['duration_ms'],
                        extra={'client_ip': d['client_ip'] or '-',
                               'method': d['method'], 'path': d['path'],
                               'status': d['status'],
                               'response_bytes': d['response_bytes'],
                               'duration_ms': d['duration_ms']})
```

`QueueHandler.emit()` puts the record in the queue and returns
immediately.  `QueueListener` runs in a daemon thread and calls
`JsonHTTPHandler.emit()` there — the blocking HTTP call never
touches the event-loop thread.

`@app.on_startup` / `@app.on_shutdown` tie the listener lifecycle
to the server, so the background thread starts only when the
server is ready and is flushed and joined cleanly before the
process exits.

## Not yet implemented

- **HTTP/2 access logging** — per-stream entries.  Today the
  access log covers HTTP/1.1 only.
- **WebSocket access logging** — connection-level entry (client
  IP, path, close code, duration).  Today the `websocket_*`
  events from [Events](events.md) cover the same data; build
  your own logger on top if you need persistence.

## Next

- [Events](events.md) — `@app.on('request_completed')` for
  per-request observability without touching the access log.
- [Configuration](configuration.md) — environment variables that
  affect logging (`BB_ACCESS_LOG`, `BB_ASYNC_LOGGING`).
