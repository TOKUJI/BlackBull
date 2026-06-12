# Environment variables

Exhaustive table of `BB_*` and `BLACKBULL_*` environment
variables.  Defaults match Linux kernel / RFC 7540 baselines so a
fresh BlackBull install behaves predictably regardless of host
tuning state.  For values that improve throughput / tail latency
on busy production deployments, see
[Performance recommendations](#performance-recommendations) below.

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
| `BB_REQUEST_TIMEOUT` | `0` (off) | Per-request handler deadline in seconds.  On HTTP/2 the stream is forcibly cancelled with `RST_STREAM CANCEL`; on HTTP/1.1 the server emits `408 Request Timeout` with `Connection: close` and closes the connection.  Use a positive value (e.g. `30`) in production to evict stalled handlers. |
| `BB_HEADER_TIMEOUT` | `10.0` | Seconds an HTTP/1.1 client has to deliver the complete header block (request-line + headers + `CRLFCRLF`).  Primary slowloris defence — without it, an attacker can hold a connection open indefinitely by dripping bytes.  Server answers `408 Request Timeout` and closes.  `0` disables. |
| `BB_BODY_TIMEOUT` | `30.0` | Per-chunk deadline for the request body once headers are parsed.  Slowloris body-half defence.  Each `await receive()` is bounded by this; exceed → the recipient surfaces `http.disconnect` and the connection tears down.  `0` disables. |
| `BB_WRITE_TIMEOUT` | `30.0` | Seconds the server will wait for a single response write to flush to the peer (via `StreamWriter.drain()`).  Defends against the *slow-read* shape of slowloris: a client that reads the response 1 byte/sec eventually fills the kernel send buffer and our drain blocks indefinitely.  On timeout the transport is force-closed and the failure surfaces as a peer-side `ConnectionResetError` for the sender's existing error path.  `0` disables. |
| `BB_KEEP_ALIVE_TIMEOUT` | `5.0` | Seconds an idle HTTP/1.1 keep-alive connection is held open after a complete response.  Lower for high-fan-in deployments; higher for chatty clients on slow links. |
| `BB_TCP_USER_TIMEOUT_MS` | `0` (off, kernel default) | `TCP_USER_TIMEOUT` socket option (Linux).  Per-connection upper bound on how long an unacknowledged sent segment can linger before the kernel kills the connection.  Useful to evict dead peers behind NATs without waiting for keepalives.  See "Performance recommendations" below for production tuning. |
| `BB_HEADER_MAX_LINE` | `8192` | Maximum bytes in a single HTTP/1.1 request-line or header line.  Matches Apache `LimitRequestLine` / nginx `large_client_header_buffers`.  Exceeded → `431 Request Header Fields Too Large`. |
| `BB_HEADER_MAX_TOTAL` | `65536` | Maximum total bytes in the entire HTTP/1.1 header block.  Exceeded → `431`. |
| `BB_STREAM_QUEUE_DEPTH` | `64` | `asyncio.Queue` depth for HTTP/2 per-stream request-body events.  Caps memory growth when an ASGI handler is slower than the client uploading data. |
| `BB_WS_QUEUE_DEPTH` | `256` | `asyncio.Queue` depth for inbound WebSocket events per connection. |

## Socket tuning

| Variable | Default | Controls |
|---|---|---|
| `BB_SOCKET_BACKLOG` | `128` (kernel `somaxconn` default) | `listen()` backlog depth.  Linux caps the effective value at `net.core.somaxconn`.  See "Performance recommendations" below for production tuning. |
| `BB_SOCKET_REUSEPORT` | `0` (kernel default) | When supported by the OS (Linux, modern BSDs), bind each worker to its own listening socket so the kernel hashes incoming connections across workers — eliminates the thundering-herd accept pattern.  No effect with one worker.  Enable on multi-worker deployments. |
| `BB_SOCKET_SNDBUF` | `0` (kernel default) | `SO_SNDBUF` (bytes) on each accepted socket.  `0` leaves the kernel default unchanged.  Linux doubles the requested value internally; larger values help throughput for responses ≥ 64 kB. |
| `BB_SOCKET_RCVBUF` | `0` (kernel default) | `SO_RCVBUF` (bytes) on each accepted socket.  `0` leaves the kernel default unchanged.  Same doubling rule as `BB_SOCKET_SNDBUF`. |

## Logging

| Variable | Default | Controls |
|---|---|---|
| `BB_ACCESS_LOG` | `1` | Emit one record on the `blackbull.access` logger per completed request.  Set to `0` to skip access-log formatting (useful during benchmarks). |
| `BB_ASYNC_LOGGING` | `1` | Install a `QueueHandler` on the `blackbull` logger so `logger.debug/info` calls from the event loop are non-blocking. |

## HTTP/2 internals

| Variable | Default | Controls |
|---|---|---|
| `BB_H2_INITIAL_WINDOW_SIZE` | `65535` (RFC 7540 §6.9.2 default) | Per-stream flow-control window advertised in the server's initial `SETTINGS` frame.  Larger lets peers send more data per stream before waiting for `WINDOW_UPDATE`.  See "Performance recommendations" below. |
| `BB_H2_CONNECTION_WINDOW_SIZE` | `65535` (RFC 7540 §6.9.2 minimum) | Connection-level flow-control window advertised via an initial `WINDOW_UPDATE` on stream 0.  Must be ≥ 65535; smaller values are silently ignored.  See "Performance recommendations" below. |
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
| `BB_BROTLI_QUALITY` | `4` | Brotli quality level (0–11) for dynamic-response compression.  4 matches Google/Cloudflare's recommendation for dynamic content; 5 matches Apache `mod_brotli`; 6 matches nginx `ngx_brotli`.  11 is appropriate only for build-time / static pre-compression — far too expensive on the request path. |

## Sessions

| Variable | Default | Controls |
|---|---|---|
| `BB_SESSION_SECRET` | *(unset)* | HMAC secret used by the `Session` middleware to sign cookies.  Either pass `secret=` to the constructor or set this env var; if neither is set, construction raises (no insecure default). |

## Diagnostic timing

| Variable | Default | Controls |
|---|---|---|
| `BB_DEADLINE_TICK_MS` | `300` | Polling interval (milliseconds) for the per-process deadline scanner that enforces connection timeouts.  Smaller = tighter timeout granularity at a small CPU cost; larger = more slack but cheaper. |

## Performance recommendations

The defaults above match Linux kernel / RFC 7540 baselines so a
fresh BlackBull install behaves predictably regardless of host
tuning state.  On a busy production deployment — multi-worker,
high-fan-in, mixed HTTP/1.1 + HTTP/2 — the following values give
measurably better throughput and tail latency at the cost of more
kernel/process memory and one custom socket option:

| Variable | Default | Recommended | Why |
|---|---|---|---|
| `BB_SOCKET_BACKLOG` | `128` | `4096` | Reduces silent connection drops during burst arrivals when the accept loop is briefly behind.  Effective value is capped by `net.core.somaxconn` — bump it too (`sysctl -w net.core.somaxconn=4096`). |
| `BB_SOCKET_REUSEPORT` | `0` | `1` | When running > 1 worker on Linux, lets the kernel hash incoming connections across workers instead of a single accept loop fanning them out.  Eliminates thundering-herd and improves CPU affinity. |
| `BB_SOCKET_SNDBUF` | `0` | `262144` | 256 kB requested → ~512 kB effective after the kernel doubles.  Helps throughput on responses ≥ 64 kB (static assets, JSON arrays, streamed bodies). |
| `BB_SOCKET_RCVBUF` | `0` | `262144` | Same shape as `SNDBUF`, for inbound traffic (large `POST` bodies). |
| `BB_TCP_USER_TIMEOUT_MS` | `0` | `60000` | Linux `TCP_USER_TIMEOUT`.  Forces the kernel to drop connections where the peer hasn't ACKed a sent segment within the window.  Evicts dead peers behind NATs / load balancers faster than keepalives. |
| `BB_H2_INITIAL_WINDOW_SIZE` | `65535` | `1048576` (1 MiB) | RFC 7540's 64 kB per-stream window is small for modern broadband; 1 MiB lets peers send a respectable chunk before they have to wait for a `WINDOW_UPDATE`. |
| `BB_H2_CONNECTION_WINDOW_SIZE` | `65535` | `4194304` (4 MiB) | Same logic at the connection level — letting multiple concurrent streams share a 4 MiB connection budget reduces head-of-line stalls when one stream's flow control is tight. |

For containerised deployments, set the socket-buffer values via
the container environment (`docker run -e BB_SOCKET_SNDBUF=...`)
since the host's `net.ipv4.tcp_wmem` won't apply inside the
container's network namespace.

For benchmarks that compare BlackBull on its own terms (no
peer-framework framing), leave the defaults alone — RFC / kernel
baselines are the right starting point and tuning above them is a
deployment concern, not a framework one.

## See also

- [Configuration](../guide/configuration.md) — how environment
  variables compose with TOML config files and CLI flags.
- [Logging](../guide/logging.md) — `BB_ACCESS_LOG` and
  `BB_ASYNC_LOGGING` semantics.
- [HTTP/2](../guide/http2.md) — what the `BB_H2_*` knobs control
  end-to-end.
