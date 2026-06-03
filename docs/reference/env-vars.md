# Environment variables

Exhaustive table of `BB_*` and `BLACKBULL_*` environment
variables.  Defaults are conservative for development; raise the
limits and tune the sockets in production.

For the precedence order (CLI flags > env > TOML), see
[Configuration](../guide/configuration.md).

## Runtime and processes

| Variable | Default | Controls |
|---|---|---|
| `BLACKBULL_ENV` | `development` | `production` \| `development` \| `test`.  In `production`, `StaticFiles` declines to serve files (production should sit behind nginx/Caddy for static assets), and the default error handler returns a terse response without exception details. |
| `BB_WORKERS` | `1` | Pre-fork worker count.  `0` resolves to `os.cpu_count()`.  Each worker runs its own asyncio event loop; combine with `BB_SOCKET_REUSEPORT=1` so the kernel load-balances accepts across workers. |
| `BB_UVLOOP` | `0` | Install `uvloop`'s asyncio policy at startup.  Requires `pip install 'blackbull[speed]'`; falls back to the standard loop with a warning when uvloop is missing. |

## Connection limits and timeouts

| Variable | Default | Controls |
|---|---|---|
| `BB_MAX_CONNECTIONS` | `1024` | Maximum simultaneous TCP connections **per worker**.  When the cap is reached, new connections receive HTTP/1.1 `503 Service Unavailable` with `Retry-After: 1` before close — a well-formed response so load-balancers / health-checks can interpret it correctly.  `0` disables the cap (rely on OS file-descriptor limit instead).  Multi-worker servers multiply the ceiling (`workers × max_connections`). |
| `BB_REQUEST_TIMEOUT` | `0` (off) | Per-HTTP/2-stream deadline in seconds.  When the deadline elapses the stream is forcibly cancelled with `RST_STREAM CANCEL`.  Use a positive value (e.g. `30`) in production to evict stalled handlers from stream slots. |
| `BB_HEADER_TIMEOUT` | `10.0` | Seconds an HTTP/1.1 client has to deliver the complete header block (request-line + headers + `CRLFCRLF`).  Primary slowloris defence — without it, an attacker can hold a connection open indefinitely by dripping bytes.  Server answers `408 Request Timeout` and closes.  `0` disables. |
| `BB_BODY_TIMEOUT` | `30.0` | Per-chunk deadline for the request body once headers are parsed.  Slowloris body-half defence.  Each `await receive()` is bounded by this; exceed → the recipient surfaces `http.disconnect` and the connection tears down.  `0` disables. |
| `BB_WRITE_TIMEOUT` | `30.0` | Seconds the server will wait for a single response write to flush to the peer (via `StreamWriter.drain()`).  Defends against the *slow-read* shape of slowloris: a client that reads the response 1 byte/sec eventually fills the kernel send buffer and our drain blocks indefinitely.  On timeout the transport is force-closed and the failure surfaces as a peer-side `ConnectionResetError` for the sender's existing error path.  `0` disables. |
| `BB_KEEP_ALIVE_TIMEOUT` | `5.0` | Seconds an idle HTTP/1.1 keep-alive connection is held open after a complete response.  Lower for high-fan-in deployments; higher for chatty clients on slow links. |
| `BB_TCP_USER_TIMEOUT_MS` | `0` (off) | `TCP_USER_TIMEOUT` socket option (Linux).  Per-connection upper bound on how long an unacknowledged sent segment can linger before the kernel kills the connection.  Useful to evict dead peers behind NATs without waiting for keepalives. |
| `BB_HEADER_MAX_LINE` | `8192` | Maximum bytes in a single HTTP/1.1 request-line or header line.  Matches Apache `LimitRequestLine` / nginx `large_client_header_buffers`.  Exceeded → `431 Request Header Fields Too Large`. |
| `BB_HEADER_MAX_TOTAL` | `65536` | Maximum total bytes in the entire HTTP/1.1 header block.  Exceeded → `431`. |
| `BB_STREAM_QUEUE_DEPTH` | `64` | `asyncio.Queue` depth for HTTP/2 per-stream request-body events.  Caps memory growth when an ASGI handler is slower than the client uploading data. |
| `BB_WS_QUEUE_DEPTH` | `256` | `asyncio.Queue` depth for inbound WebSocket events per connection. |

## Socket tuning

| Variable | Default | Controls |
|---|---|---|
| `BB_SOCKET_BACKLOG` | `1024` | `listen()` backlog depth.  Reduces silent connection drops during burst traffic.  Linux caps the effective value at `net.core.somaxconn`. |
| `BB_SOCKET_REUSEPORT` | `1` | When supported by the OS (Linux, modern BSDs), bind each worker to its own listening socket so the kernel hashes incoming connections across workers — eliminates the thundering-herd accept pattern.  No effect with one worker. |
| `BB_SOCKET_SNDBUF` | `262144` | `SO_SNDBUF` (bytes) on each accepted socket.  Linux doubles the requested value internally.  Larger helps throughput for responses ≥ 64 kB.  `0` keeps the kernel default. |
| `BB_SOCKET_RCVBUF` | `262144` | `SO_RCVBUF` (bytes) on each accepted socket.  Same doubling rule.  `0` keeps the kernel default. |

## Logging

| Variable | Default | Controls |
|---|---|---|
| `BB_ACCESS_LOG` | `1` | Emit one record on the `blackbull.access` logger per completed request.  Set to `0` to skip access-log formatting (useful during benchmarks). |
| `BB_ASYNC_LOGGING` | `1` | Install a `QueueHandler` on the `blackbull` logger so `logger.debug/info` calls from the event loop are non-blocking. |

## HTTP/2 internals

| Variable | Default | Controls |
|---|---|---|
| `BB_H2_INITIAL_WINDOW_SIZE` | `1048576` (1 MiB) | Per-stream flow-control window advertised in the server's initial `SETTINGS` frame.  Larger lets peers send more data per stream before waiting for `WINDOW_UPDATE`. |
| `BB_H2_CONNECTION_WINDOW_SIZE` | `4194304` (4 MiB) | Connection-level flow-control window advertised via an initial `WINDOW_UPDATE` on stream 0.  Must be ≥ 65535 (the RFC default); smaller values are silently ignored. |
| `BB_H2_MAX_CONCURRENT_STREAMS` | `100` | `SETTINGS_MAX_CONCURRENT_STREAMS` (RFC 9113 §6.5.2 id `0x3`).  Streams beyond the cap receive `RST_STREAM REFUSED_STREAM` and are not dispatched. |
| `BB_H2_ACTIVE_STREAMS` | `20` | Per-connection `asyncio.Semaphore` cap on stream handlers actually running concurrently, under multi-worker.  Prevents one high-mux connection from saturating a single event loop.  `0` disables (no cap beyond `BB_H2_MAX_CONCURRENT_STREAMS`). |
| `BB_H2_ACTIVE_STREAMS_1W` | `20` | Same as above, but used when `BB_WORKERS=1`. |
| `BB_FRAME_YIELD_EVERY` | `8` | Number of stream tasks spawned per connection before the frame loop inserts `await asyncio.sleep(0)`.  Caps the maximum synchronous run between yields under burst traffic.  `0` disables the cooperative yield (legacy behaviour). |

## WebSocket

| Variable | Default | Controls |
|---|---|---|
| `BB_WS_PERMESSAGE_DEFLATE` | `1` | Negotiate `permessage-deflate` (RFC 7692) on the inbound handshake when the peer offers it. |
| `BB_H2_ENABLE_WEBSOCKET` | `0` | Advertise `SETTINGS_ENABLE_CONNECT_PROTOCOL=1` (RFC 8441 §3) so peers may bootstrap WebSocket over HTTP/2 via Extended CONNECT.  Off by default — this path has fewer conformance tests than the HTTP/1.1 Upgrade path and few clients use it. |

## Compression

| Variable | Default | Controls |
|---|---|---|
| `BB_COMPRESSION_MIN_SIZE` | `100` | Minimum body size in bytes below which the `Compression` middleware skips compression entirely. |
| `BB_COMPRESSION_EXECUTOR_THRESHOLD` | `65536` (64 KiB) | Body size above which compression is offloaded to a thread-pool executor so the event loop stays responsive during the (CPU-bound) compress call.  `0` always compresses on the event loop. |

## Sessions

| Variable | Default | Controls |
|---|---|---|
| `BB_SESSION_SECRET` | *(unset)* | HMAC secret used by the `Session` middleware to sign cookies.  Either pass `secret=` to the constructor or set this env var; if neither is set, construction raises (no insecure default). |

## Diagnostic timing

| Variable | Default | Controls |
|---|---|---|
| `BB_DEADLINE_TICK_MS` | `300` | Polling interval (milliseconds) for the per-process deadline scanner that enforces connection timeouts.  Smaller = tighter timeout granularity at a small CPU cost; larger = more slack but cheaper. |

## See also

- [Configuration](../guide/configuration.md) — how environment
  variables compose with TOML config files and CLI flags.
- [Logging](../guide/logging.md) — `BB_ACCESS_LOG` and
  `BB_ASYNC_LOGGING` semantics.
- [HTTP/2](../guide/http2.md) — what the `BB_H2_*` knobs control
  end-to-end.
