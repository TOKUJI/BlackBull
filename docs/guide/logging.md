# Logging

BlackBull uses three separate logger hierarchies, each with a
distinct purpose:

| Logger | Level | What it carries |
|---|---|---|
| `blackbull.access` | `INFO` | One record per completed HTTP/1.1 request (access log) |
| `blackbull.caps` | `WARNING` | One record per cap rejection (header sizes, timeouts, connection cap, WS frame cap, H/2 stream caps, compression in-flight, …) |
| `blackbull` (+ children) | `DEBUG` | Internal framework events (frame parsing, HPACK, routing decisions, TLS handshake) |

All three follow standard `logging` semantics — no handlers
attached by default, so nothing is printed until you opt in.

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

## Built-in async logging sinks

With `BB_ASYNC_LOGGING=1` (the default) BlackBull installs a `QueueHandler` on
the `blackbull` logger so every log call from the event loop enqueues in O(1); a
background `QueueListener` thread drains the queue and writes to a sink. You
select the sink and format entirely with environment variables — no code:

| Variable | Default | Effect |
|---|---|---|
| `BB_LOG_FILE` | *(stderr)* | Write to a file (append mode) instead of `stderr`. |
| `BB_LOG_FORMAT` | *(plain)* | `json` → one structured JSON object per line (the `AccessLogRecord` fields become top-level keys). |
| `BB_SYSLOG_ADDR` | *(unset)* | `host:port` → ship records via a UDP `SysLogHandler`. |
| `BB_LOG_BATCH_SIZE` | `64` | Coalescing width — records joined into one `write()`+`flush()`. |
| `BB_LOG_BATCH_TIMEOUT_MS` | `5` | Max time a partial batch waits before flushing. |

**Async logging is batch logging.** The stream/file sink *always* coalesces
records into one write per batch — a per-record `flush()` is the dominant cost of
a high-rate access log (one flush syscall per request, contending for the GIL
with the event loop). `BB_LOG_BATCH_SIZE` tunes the coalescing width, not an
on/off switch; the timeout bounds visibility latency at low rate. To force an
immediate per-record flush, disable async logging (`BB_ASYNC_LOGGING=0`, the
synchronous path).

When the access logger is left in its default state, records are enqueued on a
fast path that skips the stdlib `logging.Logger._log` machinery (~93% of the
per-emit cost). This is transparent: if you attach your own handlers or filters
to `blackbull.access` (see the next section), BlackBull automatically uses the
standard logging path so they are honoured.

For the full list see [environment variables](../reference/env-vars.md).

## Extending the access log record from middleware

The `AccessLogRecord` for the current request is stored at
`conn.state['access_log']`.  Middleware that runs before the
handler can attach extra attributes:

```python
import uuid

async def request_id_mw(conn, receive, send, call_next):
    req_id = uuid.uuid4().hex
    conn.state['request_id'] = req_id
    # Attach to the access log record so it appears in log output
    record = conn.state.get('access_log')
    if record:
        record.request_id = req_id   # arbitrary extra attribute
    await call_next(conn, receive, send)
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

### The same applies to internal `DEBUG` logging

Framework modules on a per-request path — request dispatch, HTTP/2
frame parsing, the response senders — read the `DEBUG` level once
at import and branch on the result, for the same reason `@log`
does.  A `logger.debug(...)` call that emits nothing is not free:
the call happens, its arguments are built, and the level is
checked, which measured at 24 bytecode instructions per site.
HTTP/2 was making twenty such calls per request.

So **configure `DEBUG` before importing `blackbull`** if you want
internal debug output:

```python
import logging
logging.basicConfig(level=logging.DEBUG)   # before the import below

from blackbull import BlackBull
```

Raising the level afterwards still affects `WARNING`/`ERROR` and
the access log, which are checked per call as usual; it will not
switch on the per-request `DEBUG` traces.  Those paths emit around
twenty lines per request, so they are a development setting rather
than something to enable on a running server.

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

## Cap-hit log — `blackbull.caps`

Every user-tunable resource cap in BlackBull emits one `WARNING`
record on `blackbull.caps` when it fires.  Coverage:

| Cap (env var) | Where it fires |
|---|---|
| `BB_MAX_CONNECTIONS` | accept loop — connection cap hit |
| `BB_HEADER_TIMEOUT` | slowloris defence — headers didn't arrive in time |
| `BB_HEADER_MAX_LINE` | per-line header limit exceeded |
| `BB_HEADER_MAX_TOTAL` | aggregate header block exceeded (H/1.1 + H/2 CONTINUATION) |
| `BB_BODY_TIMEOUT` | body bytes didn't arrive in time |
| `BB_MAX_BODY_SIZE` | request body over the total cap (H/1.1 + H/2; `requested` is the declared length at head time, the running total mid-stream) |
| `BB_MIN_BODY_RATE` | body delivered below the minimum rate past the grace period — `requested` is the observed rate in bytes/second |
| `BB_REQUEST_TIMEOUT` | handler exceeded per-request budget (H/1.1 + H/2) |
| `BB_WRITE_TIMEOUT` | drain stalled (slow-read peer) |
| `BB_WS_MAX_FRAME_PAYLOAD` | WebSocket frame declared length exceeded |
| `BB_WS_MAX_MESSAGE_SIZE` | WebSocket message over the total cap post-reassembly / post-inflation — `requested` is the size reached when the bound tripped, never the size the message would have become |
| `BB_FRAME_RATE_LIMIT` | A metered control frame exceeded its per-type budget (H/2 `RST_STREAM` — inbound or server-emitted — `PING`, `SETTINGS`, zero-length frames; WebSocket control frames).  Logged as cap name `frame_rate`; `protocol` says which protocol |
| `BB_MQTT_MAX_PACKET_SIZE` | MQTT packet declared larger than the cap — `requested` is the declared size, since the payload is refused unread |
| `BB_MQTT_MAX_QUEUED_MESSAGES` | MQTT session backlog full while the client's Receive Maximum window was; `scope_path` carries the topic |
| `BB_MQTT_MAX_RETAINED` | MQTT retained publish to a new topic refused at the store cap; `scope_path` carries the topic |
| `BB_H2_MAX_CONCURRENT_STREAMS` | HTTP/2 stream-open guard tripped |
| `BB_H2_WS_MAX_STREAMS_PER_CONNECTION` | RFC 8441 WebSocket stream cap tripped |
| `BB_COMPRESSION_MAX_INFLIGHT` | Compression middleware bypassed (executor saturated) |

The async client under `blackbull/client/` keeps the same record for its
own bounds.  The argument for a client having bounds at all is
diagnostic — a client picks its peer, and
[fault injection](fault_injection.md) exists to point one at a server
that misbehaves — so a bound that refused without naming itself would be
no diagnostic at all.  A cap enforced on both protocols is two rejection
sites and keeps two records, which is why this table has a column per
protocol rather than a row per cap:

| Cap (env var) | HTTP/1.1 | HTTP/2 |
|---|---|---|
| `BB_CLIENT_HEAD_MAX_TOTAL` | the response head over the budget; the trailer section over the same one | the stream's field lines in aggregate; the encoded block reassembled across CONTINUATION |
| `BB_CLIENT_HEAD_MAX_LINE` | a status line, response field line, chunk-size line or trailer field line over the per-line rule | — no field *line* exists; the section is the unit |
| `BB_CLIENT_HEAD_TIMEOUT` | the response head did not arrive in time | the peer took the request and never began to answer — that stream is reset; or a field block opened and never finished with END_HEADERS — the connection ends |
| `BB_CLIENT_BODY_TIMEOUT` | one body read outlasted its deadline | no frame for that stream inside the deadline |
| `BB_CLIENT_BODY_MAX_TOTAL` | a declared `Content-Length` over the cap, or the running total of a chunked or close-delimited body | the running total, checked before a DATA payload is held |
| `BB_CLIENT_MIN_BODY_RATE` | body arriving below the floor past the grace period — `requested` is the observed rate in bytes/second | — no rate floor on HTTP/2 |
| `BB_CLIENT_MAX_INTERIM_RESPONSES` | too many `1xx` responses before the final one | — `BB_CLIENT_HEAD_MAX_TOTAL` owns that aggregate |
| `BB_CLIENT_RAW_QUEUE_DEPTH` | — no raw-stream hatch | the raw-stream queue is full |
| `BB_CLIENT_H2_MAX_FRAME_SIZE` | — no frame | the declared frame length is over the cap |
| `BB_CLIENT_H2_MAX_HEADER_LIST_SIZE` | — no field section | the decoded field section is over the cap.  `requested` is a **lower bound**, not the figure: hpack reports the limit it refused at and never the total it reached.  A tight one — it charges each entry and compares immediately, so it raises on the entry that crosses and the section is provably just over the limit |

A time bound records only when **its own** deadline expired.  A
`TimeoutError` that reached the client from inside the transport is not
the cap refusing, and is not filed under one.

`BB_CLIENT_H2_ENABLE_PUSH` and `BB_CLIENT_MIN_BODY_RATE_GRACE` keep no
record because neither is a cap: the first is a conformance switch that
refuses nothing on its own, and the second is a grace-period modifier of
`BB_CLIENT_MIN_BODY_RATE`, which owns both the refusal and the record.

`BB_WS_QUEUE_DEPTH` is intentionally **not** logged — when read-ahead
is enabled at all, the WebSocket event queue applies backpressure
(blocking `await put()`) rather than dropping events, so a hit is
normal flow control rather than a rejection.  At the default depth of
`0` there is no queue to hit.  The HTTP/2 per-stream queue (depth controlled by
`BB_STREAM_QUEUE_DEPTH`-style internals) does drop and is logged
under the cap name `stream_queue_depth`.

### Record shape

Each record carries the cap name in the message and the structured
fields in `record.extra`:

| `extra` field | Meaning |
|---|---|
| `cap` | Cap name (e.g. `"ws_max_frame_payload"`) |
| `requested` | Value the peer asked for (frame size, header bytes, …) |
| `limit` | Configured cap |
| `peer` | Peer `(host, port)` tuple when available |
| `scope_path` | ASGI `scope['path']` when available |
| `protocol` | `"http1"`, `"http2"`, `"ws"`, `"h2-ws"`, `"compression"`, … |

### Rate limiting

A single misbehaving peer cannot flood the log: each
`ConnectionActor` carries a `CapHitCounter` (installed via a
`contextvars`-bound context manager so every actor on the same
task tree picks it up without plumbing).  The first hit per
`(connection, cap)` logs in full; subsequent hits on the same
connection are silently counted.  When the connection closes, the
counter emits one summary record per suppressed cap:

```
cap hit summary: ws_max_frame_payload suppressed=99 more
```

### Subscribing

```python
import logging

class CapsHandler(logging.Handler):
    def emit(self, record):
        # Forward to your metrics pipeline, page on certain caps, etc.
        print(f"[CAP] {record.cap} requested={record.requested} "
              f"limit={record.limit} peer={record.peer}")

caps = logging.getLogger('blackbull.caps')
caps.addHandler(CapsHandler())
caps.setLevel(logging.WARNING)
```

In production set the level once at startup and route the records
to whatever observability surface you prefer (structured JSON to a
log aggregator, Prometheus counters via a custom handler, Sentry
breadcrumbs, …).  The `extra` payload is designed to round-trip
through `json.dumps(record.__dict__)` cleanly.

## Not yet implemented

- **WebSocket access logging** — connection-level entry (client
  IP, path, close code, duration).  Today the `websocket_*`
  events from [Events](events.md) cover the same data; build
  your own logger on top if you need persistence.

The access log covers both **HTTP/1.1 and HTTP/2** (one entry per
completed request / stream).

## Next

- [Events](events.md) — `@app.on('request_completed')` for
  per-request observability without touching the access log.
- [Configuration](configuration.md) — environment variables that
  affect logging (`BB_ACCESS_LOG`, `BB_ASYNC_LOGGING`).
