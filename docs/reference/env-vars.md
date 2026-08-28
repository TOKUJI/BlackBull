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
| `BB_CPU_PINNING` | `auto` | Per-worker CPU pinning, applied after fork.  `auto` gives worker *i* the *i*-th CPU of the mask the process already has, so `taskset` / `numactl` / a cpuset are honoured rather than overridden; `off` pins nothing; an explicit `taskset`-style list (`2,4,6-9`; `0` is CPU 0, not the off switch) confines workers to those CPUs, intersected with the mask granted to the process.  Only the event loop is pinned — the thread pool behind `run_in_executor` compression and `asyncio.to_thread` file reads keeps the full mask, since offloading exists to get off the loop's core.  Multi-worker and Linux only; a single-worker server is never pinned.  Set `off` on a shared or externally-orchestrated host. |

## Warm-up

Warm-up runs any hooks registered with `@app.on_warmup` **once, in the master, before the listening socket is created and before workers fork**, so every worker inherits the warmed heap (specialized bytecode, primed codecs/TLS) via copy-on-write.  These knobs only matter when the app registers at least one warm-up hook; with none, warm-up is a no-op.

| Variable | Default | Controls |
|---|---|---|
| `BB_WARMUP_BUDGET_S` | `60` | Hard wall-clock cap (seconds) on total warm-up.  A hook that overruns is cancelled and the master proceeds to bind — warm-up is best-effort and never blocks boot indefinitely. |
| `BB_WARMUP_TLS_N` | `64` | Number of in-memory (`ssl.MemoryBIO`) TLS handshakes the framework performs to prime the OpenSSL/RSA/ALPN path, when the listener terminates TLS.  Runs automatically after the app's own warm-up hooks; `0` disables it. |

## Connection limits and timeouts

Every cap in this section (plus the HTTP/2, WebSocket, and
Compression caps below) emits one `WARNING` record on the
`blackbull.caps` logger when it fires.  Subscribe to that logger to get a real-time signal
when a deployment hits its configured limits — see
[Logging](../guide/logging.md#cap-hit-log-blackbullcaps) for the
record shape, the inventory, and the per-connection
first-hit-then-summary rate-limit model.

| Variable | Default | Controls |
|---|---|---|
| `BB_MAX_CONNECTIONS` | `auto` | Maximum simultaneous TCP connections **per worker**.  At the cap, new connections receive HTTP/1.1 `503 Service Unavailable` with `Retry-After: 1` before close — a well-formed response so load-balancers and health-checks can interpret it correctly.  Accepts `auto`, `0` (uncapped), or a number.<br><br>**`auto` derives the cap from the process's own `RLIMIT_NOFILE`**, less a 64-descriptor reserve for listeners, the event loop's selector, log files and your application's own descriptors.  A cap above the fd budget would be decorative — `accept()` fails with `EMFILE` before the cap is ever consulted, and the peer gets a dropped connection instead of the 503 — so the derived value can only refuse connections the OS was going to refuse anyway.  That is what makes a finite default safe to ship, and it follows your own intent: raising the fd limit is how you say how large this process may become.  The resolved value is logged at startup.<br><br>An explicit number is honoured as given, not clamped to the fd budget.  Note the derived cap bounds *descriptor exhaustion*, not event-loop health — a ceiling reflecting what one asyncio loop serves well is a policy number that depends on your workload, so set it explicitly; 1024 is a typical single-loop value.  Multi-worker servers multiply the ceiling (`workers × max_connections`). |
| `BB_REQUEST_TIMEOUT` | `0` (off) | Per-HTTP/2-stream deadline in seconds.  When the deadline elapses the stream is forcibly cancelled with `RST_STREAM CANCEL`.  Use a positive value (e.g. `30`) in production to evict stalled handlers from stream slots. |
| `BB_HEADER_TIMEOUT` | `10.0` | Seconds an HTTP/1.1 client has to deliver the complete header block (request-line + headers + `CRLFCRLF`).  Primary slowloris defence — without it, an attacker can hold a connection open indefinitely by dripping bytes.  Server answers `408 Request Timeout` and closes.  Also bounds an HTTP/2 header block opened with HEADERS and never finished with END_HEADERS; there the answer is `GOAWAY(ENHANCE_YOUR_CALM)`, because HPACK state is connection-wide and a block whose bytes never arrived leaves the decoder unable to read any later one.  `0` disables. |
| `BB_BODY_TIMEOUT` | `30.0` | Per-chunk deadline for the request body once headers are parsed.  Slowloris body-half defence.  Each `await receive()` is bounded by this; exceed → the recipient surfaces `http.disconnect` and the connection tears down.  `0` disables. |
| `BB_WRITE_TIMEOUT` | `30.0` | Seconds the server will wait for a single response write to flush to the peer (via `StreamWriter.drain()`), or for one megabyte of a `sendfile` transfer to make progress.  Defends against the *slow-read* shape of slowloris: a client that reads the response 1 byte/sec eventually fills the kernel send buffer and our drain blocks indefinitely.  The bound is per write, not per response, so a legitimately large static file is never cut off for its size — only for stalling.  On timeout the transport is force-closed and the failure surfaces as a peer-side `ConnectionResetError` for the sender's existing error path.  On HTTP/2 the same knob bounds a second wait: a stream blocked on flow-control credit the peer never grants (the *data dribble* shape of CVE-2019-9511) gives up and sends `RST_STREAM(CANCEL)` rather than parking its task forever.  Enforced by the shared deadline scanner, so the drain timeout fires within `BB_DEADLINE_TICK_MS` of the requested instant rather than exactly on it — ~1 % slop at the 30 s default, and no per-response timer on the hot path.  `0` disables. |
| `BB_KEEP_ALIVE_TIMEOUT` | `5.0` | Seconds an idle HTTP/1.1 keep-alive connection is held open after a complete response.  Lower for high-fan-in deployments; higher for chatty clients on slow links. |
| `BB_TCP_USER_TIMEOUT_MS` | `0` (off, kernel default) | `TCP_USER_TIMEOUT` socket option (Linux).  Per-connection upper bound on how long an unacknowledged sent segment can linger before the kernel kills the connection.  Useful to evict dead peers behind NATs without waiting for keepalives.  See "Performance recommendations" below for production tuning. |
| `BB_HEADER_MAX_LINE` | `8192` | Maximum bytes in a single HTTP/1.1 request-line or header line.  Matches Apache `LimitRequestLine` / nginx `large_client_header_buffers`.  Exceeded → `431 Request Header Fields Too Large`. |
| `BB_HEADER_MAX_TOTAL` | `65536` | Maximum total bytes in the entire HTTP/1.1 header block.  Exceeded → `431`. |
| `BB_BODY_CHUNK_SIZE` | `65536` | Slice size (bytes) for a `Transfer-Encoding: chunked` request body: each chunk in progress is delivered in reads of at most this many bytes, so a peer-declared `chunk-size` never sets the read size.  64 KiB sits below the backpressure high-water mark, which is what lets the pause work.  The `Content-Length` path is transport-paced instead — its per-read bound is `BB_BODY_CHUNK_MAX`.  Must be `> 0` (invalid values fall back to the default). |
| `BB_BODY_CHUNK_MAX` | `524288` | Per-read bound (bytes) for a `Content-Length` request body.  Reads are up-to-n and transport-paced: each returns whatever the peer has delivered so far, up to this cap, and never blocks waiting to fill it — so a slow peer yields small slices (no read is a latency commitment `BB_BODY_TIMEOUT` might not deliver) while a fast one earns fewer, larger ones.  The cap is a memory bound, not a latency one: it limits how much a single read may materialise per connection.  Values below `BB_BODY_CHUNK_SIZE` are raised to it. |
| `BB_MAX_BODY_SIZE` | `31457280` (30 MiB) | Maximum total request-body octets accepted for one request, on **HTTP/1.1 and HTTP/2 alike**.  Without it the peer picks how much memory a request costs: `BB_BODY_CHUNK_MAX` bounds a single read, never the sum, and `conn.body()` accumulates whatever arrives.  A declared `Content-Length` over the cap is refused at head time, before a body octet is read; a `chunked` (H1) or undeclared (H2) body is refused the moment the running total passes it.  **HTTP/1.1** answers `413 Content Too Large` and **closes the connection** — the octets we declined are still arriving, so reading the next request out of them is the request-smuggling shape.  **HTTP/2** answers `413` + `RST_STREAM(NO_ERROR)` for a declared body (RFC 9113 §8.1) and `RST_STREAM(ENHANCE_YOUR_CALM)` for one discovered mid-stream; the connection survives, because every stream is framed explicitly.  30 MiB is the same class as Kestrel's `MaxRequestBodySize` (30,000,000 bytes = 28.6 MiB — near, not equal); nginx defaults to 1 MB, axum to 2 MB.  `0` disables the cap (uvicorn's behaviour — the app then owns the 413 decision). |
| `BB_MIN_BODY_RATE` | `240.0` | Minimum sustained request-body delivery rate in **bytes per second**; below it, past `BB_MIN_BODY_RATE_GRACE`, the request is abandoned (HTTP/1.1: the same `http.disconnect` + close as `BB_BODY_TIMEOUT`; HTTP/2: `RST_STREAM(ENHANCE_YOUR_CALM)`).  The rate is averaged over a sliding window one grace period wide — a peer that delivered early and then stalled is judged on the stalled window, not on the lifetime average, so a burst cannot shelter a drip.  This is the anti-trickle half of the body defence: a transport-paced read returns on *any* arrival, so `BB_BODY_TIMEOUT` degrades from "fill a slice in 30 s" to "send something every 30 s" — which a one-byte drip always satisfies, holding a connection open indefinitely.  A rate is what a drip cannot fake.  Matches Kestrel `MinRequestBodyDataRate`.  `0` disables the detector. |
| `BB_MIN_BODY_RATE_GRACE` | `5.0` | Seconds of body delivery before `BB_MIN_BODY_RATE` starts being enforced — the slow-start allowance, so nothing is judged on its first packets.  Also the width of the rate window the average is taken over.  What is measured differs by protocol, and deliberately: **HTTP/1.1** counts only time spent *waiting on the transport*, so a handler that writes each chunk to a slow disk is never mistaken for a slow peer; **HTTP/2** counts wall clock from the first DATA frame (frames arrive whether or not the handler reads), and instead exempts a peer that our own closed inbound window back-pressured. |
| `BB_STREAM_QUEUE_DEPTH` | `64` | `asyncio.Queue` depth for HTTP/2 per-stream request-body events.  Caps memory growth when an ASGI handler is slower than the client uploading data. |
| `BB_WS_QUEUE_DEPTH` | `0` | WebSocket inbound **read-ahead** depth.  `0` (default) reads frames inline, in the handler's own task, when it calls `receive()` — no reader task and no per-message queue hop.  A positive value restores a background reader that reads *ahead* of the handler into a queue of that depth, so control frames are serviced between `receive()` calls and up to `N` messages buffer under a slow handler.  Registering a `websocket_message` listener does not force read-ahead on: that event fires when the server reads rather than when the handler consumes, and a consuming handler is already reading, so the reader is only marked *deferred* and the idle watchdog starts it if the handler goes quiet.  See the [WebSocket guide](../guide/websockets.md). |

## Socket tuning

| Variable | Default | Controls |
|---|---|---|
| `BB_SOCKET_BACKLOG` | `1024` | `listen()` backlog depth.  A sane default for servers facing connection bursts (128 — the traditional `SOMAXCONN` — is shallow next to nginx's 511).  Linux caps the effective value at `net.core.somaxconn`.  See "Performance recommendations" below for production tuning. |
| `BB_SOCKET_REUSEPORT` | `0` (kernel default) | When supported by the OS (Linux, modern BSDs), bind each worker to its own listening socket so the kernel hashes incoming connections across workers — eliminates the thundering-herd accept pattern.  No effect with one worker.  Enable on multi-worker deployments. |
| `BB_SOCKET_SNDBUF` | `0` (kernel default) | `SO_SNDBUF` (bytes) on each accepted socket.  `0` leaves the kernel default unchanged.  Linux doubles the requested value internally; larger values help throughput for responses ≥ 64 kB. |
| `BB_SOCKET_RCVBUF` | `0` (kernel default) | `SO_RCVBUF` (bytes) on each accepted socket.  `0` leaves the kernel default unchanged.  Same doubling rule as `BB_SOCKET_SNDBUF`. |

## HTTP/1.1 internals

| Variable | Default | Controls |
|---|---|---|

## Logging

| Variable | Default | Controls |
|---|---|---|
| `BB_ACCESS_LOG` | `1` | Emit one record on the `blackbull.access` logger per completed request.  Set to `0` to skip access-log formatting (useful during benchmarks). |
| `BB_ASYNC_LOGGING` | `1` | Install a `QueueHandler` on the `blackbull` logger so `logger.debug/info` calls from the event loop are non-blocking. |
| `BB_LOG_FORMAT` | *(plain)* | Set to `json` to emit one structured JSON object per log line.  Access-log records expose `client_ip`, `method`, `path`, `http_version`, `status`, `response_bytes`, `duration_ms` (plus `close_code` on WebSocket disconnect) as top-level keys; every record carries `timestamp`, `level`, `logger`, `message`.  Applies to the default sink installed by async logging. |
| `BB_SYSLOG_ADDR` | *(unset)* | `host:port` of a syslog/UDP collector (e.g. `127.0.0.1:514`).  When set, the async-logging sink ships records via a UDP `SysLogHandler` instead of `stderr`.  Composes with `BB_LOG_FORMAT=json`.  An unparseable value falls back to `stderr` with a warning. |
| `BB_LOG_FILE` | *(unset)* | Path for the async-logging sink to write to (append mode) instead of `stderr`.  Composes with `BB_LOG_FORMAT=json` and `BB_LOG_BATCH_SIZE`.  Each worker opens its own append stream on the listener side (post-fork), so no writer thread is inherited across `fork()`; access-log lines (< `PIPE_BUF`) interleave atomically under `O_APPEND`.  Ignored for the syslog sink.  An unopenable path falls back to `stderr` with a warning. |
| `BB_LOG_BATCH_SIZE` | `64` | Coalescing width of the async-logging sink: up to this many formatted records are joined into a single `write()`+`flush()`. **Async logging is batch logging** — the stream/file sink always coalesces (floored at 2); a per-record flush is the dominant access-log cost (one flush syscall per request churns the GIL against the event loop — profiling showed ~16% CPU and a −44% throughput hit), so it is not an async option. A single flusher thread emits the batch when it fills or `BB_LOG_BATCH_TIMEOUT_MS` elapses. To force an immediate per-record flush, disable async logging (`BB_ASYNC_LOGGING=0`, the synchronous path) instead. Ignored for the syslog sink (UDP is one datagram per message). |
| `BB_LOG_BATCH_TIMEOUT_MS` | `5` | Max time a partial batch waits before it is flushed, bounding log-visibility latency at low request rates. |

The `blackbull.caps` logger has no env-var toggle — set its level
via `logging.getLogger('blackbull.caps').setLevel(...)` at
startup.  Default level is `WARNING`; raise to `ERROR` to silence
or drop to `INFO` to surface the rate-limit summary records.

## HTTP/2 internals

| Variable | Default | Controls |
|---|---|---|
| `BB_H2_INITIAL_WINDOW_SIZE` | `65535` (RFC 7540 §6.9.2 default) | Per-stream flow-control window advertised in the server's initial `SETTINGS` frame.  Larger lets peers send more data per stream before waiting for `WINDOW_UPDATE`.  See "Performance recommendations" below. |
| `BB_H2_CONNECTION_WINDOW_SIZE` | `65535` (RFC 7540 §6.9.2 minimum) | Connection-level flow-control window advertised via an initial `WINDOW_UPDATE` on stream 0.  Must be ≥ 65535; smaller values are silently ignored.  See "Performance recommendations" below. |
| `BB_H2_MAX_CONCURRENT_STREAMS` | `100` | `SETTINGS_MAX_CONCURRENT_STREAMS` (RFC 9113 §6.5.2 id `0x3`).  Streams beyond the cap receive `RST_STREAM REFUSED_STREAM` and are not dispatched. |
| `BB_WORKER_DRAIN_TIMEOUT` | `8.0` | Seconds a worker spends letting already-accepted connections finish after `SIGTERM`, before cancelling what is left.  Nothing in flight is cancelled while it lasts — a cancelled handler is a client holding a half-written response.  It sits inside the supervisor's own wait, so the drain ends in the worker rather than in a `SIGKILL`; raising it past that wait only moves the deadline.  `0` drops in-flight requests immediately, which is what the server did before this existed.  See [Shutdown](../deployment/workers.md#shutdown). |
| `BB_H2_ACTIVE_STREAMS` | `20` | Per-connection `asyncio.Semaphore` cap on stream handlers actually running concurrently, under multi-worker.  Prevents one high-mux connection from saturating a single event loop.  `0` disables (no cap beyond `BB_H2_MAX_CONCURRENT_STREAMS`). |
| `BB_H2_ACTIVE_STREAMS_1W` | `20` | Same as above, but used when `BB_WORKERS=1`. |
| `BB_FRAME_YIELD_EVERY` | `8` | Number of stream tasks spawned per connection before the frame loop inserts `await asyncio.sleep(0)`.  Caps the maximum synchronous run between yields under burst traffic.  `0` disables the cooperative yield (legacy behaviour). |

## WebSocket

| Variable | Default | Controls |
|---|---|---|
| `BB_WS_PERMESSAGE_DEFLATE` | `1` | Negotiate `permessage-deflate` (RFC 7692) on the inbound handshake when the peer offers it. |
| `BB_WS_MAX_FRAME_PAYLOAD` | `67108864` (64 MiB) | Maximum declared payload length of a single inbound frame, checked against the header **before** any payload byte is read — RFC 6455 §5.2 permits a peer to advertise 2<sup>63</sup>−1.  Exceeding it closes with 1009 (Message Too Big). |
| `BB_WS_MAX_MESSAGE_SIZE` | `16777216` (16 MiB) | Maximum size of a message **as your handler receives it** — after fragment reassembly and after decompression.  This is the bound the frame cap cannot express: permessage-deflate ratios reach 1028.8:1 in this tree, so a frame far under the frame cap can still inflate to gigabytes, and fragmentation accumulates frames that are each individually legal.  Exceeding it closes with 1009 and logs a `ws_max_message_size` cap hit.  `0` disables.  The default is the largest message the Autobahn suite sends, so conformance passes unconfigured; **lower it if your application does not serve huge messages** — at the ratio above, 16 MiB of server memory costs a peer roughly 16 KiB of bandwidth. |
| `BB_WS_IDLE_TIMEOUT` | `300.0` | Seconds of complete silence on a WebSocket connection before the server probes the peer with a PING (RFC 6455 §5.5.2).  **Same purpose and same default as `BB_H2_IDLE_TIMEOUT`** — an idle WebSocket is *normal*, since a subscription channel pushes nothing until something happens, so reaping on idleness alone would break the legitimate case.  Probing distinguishes *idle* from *gone*: a peer that answers is never closed, one that does not answer within `BB_WS_PONG_TIMEOUT` is closed with **1001 (Going Away)**.  **Any inbound frame counts as an answer**, not only a PONG — a peer that is talking to us is demonstrably alive.  This is the *time* bound on a WebSocket connection; the *unit* is `BB_WS_MAX_FRAME_PAYLOAD` and the *total* is `BB_WS_MAX_MESSAGE_SIZE` for a message and `BB_MAX_CONNECTIONS` for the connection.  Server-side only — the bundled clients do not probe.  `0` disables. |
| `BB_WS_PONG_TIMEOUT` | `30.0` | Seconds to wait for any inbound frame after a WebSocket liveness PING before concluding the peer is gone and closing with 1001.  Only meaningful when `BB_WS_IDLE_TIMEOUT` is non-zero.  As `BB_H2_PING_TIMEOUT`, and for the same reason. |
| `BB_H2_ENABLE_WEBSOCKET` | `0` | Advertise `SETTINGS_ENABLE_CONNECT_PROTOCOL=1` (RFC 8441 §3) so peers may bootstrap WebSocket over HTTP/2 via Extended CONNECT.  Off by default — this path has fewer conformance tests than the HTTP/1.1 Upgrade path and few clients use it.  When enabling this on a BlackBull instance that terminates TLS/HTTP/2 directly (no nginx / L7 proxy in front), also set `BB_MAX_CONNECTIONS` to a finite value and review `BB_H2_WS_MAX_STREAMS_PER_CONNECTION`.  The recommended production shape (nginx terminating TLS/HTTP/2, BlackBull behind it on HTTP/1.1) eliminates the RFC 8441 attack surface entirely because nginx does not forward Extended CONNECT to the backend. |
| `BB_FRAME_RATE_LIMIT` | `20` | Maximum number of each metered control frame a peer may send per `BB_FRAME_RATE_WINDOW`, **per type, per connection**.  Several attack shapes share one form — a frame cheap to send that obliges the server to a small piece of work — so no byte budget can see them and only a count can.  Metered: HTTP/2 `RST_STREAM` (CVE-2023-44487 Rapid Reset, inbound **and** server-emitted), `PING` (CVE-2019-9512), `SETTINGS` (CVE-2019-9515), zero-length `CONTINUATION`/`DATA` (CVE-2019-9518 — invisible to `BB_HEADER_MAX_TOTAL`, which counts bytes), and WebSocket control frames.  Each type has its own budget, so a peer may spend its allowance of PINGs *and* of SETTINGS without the two competing.  Over the budget: `GOAWAY(ENHANCE_YOUR_CALM)` on HTTP/2, close `1008` on WebSocket, plus a `frame_rate` cap hit.  `0` disables all frame-rate metering. |
| `BB_FRAME_RATE_WINDOW` | `1.0` | Width in seconds of the rolling window `BB_FRAME_RATE_LIMIT` counts within. |
| `BB_H2_IDLE_TIMEOUT` | `300.0` | Seconds of complete silence before the server probes the peer with a PING.  HTTP/2 connections are *meant* to be long-lived and idle — a browser holds one across a page's lifetime, a gRPC channel idles between calls — so this probes rather than reaps: a peer that answers is never closed, and any inbound frame counts as an answer.  `0` disables probing, leaving a silent connection bounded only by `BB_MAX_CONNECTIONS`. |
| `BB_H2_PING_TIMEOUT` | `30.0` | Seconds to wait for any frame after a liveness PING before concluding the peer is gone and closing with `GOAWAY(NO_ERROR)`.  Only meaningful when `BB_H2_IDLE_TIMEOUT` is non-zero. |
| `BB_H2_WS_MAX_STREAMS_PER_CONNECTION` | `5` | Maximum concurrent WebSocket (RFC 8441 Extended CONNECT) streams per HTTP/2 connection.  Caps the per-connection blast radius of WS-over-H2 stream-exhaustion attacks.  `0` disables the cap (no upper bound beyond `BB_H2_MAX_CONCURRENT_STREAMS`).  Only meaningful when `BB_H2_ENABLE_WEBSOCKET=1`. |

## MQTT

Every limit here is also **advertised** to the client in CONNACK where MQTT 5
has a property for it, so a conforming client stays inside the bounds without
ever meeting the enforcement path.

| Variable | Default | Controls |
|---|---|---|
| `BB_MQTT_MAX_PACKET_SIZE` | `1048576` (1 MiB) | Maximum size of one inbound control packet, advertised as `Maximum Packet Size` (§3.2.2.3.6).  Checked against the declared Remaining Length as soon as the fixed header is readable, so an over-size packet is refused **without buffering its payload** — MQTT 5 lets a peer declare 268,435,455 bytes (256 MiB) and then dribble them.  Over the cap the broker answers `DISCONNECT` with **0x95 (Packet Too Large)** and closes.  `0` disables. |
| `BB_MQTT_RECEIVE_MAXIMUM` | `64` | The broker's own `Receive Maximum` (§3.2.2.3.3): how many QoS>0 PUBLISH packets a client may have in flight towards the broker before waiting for acknowledgements.  **This is a promise a conforming client keeps, not a gate the broker closes** — nothing counts a non-conforming client's in-flight publishes against it; what bounds that direction is the 16-bit packet-identifier space and `BB_MQTT_MAX_PACKET_SIZE`.  The *client's* Receive Maximum, in the outbound direction, **is** enforced (see `BB_MQTT_MAX_QUEUED_MESSAGES`). |
| `BB_MQTT_MAX_QUEUED_MESSAGES` | `1000` | Per-session bound on QoS>0 messages held while the **client's** Receive Maximum window is full.  §4.9 forbids sending more than that many unacknowledged PUBLISH packets, so a client that subscribes and never acknowledges would otherwise make the broker hold every matching message for the life of its session.  At the bound the newest message is refused and a cap hit logged — the oldest are kept, because a subscriber is owed what it was promised first and has no way to detect a silently dropped message.  `0` disables. |
| `BB_MQTT_MAX_RETAINED` | `10000` | Maximum number of topics holding a retained message.  Retained messages are permanent by design, so one PUBLISH per topic grows broker memory forever without a bound.  At the cap a retained publish to a **new** topic is refused; updating or deleting an already-retained topic always works, so a client can never be locked out of correcting its own state, and the message is still delivered to current subscribers — only the storage is declined.  How the publisher learns depends on its QoS, because that decides whether the protocol has a channel for the answer: **QoS 1 and 2** get `0x97 (Quota Exceeded)` in the PUBACK/PUBREC; **QoS 0 is not told** — it has no acknowledgement (§3.3.4), and closing the connection over a storage quota would be disproportionate and would destroy a live delivery that succeeded.  Use QoS ≥ 1 if you need to know your retained state was stored.  The operator sees every refusal in `blackbull.caps` either way.  `0` disables. |
| `BB_MQTT_MAX_SUBSCRIPTIONS` | `1000` | Maximum Topic Filters one session may hold — the **unit** bound on session state, whose total is `BB_MQTT_MAX_SESSIONS` and whose time bound is the Session Expiry Interval the client declares.  Without it one connected client grows broker memory without limit, and with it the per-PUBLISH routing walk, since routing tests every filter of every connected session.  At the cap a **new** filter is refused with `0x97 (Quota Exceeded)` in the SUBACK and a cap hit logged; re-subscribing to a filter the session already holds always works, because §3.8.4 makes that a replacement rather than an addition, so it occupies no new slot.  `0` disables. |
| `BB_MQTT_MAX_SESSIONS` | `10000` | Maximum sessions the broker retains — the **total** bound on session state.  A session outlives its connection by design, and §3.1.2.11.2 defines `0xFFFFFFFF` as *never expires*, so a peer cycling Client Identifiers can pin one entry per identifier while breaking no rule.  At the cap a CONNECT for an **unknown** Client Identifier is refused with `0x97 (Quota Exceeded)` in the CONNACK and the connection closed; a client resuming a session the table already holds is admitted, because refusing it frees nothing.  Expired sessions are swept first, so the cap binds live state only.  `0` disables. |

## Compression

| Variable | Default | Controls |
|---|---|---|
| `BB_COMPRESSION_MIN_SIZE` | `100` | Minimum body size in bytes below which the `Compression` middleware skips compression entirely. |
| `BB_COMPRESSION_EXECUTOR_THRESHOLD` | `65536` (64 KiB) | Body size above which compression is offloaded to a thread-pool executor so the event loop stays responsive during the (CPU-bound) compress call.  `0` always compresses on the event loop. |
| `BB_COMPRESSION_MAX_INFLIGHT` | `os.cpu_count() * 2` | Maximum concurrent compression offloads to the asyncio default thread pool.  When at or above this cap, additional eligible responses are served **uncompressed** rather than queued — bounded fall-back rather than unbounded queue growth.  `0` disables the cap (unbounded queue, pre-0.29 behaviour). |
| `BB_BROTLI_QUALITY` | `4` | Brotli quality level (0–11) for dynamic-response compression.  4 matches Google/Cloudflare's recommendation for dynamic content; 5 matches Apache `mod_brotli`; 6 matches nginx `ngx_brotli`.  11 is appropriate only for build-time / static pre-compression — far too expensive on the request path. |

## Diagnostic timing

| Variable | Default | Controls |
|---|---|---|
| `BB_DEADLINE_TICK_MS` | `300` | Polling interval (milliseconds) for the per-process deadline scanner that enforces `BB_HEADER_TIMEOUT`, `BB_BODY_TIMEOUT`, `BB_WRITE_TIMEOUT`, and `BB_KEEP_ALIVE_TIMEOUT`.  One shared timer for the whole process instead of one per request, which is why enabling those timeouts costs nothing per request.  Smaller = tighter timeout granularity at a small CPU cost; larger = more slack but cheaper. |

## Performance recommendations

The defaults above match Linux kernel / RFC 7540 baselines so a
fresh BlackBull install behaves predictably regardless of host
tuning state.  On a busy production deployment — multi-worker,
high-fan-in, mixed HTTP/1.1 + HTTP/2 — the following values give
measurably better throughput and tail latency at the cost of more
kernel/process memory and one custom socket option:

| Variable | Default | Recommended | Why |
|---|---|---|---|
| `BB_SOCKET_BACKLOG` | `1024` | `4096` | Reduces silent connection drops during burst arrivals when the accept loop is briefly behind.  Effective value is capped by `net.core.somaxconn` — bump it too (`sysctl -w net.core.somaxconn=4096`). |
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
