"""Runtime configuration sourced from environment variables.

All server settings live in [`Settings`][].  Retrieve the current
configuration with [`get_settings`][], which reads environment variables
once and returns an immutable snapshot.

Environment variables
---------------------
BLACKBULL_ENV
    ``production`` | ``development`` | ``test``.  An unrecognised value falls
    back to ``development``.
    Default: ``development``.
BB_WORKERS
    Number of worker processes.  ``0`` resolves to ``os.cpu_count()``.
    Default: ``1``.
BB_WORKER_DRAIN_TIMEOUT
    Seconds a worker lets already-accepted connections finish after SIGTERM
    before cancelling what is left.  Sits inside the supervisor's own wait,
    so raising it past that wait moves the deadline without extending it.
    ``0`` drops in-flight requests immediately.
    Default: ``8.0``.
BB_MAX_CONNECTIONS
    Maximum simultaneous TCP connections accepted per worker.  When the
    cap is reached, new connections receive HTTP/1.1 ``503 Service
    Unavailable`` with ``Retry-After: 1`` (a load-balancer-friendly
    response, not a silent reset).
    Accepts ``auto`` (the default), ``0`` to disable the cap, or an
    explicit number.
    **``auto`` derives the cap from this process's own ``RLIMIT_NOFILE``**,
    less a small reserve for listeners, the event loop's selector, log
    files and the application's own descriptors.  A cap above the fd
    budget would be decorative — ``accept()`` fails with ``EMFILE`` before
    the cap is consulted, and the peer gets a dropped connection instead
    of the 503 — so the derived value can only refuse connections the OS
    was going to refuse anyway.  That is what makes a finite default safe
    to ship, and it follows the operator's own intent: raising the fd
    limit is how you say how large this process may become.  The resolved
    value is logged at startup, because a derived default nobody can see
    is a default nobody can size.
    An explicit number is honoured as given, not clamped to the fd budget.
    Note this bounds *descriptor exhaustion*, not event-loop health: a
    ceiling reflecting what one asyncio loop serves well is a policy
    number that depends on the workload — set it explicitly; 1024 is a
    typical single-loop value.  Multi-worker deployments multiply (so
    ``workers=8`` × ``BB_MAX_CONNECTIONS=1024`` → 8K per process).
    Default: ``auto``.
BB_STREAM_QUEUE_DEPTH
    ``asyncio.Queue`` depth for HTTP/2 per-stream request-body events.
    Limits memory growth when an ASGI handler is slower than the client.
    Default: ``64``.
BB_WS_QUEUE_DEPTH
    WebSocket inbound read-ahead depth.  ``0`` reads frames inline in the
    handler's own task — no reader task and no per-message queue hop.  A
    positive value runs a background reader that reads *ahead* of the
    handler into an ``asyncio.Queue`` of that depth, which buys
    control-frame servicing between the handler's ``receive()`` calls.
    Default: ``0``.
BB_ASYNC_LOGGING
    ``1`` | ``true`` | ``yes`` to enable; ``0`` | ``false`` | ``no`` to disable.
    When enabled, a ``QueueHandler`` is installed on the ``blackbull`` logger
    so that ``logger.debug/info`` calls in the event loop are non-blocking.
    Default: ``true``.
BB_ACCESS_LOG
    ``1`` | ``true`` | ``yes`` to enable; ``0`` | ``false`` | ``no`` to disable.
    When disabled, the ``blackbull.access`` logger is silenced (level set to
    WARNING) so no access log records are formatted or emitted.  Useful in
    production where a separate log aggregator consumes structured logs and the
    per-request overhead of the access logger is undesirable.
    Default: ``true``.
BB_LOG_FORMAT
    Async-logging sink format.  ``json`` emits one structured JSON object per
    line; anything else (default) keeps plain text.
    Default: `` `` (plain).
BB_SYSLOG_ADDR
    ``host:port`` of a syslog/UDP collector (e.g. ``127.0.0.1:514``).  When set,
    the async-logging sink ships records via a UDP ``SysLogHandler`` instead of
    ``stderr``.  Composes with ``BB_LOG_FORMAT=json``.
    Default: `` `` (stderr sink).
BB_LOG_FILE
    Path the async-logging sink appends to instead of ``stderr``.  Each
    worker opens its own append stream post-fork, so no writer thread is
    inherited across ``fork()``.  Composes with ``BB_LOG_FORMAT`` and
    ``BB_LOG_BATCH_SIZE``; ignored for the syslog sink.  An unopenable path
    falls back to ``stderr`` with a warning.
    Default: `` `` (stderr sink).
BB_LOG_BATCH_SIZE
    Coalescing width of the ``stderr``/file async-logging sink: up to this
    many formatted lines are joined into a single ``write()``.  Async
    logging *is* batch logging — the sink always coalesces (floored at 2),
    because a per-record flush is the dominant access-log cost.  To get one
    write per record, disable async logging (``BB_ASYNC_LOGGING=0``, the
    synchronous path) rather than lowering this.  Ignored for the syslog
    sink.
    Default: ``64``.
BB_LOG_BATCH_TIMEOUT_MS
    Max milliseconds a partial log batch waits before flush.  Only meaningful
    when ``BB_LOG_BATCH_SIZE`` > 1.
    Default: ``5``.
BB_SOCKET_BACKLOG
    ``listen()`` backlog depth for the server socket.  Increasing this reduces
    silent connection drops during burst traffic when the accept loop falls
    behind.  Capped by ``net.core.somaxconn`` on Linux.
    Default: ``1024`` — sized for servers facing connection bursts, since
    128 (the traditional ``SOMAXCONN``) is shallow next to nginx's and
    Node's 511.  Bump to 4096 for production traffic — see
    docs/reference/env-vars.md "Performance recommendations".
BB_SOCKET_SNDBUF
    Kernel send-buffer size (bytes) set on each accepted TCP socket via
    ``SO_SNDBUF``.  The kernel doubles the requested value internally.
    Larger values improve throughput for large responses (≥64 kB).
    ``0`` leaves the kernel default unchanged.
    Default: ``0`` (kernel default).  ``262144`` (256 kB) is a common
    production value — see docs/reference/env-vars.md.
BB_SOCKET_RCVBUF
    Kernel receive-buffer size (bytes) set on each accepted TCP socket via
    ``SO_RCVBUF``.  Same doubling rule as ``BB_SOCKET_SNDBUF``.
    ``0`` leaves the kernel default unchanged.
    Default: ``0`` (kernel default).  ``262144`` (256 kB) is a common
    production value — see docs/reference/env-vars.md.
BB_SOCKET_REUSEPORT
    ``1`` | ``true`` | ``yes`` to enable; ``0`` | ``false`` | ``no`` to disable.
    When enabled and the OS supports ``SO_REUSEPORT``, each worker binds its own
    listening socket so the kernel distributes incoming connections across workers
    independently, eliminating thundering-herd and improving CPU affinity.
    Has no effect with a single worker or on platforms without ``SO_REUSEPORT``.
    Default: ``false`` (kernel default).  Enable on multi-worker production
    deployments — see docs/reference/env-vars.md.
BB_KEEP_ALIVE_TIMEOUT
    Idle timeout (seconds) on a keep-alive HTTP/1.1 connection that is
    awaiting the *next* request.  Application-level timer; same
    ghost-eviction guarantee as ``SO_KEEPALIVE`` without the per-accept
    syscall cost.  ``0`` disables the timer.  Default: ``5.0``.
BB_TCP_USER_TIMEOUT_MS
    ``TCP_USER_TIMEOUT`` value in **milliseconds** for accepted sockets
    (Linux only).  Forces a connection-level error if a peer fails to
    ACK in this window — defends against dead-mid-write peers that
    ``SO_KEEPALIVE`` misses.  ``0`` leaves the kernel default
    unchanged.  Default: ``0``.
BB_HEADER_TIMEOUT
    Maximum seconds the server will wait for a complete HTTP/1.1
    request-header block (request-line + headers + CRLFCRLF).  Primary
    slowloris defence.  When the deadline elapses the server returns
    ``408 Request Timeout`` and closes.  ``0`` disables.
    Default: ``10.0``.
BB_BODY_TIMEOUT
    Maximum seconds for the HTTP/1.1 request body to arrive once headers
    are parsed.  Mirrors ``BB_HEADER_TIMEOUT`` for the body half;
    defeats slowloris-style ``Content-Length: N`` connections that drip
    body bytes after the headers have arrived.  ``0`` disables.
    Default: ``30.0``.
BB_WRITE_TIMEOUT
    Maximum seconds the server will wait for a single write to flush to
    the peer (via ``StreamWriter.drain()``).  Defends against the
    *slow-read* shape of slowloris: a client that reads 1 byte/sec
    eventually fills the kernel send buffer and ``drain()`` would block
    indefinitely.  ``0`` disables.  Default: ``30.0``.
BB_REQUEST_TIMEOUT
    Maximum seconds a single request handler is allowed to run.  Applied on
    both protocols: HTTP/2 cancels the stream with RST_STREAM CANCEL; HTTP/1.1
    emits ``408 Request Timeout`` with ``Connection: close`` and closes the
    connection (no keep-alive across a timed-out request).  Prevents slow or
    stalled handlers from holding stream / connection slots indefinitely.
    ``0`` disables the timeout.  Default: ``0`` (disabled).
BB_HEADER_MAX_LINE
    Maximum bytes in a single HTTP/1.1 request-line or header line.
    Enforced before parsing so an attacker cannot exhaust memory with a
    pathological 1 GB header.  Default: ``8192`` (matches Apache
    ``LimitRequestLine`` / nginx ``large_client_header_buffers``).
BB_HEADER_MAX_TOTAL
    Maximum total bytes in the entire HTTP/1.1 request header block
    (request-line + all headers + CRLFCRLF).  Default: ``65536``
    (matches typical reverse-proxy defaults).
BB_BODY_CHUNK_SIZE
    Slice size (bytes) for streaming an HTTP/1.1 ``Content-Length`` request
    body to the ASGI app as successive ``http.request`` events instead of one
    giant allocation.  Default: ``65536`` (asyncio's ``StreamReader`` buffer).
    Must be ``> 0``.
BB_BODY_CHUNK_MAX
    Upper bound (bytes) on one such read.  Reads are up-to-n and
    transport-paced, so a slow peer yields small slices while a fast one
    earns fewer, larger ones; this caps what a single read may materialise
    per connection.  Bounds the ``Content-Length`` framing only —
    ``BB_BODY_CHUNK_SIZE`` is the ``chunked`` path's slice, and the two are
    never compared against each other.  ``0`` is raised to ``1``, since an
    up-to-zero read returns ``b''`` and would be indistinguishable from EOF.
    Default: ``524288`` (512 KiB).
BB_MAX_BODY_SIZE
    Maximum total request-body octets accepted for one request — the total
    that ``BB_BODY_CHUNK_MAX``, a per-read bound, does not own.  Over the cap
    the server answers ``413 Content Too Large`` and closes: a declared
    ``Content-Length`` is refused at head time, a ``chunked`` body the moment
    the running total passes it.  The connection always closes on a refusal,
    since parsing attacker-chosen unread octets as the next request is the
    request-smuggling shape.  ``0`` disables the cap.
    Default: ``31457280`` (30 MiB) — the same class as Kestrel's
    ``MaxRequestBodySize``; nginx defaults to 1 MB, axum to 2 MB.
BB_MIN_BODY_RATE
    Minimum sustained request-body delivery rate in **bytes per second**,
    averaged over a sliding window one grace period wide.  A transport-paced
    read cannot be the anti-trickle bound: ``BB_BODY_TIMEOUT`` degrades to
    "send *something* every N seconds", which a one-byte drip satisfies, and
    a rate is what a drip cannot fake.  ``0`` disables the detector.
    Default: ``240.0`` (with the 5 s grace, Kestrel's
    ``MinRequestBodyDataRate`` defaults).
BB_MIN_BODY_RATE_GRACE
    Seconds of body-read waiting before ``BB_MIN_BODY_RATE`` is enforced, so
    a connection is never judged on its first few packets.  Only time spent
    waiting on the transport counts, never time the handler spent between
    reads.
    Default: ``5.0``.
BB_H2_INITIAL_WINDOW_SIZE
    Per-stream flow-control window size (bytes) advertised to HTTP/2 peers in the
    server's initial SETTINGS frame.  Larger values allow peers to send more data
    per stream before waiting for WINDOW_UPDATE.
    Default: ``65535`` (RFC 9113 §6.9.2 default).  ``1048576`` (1 MiB) is a
    common tuned value for upload-heavy or multiplexed workloads — see
    docs/reference/env-vars.md.
BB_H2_CONNECTION_WINDOW_SIZE
    Connection-level flow-control window size (bytes) advertised to HTTP/2 peers
    via an initial WINDOW_UPDATE on stream 0 after the SETTINGS handshake.
    Must be ≥ 65535 (the RFC default); values below that are silently ignored.
    Default: ``65535`` (RFC 9113 §6.9.2 minimum).  ``4194304`` (4 MiB) is a
    common tuned value to allow concurrent streams to share the connection
    budget without head-of-line stalls — see docs/reference/env-vars.md.
BB_H2_MAX_CONCURRENT_STREAMS
    Maximum number of HTTP/2 streams the server accepts at the same time per
    connection, advertised to peers in the initial SETTINGS frame
    (RFC 9113 §6.5.2 — SETTINGS_MAX_CONCURRENT_STREAMS, identifier 0x0003).
    Incoming streams that would exceed this limit receive RST_STREAM
    REFUSED_STREAM and are not dispatched to the application.
    Default: ``100``.
BB_H2_ACTIVE_STREAMS_1W
    Per-connection ``asyncio.Semaphore`` cap on running stream handlers
    when ``workers == 1``.  Counterpart of ``BB_H2_ACTIVE_STREAMS`` for
    the single-worker case (where one event loop sees all connections).
    ``0`` disables the cap.  Default: ``20``.
BB_H2_ACTIVE_STREAMS
    Per-connection ``asyncio.Semaphore`` cap on running stream handlers
    when ``workers > 1``.  Newly-spawned stream tasks queue for the
    semaphore instead of running immediately, which prevents one high-mux
    connection from monopolising the event loop and starving other
    connections on the same worker.  ``0`` disables the cap (no upper
    bound beyond ``BB_H2_MAX_CONCURRENT_STREAMS``).  Default: ``20``.
BB_H2_ENABLE_WEBSOCKET
    Advertise ``SETTINGS_ENABLE_CONNECT_PROTOCOL=1`` (RFC 8441 §3) so
    peers may bootstrap WebSocket over HTTP/2 via Extended CONNECT.
    Off by default — this path has fewer conformance tests than the
    HTTP/1.1 upgrade path.  Default: ``false``.
BB_H2_WS_MAX_STREAMS_PER_CONNECTION
    Maximum concurrent WebSocket (RFC 8441 Extended CONNECT) streams
    per HTTP/2 connection.  ``0`` disables the per-connection cap (no
    upper bound beyond ``BB_H2_MAX_CONCURRENT_STREAMS``).  Only
    meaningful when ``BB_H2_ENABLE_WEBSOCKET=1`` — without that, no
    WS-over-H2 streams are accepted at all.  Defends against
    stream-exhaustion DoS: without a per-connection cap, an attacker
    can hold ``BB_H2_MAX_CONCURRENT_STREAMS`` idle WS streams open
    per connection, multiplied by ``BB_MAX_CONNECTIONS``.
    Default: ``5``.
BB_WS_PERMESSAGE_DEFLATE
    Negotiate ``permessage-deflate`` (RFC 7692) on incoming WebSocket
    handshakes when the peer offers it.  Matches modern browsers and
    major WebSocket libraries.  Default: ``true``.
BB_WS_MAX_FRAME_PAYLOAD
    Hard cap on the declared payload length (bytes) of a single
    inbound WebSocket frame.  RFC 6455 §5.2 allows up to 2**63 - 1; an
    adversary post-handshake could advertise that to OOM the server
    before any body bytes arrive.  This cap is enforced on the
    declared length in the frame header (before reading bytes off the
    wire) and triggers ``CLOSE`` with status code 1009 (MESSAGE_TOO_BIG)
    when exceeded.  Default: ``67108864`` (64 MiB) — comfortably above
    the largest frame the Autobahn|Testsuite sends (16 MiB, case 9.1.6)
    while still bounding per-connection memory use.  Lower for stricter
    exposure (e.g. ``1048576`` for 1 MiB matching the
    ``python-websockets`` default).  This bounds the frame *as it
    arrives on the wire*; what the application is handed after
    reassembly and inflation is bounded by ``BB_WS_MAX_MESSAGE_SIZE``.
BB_WS_MAX_MESSAGE_SIZE
    Maximum size (bytes) of a WebSocket message **as the application
    receives it** — after fragment reassembly and after
    permessage-deflate inflation.  This is the bound
    ``BB_WS_MAX_FRAME_PAYLOAD`` cannot express: that one caps a single
    compressed frame on the wire, and deflate ratios in this tree
    measure 1028.8:1, so a frame at that cap inflates to ~64 GiB with
    nothing between the peer and the allocator.  Fragmentation is the
    same defect without the compression: N frames each under the frame
    cap accumulate with no total.
    Exceeding it closes with 1009 (MESSAGE_TOO_BIG, RFC 6455 §7.4.1) and
    logs a ``ws_max_message_size`` cap hit.  ``0`` disables the cap.
    Default: ``16777216`` (16 MiB) — the largest message the
    Autobahn|Testsuite sends (9.1.6 text / 9.2.6 binary), so the suite
    stays green on shipped defaults.  An application that does not serve
    huge messages should lower this: at the measured ratio a peer still
    buys 16 MiB of server memory for ~16 KiB of upstream bandwidth.
BB_FRAME_RATE_LIMIT
    Maximum number of each metered control frame a peer may send per
    ``BB_FRAME_RATE_WINDOW``, **per type, per connection**.  Several
    attack shapes share one form: a frame that is cheap to send and
    obliges the server to a small piece of work per frame, so no byte
    budget can see them and only a count can.  Metered:
    HTTP/2 ``RST_STREAM`` (CVE-2023-44487 Rapid Reset — inbound *and*
    server-emitted), ``PING`` (CVE-2019-9512), ``SETTINGS``
    (CVE-2019-9515), zero-length ``CONTINUATION``/``DATA``
    (CVE-2019-9518 — invisible to ``BB_HEADER_MAX_TOTAL``, which counts
    bytes), and WebSocket control frames.
    Each type gets its own budget, so a peer may legitimately spend its
    allowance of PINGs *and* of SETTINGS without the two competing.
    Exceeding it closes the connection (``GOAWAY(ENHANCE_YOUR_CALM)`` on
    HTTP/2, close ``1008`` on WebSocket) and logs a ``frame_rate`` cap
    hit naming the frame type.  ``0`` disables all frame-rate metering.
    Default: ``20`` — generous for legitimate peers (browser navigation
    plus prefetch cancellation rarely exceeds ~10 RST/s) and limiting for
    the attack shapes, which run to thousands per second.
BB_FRAME_RATE_WINDOW
    Width in seconds of the rolling window ``BB_FRAME_RATE_LIMIT``
    counts within.  Default: ``1.0``.
BB_H2_IDLE_TIMEOUT
    Seconds of complete silence on an HTTP/2 connection before the
    server probes the peer with a PING.  HTTP/2 connections are *meant*
    to be long-lived and idle — a browser holds one across a page's
    lifetime and a gRPC channel idles between calls — so reaping on
    idleness alone would break both.  Probing distinguishes *idle* from
    *gone*: a peer that answers is never closed, and one that does not
    answer within ``BB_H2_PING_TIMEOUT`` gets ``GOAWAY(NO_ERROR)`` and a
    close.  Any inbound frame counts as an answer.  ``0`` disables the
    probe entirely, leaving a silent connection bounded only by
    ``BB_MAX_CONNECTIONS``.  Default: ``300.0`` (5 minutes).
BB_H2_PING_TIMEOUT
    Seconds to wait for any frame after a liveness PING before
    concluding the peer is gone and closing with ``GOAWAY(NO_ERROR)``.
    Only meaningful when ``BB_H2_IDLE_TIMEOUT`` is non-zero.
    Default: ``30.0``.
BB_WS_IDLE_TIMEOUT
    Seconds of complete silence on a WebSocket connection before the
    server probes the peer with a PING (RFC 6455 §5.5.2).  **Same
    purpose, same triad column and same default as
    ``BB_H2_IDLE_TIMEOUT``** — an idle WebSocket is *normal*, since a
    subscription channel pushes nothing until something happens, so
    reaping on idleness alone would break the legitimate case.  Probing
    distinguishes *idle* from *gone*: a peer that answers is never
    closed, and one that does not answer within ``BB_WS_PONG_TIMEOUT``
    is closed with ``1001 (Going Away)``.  Any inbound frame counts as
    an answer, not only a PONG — a peer that is talking to us is
    demonstrably alive.  This is the *time* column for a WebSocket
    connection; the *unit* is ``BB_WS_MAX_FRAME_PAYLOAD`` and the
    *total* is ``BB_WS_MAX_MESSAGE_SIZE`` for the message and
    ``BB_MAX_CONNECTIONS`` for the connection.  ``0`` disables the
    probe, leaving a silent connection bounded only by
    ``BB_MAX_CONNECTIONS``.  Default: ``300.0`` (5 minutes).
BB_WS_PONG_TIMEOUT
    Seconds to wait for any inbound frame after a liveness PING before
    concluding the peer is gone and closing with ``1001``.  Only
    meaningful when ``BB_WS_IDLE_TIMEOUT`` is non-zero — as
    ``BB_H2_PING_TIMEOUT``, and for the same reason.
    Default: ``30.0``.
BB_MQTT_MAX_PACKET_SIZE
    Maximum size (bytes) of a single inbound MQTT control packet,
    advertised to clients as the ``Maximum Packet Size`` property in
    CONNACK (§3.2.2.3.6) so a conforming client never sends one.  The
    check runs on the declared Remaining Length as soon as the fixed
    header is readable, so an over-size packet is refused **without
    buffering its payload** — MQTT 5 permits a peer to declare
    268,435,455 bytes (256 MiB) and dribble them.  Over the cap the
    broker answers ``DISCONNECT`` with reason code **0x95 (Packet Too
    Large)** and closes.  ``0`` disables the cap.
    Default: ``1048576`` (1 MiB) — MQTT payloads are overwhelmingly small,
    so a limit that admits a megabyte still admits every realistic message
    while refusing the spec ceiling.
BB_MQTT_RECEIVE_MAXIMUM
    The broker's own ``Receive Maximum`` (§3.2.2.3.3), advertised in
    CONNACK: how many QoS>0 PUBLISH packets a client may have in flight
    towards the broker before it must wait for acknowledgements.
    **This is a promise a conforming client keeps, not a gate the broker
    closes** — nothing counts a non-conforming client's in-flight
    publishes against it.  What bounds that direction is the 16-bit
    packet-identifier space and ``BB_MQTT_MAX_PACKET_SIZE``.  The
    *client's* Receive Maximum, in the outbound direction, **is**
    enforced — see ``BB_MQTT_MAX_QUEUED_MESSAGES``.  Default: ``64``.
BB_MQTT_MAX_QUEUED_MESSAGES
    Maximum QoS>0 messages held per session while the client's own
    ``Receive Maximum`` window is full.  MQTT 5 §4.9 forbids sending
    more than that many unacknowledged PUBLISH packets, so a client that
    subscribes and never acknowledges would otherwise make the broker
    hold every matching message for the life of the session.  Beyond
    this bound the newest message is **refused** rather than an older one
    silently discarded, and a cap hit is logged.  ``0`` disables the
    bound (unbounded backlog — not recommended on an exposed broker).
    Default: ``1000``.
BB_MQTT_BROKER_INBOX_MAXSIZE
    Waiting messages in the worker's MQTT broker inbox. At capacity, readers
    await admission instead of decoding further packets. Positive integer,
    independent of the per-session QoS backlog.
    Default: ``1024``.
BB_MQTT_BROKER_INBOX_MAX_BYTES
    Wire-size charge for waiting broker messages; positive integer. A packet
    exceeding this budget cannot wait for admission and its connection is
    refused. Not a Python heap limit.
    Default: ``16777216`` (16 MiB).
BB_MQTT_CONNECTION_INBOX_MAXSIZE
    Waiting packets in each MQTT connection writer inbox, including QoS 0
    and control replies; positive integer. After yielding to the writer, a
    full inbox ends only that connection, with a cap log.
    Default: ``1024``.
BB_MQTT_CONNECTION_INBOX_MAX_BYTES
    Encoded bytes waiting in each MQTT writer inbox; positive integer. The
    active write is outside the queue budget; its duration is bounded by
    ``BB_WRITE_TIMEOUT`` when enabled.
    Default: ``16777216`` (16 MiB).
BB_MQTT_MAX_RETAINED
    Maximum number of distinct topics holding a retained message
    (§3.3.1.3).  A retained message is permanent by design, so without a
    bound one PUBLISH per topic grows broker memory forever.  At the cap
    a retained publish to a **new** topic is refused and logged;
    updating or deleting an already-retained topic always works, so a
    client can never be locked out of correcting its own state.  The
    message is still delivered to current subscribers — only the storage
    is declined.
    How the publisher learns depends on the QoS it chose, because that is
    what decides whether the protocol has a channel for the answer:
    **QoS 1 and 2** receive ``0x97 (Quota Exceeded)`` in the PUBACK or
    PUBREC; **QoS 0 is not told at all** — it has no acknowledgement
    (§3.3.4), and closing the connection over a storage quota would be
    disproportionate and would also destroy a live delivery that
    succeeded.  A publisher that needs to know its retained state was
    stored must use QoS ≥ 1.  The operator sees every refusal in the
    ``blackbull.caps`` log regardless.  ``0`` disables the cap.
    Default: ``10000``.
BB_MQTT_MAX_SUBSCRIPTIONS
    Maximum Topic Filters one session may hold — the *unit* bound on
    session state, whose total is ``BB_MQTT_MAX_SESSIONS`` and whose time
    bound is the Session Expiry Interval the client declares.  Without
    it a single connected client grows broker memory without limit, and
    with it the per-PUBLISH routing walk, since routing tests every
    filter of every connected session.  At the cap a **new** filter is
    refused with ``0x97 (Quota Exceeded)`` in the SUBACK and logged;
    re-subscribing to a filter the session already holds always works
    (§3.8.4 makes that a replacement, so it occupies no new slot).
    ``0`` disables the cap.  Default: ``1000``.
BB_MQTT_MAX_SESSIONS
    Maximum sessions the broker retains — the *total* bound on session
    state, whose unit is ``BB_MQTT_MAX_SUBSCRIPTIONS`` plus
    ``BB_MQTT_MAX_QUEUED_MESSAGES`` and whose time bound is the Session
    Expiry Interval.  A session outlives its connection by design, and
    §3.1.2.11.2 defines ``0xFFFFFFFF`` as *never expires*, so a peer
    cycling Client Identifiers can pin one entry per identifier while
    breaking no rule.  At the cap a CONNECT for an **unknown** Client
    Identifier is refused with ``0x97 (Quota Exceeded)`` in the CONNACK
    and the connection closed; a client resuming a session already in
    the table is admitted, since refusing it frees nothing.  Expired
    sessions are swept before the cap is applied, so it binds live state
    only.  ``0`` disables the cap.  Default: ``10000``.
BB_COMPRESSION_MIN_SIZE
    Minimum response body size in bytes below which
    [`Compression`][blackbull.middleware.compression.Compression] skips
    compression entirely.  Raising this threshold under load reduces CPU
    pressure at the cost of slightly larger small responses.
    Default: ``100``.
BB_COMPRESSION_EXECUTOR_THRESHOLD
    Body size in bytes above which compression is offloaded to a thread-pool
    executor so the event loop can continue processing other requests during
    the (CPU-heavy) compress call.  ``0`` always compresses on the event loop
    (disables offloading).  Default: ``65536`` (64 KiB).
BB_COMPRESSION_MAX_INFLIGHT
    Maximum number of compression offloads allowed to be running
    concurrently in the asyncio default thread pool.  When at or above
    this cap, additional eligible responses are served **uncompressed**
    rather than queued — bounded fall-back rather than unbounded queue
    growth.  Tied to executor size: setting this above Python's default
    ``ThreadPoolExecutor`` ``max_workers`` provides no benefit.  That
    default is ``min(32, os.cpu_count() + 4)`` on Python ≤ 3.12 and
    ``min(128, os.cpu_count() * 5)`` on Python ≥ 3.13.
    ``0`` removes the cap, leaving an unbounded executor queue that
    saturates under burst load.
    Default: ``max((os.cpu_count() or 1) * 2, 4)`` — the floor keeps a
    one- or two-CPU host overlapping a few offloads instead of
    serialising them.
BB_BROTLI_QUALITY
    Brotli quality level (0–11) for dynamic-response compression.  The
    brotli library's own default is 11 — designed for build-time/static
    pre-compression and far too expensive on the request path.  4 matches
    Google's and Cloudflare's dynamic-content recommendation; 5 matches
    Apache mod_brotli's default; 6 matches nginx ngx_brotli's default; 11
    is appropriate only for offline pre-compression of static siblings.
    Default: ``4``.
BB_FRAME_YIELD_EVERY
    Number of stream tasks spawned per connection before the frame loop
    inserts ``await asyncio.sleep(0)`` to let the event loop dispatch the
    queued tasks.  Under burst traffic (e.g. 500 VUs all sending at once)
    the frame loop can process many HEADERS frames without yielding, which
    stalls all waiting tasks and inflates p99 latency.  Yielding every N
    spawns caps the maximum synchronous run to N × ~50 µs regardless of
    burst size.  ``0`` disables cooperative yielding.
    Default: ``8``.
BB_UVLOOP
    Install the ``uvloop`` event loop policy before each
    ``asyncio.run()`` when the optional ``[speed]`` extra is installed.
    Falls back to the standard asyncio loop with a warning if uvloop is
    not importable.  Default: ``false``.
BB_FORCE_ASGI_SCOPE
    Dual-path conformance lane.  When enabled, every request round-trips the
    native [`Connection`][blackbull.connection.Connection] through ``as_scope()`` +
    ``from_scope()`` before dispatch, so the ASGI compat conversion is
    exercised on the self-hosted path and cannot silently bitrot.  Enabled in
    CI; off in normal operation, where the native path skips the round-trip.
    Default: ``false``.
BB_DEADLINE_TICK_MS
    Polling interval (milliseconds) for the per-process deadline scanner
    that enforces connection timeouts (``BB_HEADER_TIMEOUT``,
    ``BB_BODY_TIMEOUT``, ``BB_WRITE_TIMEOUT``, ``BB_KEEP_ALIVE_TIMEOUT``).
    Smaller = tighter timeout granularity at a small CPU cost; larger =
    more slack but cheaper.  Default: ``300``.
BB_CPU_PINNING
    Per-worker CPU pinning, applied after fork in each worker process.
    ``auto`` (default) gives worker *i* the *i*-th CPU of the mask the
    process already carries, so ``taskset``/``numactl``/cpuset placement is
    honoured rather than overridden; ``off`` pins nothing; an explicit
    ``taskset``-style list (``2,4,6-9`` — ``0`` is CPU 0, not the off
    switch) confines workers to those CPUs, intersected with the mask we
    were granted.  Only the event loop is pinned — the thread pool that
    serves ``run_in_executor`` compression and ``asyncio.to_thread`` file
    reads keeps the full mask.  Multi-worker and Linux only; a single-worker
    server is never pinned.  Default: ``auto``.

The ``BB_CLIENT_*`` block below bounds the **async client**, not the server.
The two are held apart deliberately: a server is addressed by anyone, while a
client picks its peer, so the same shape of limit gets a different default on
each side.  Each entry states what it bounds; the trade-off behind each number
lives on the matching [`Settings`][] field.

BB_CLIENT_HEAD_MAX_TOTAL
    Maximum total bytes in a response head the client will read (status line
    + all field lines + CRLFCRLF), bounded as it accumulates so an endless
    header cannot grow the client's memory.  Under HTTP/2 this bounds the
    field lines in aggregate across every HEADERS frame on the stream.
    ``0`` disables.
    Default: ``65536``.
BB_CLIENT_HEAD_MAX_LINE
    Maximum bytes in a single status line or response field line.  A policy
    rule rather than a second memory guard — no line can be longer than the
    already-bounded block containing it.  ``0`` disables.
    Default: ``8192``.
BB_CLIENT_HEAD_TIMEOUT
    Seconds the client waits for a complete response head — the time column
    for the read ``BB_CLIENT_HEAD_MAX_TOTAL`` bounds by size, since a peer
    that sends half a head and stops passes every byte budget forever.
    ``0`` disables.
    Default: ``30.0``.
BB_CLIENT_WRITE_TIMEOUT
    Maximum seconds the client waits for one send-progress operation: a
    socket drain on HTTP/1.1 and WebSocket, one ``SETTINGS_MAX_FRAME_SIZE``
    unit of DATA plus its flow-control credit on HTTP/2.  There is
    intentionally no whole-upload total owner.  ``0`` disables.
    Default: ``30.0``.
BB_CLIENT_BODY_TIMEOUT
    Seconds the client waits for a single response-body read — per read, not
    per body, so a peer must keep making progress.  Armed by the final
    response head rather than the request, and re-armed only by octets that
    are body payload.  ``0`` disables.
    Default: ``30.0``.
BB_CLIENT_BODY_MAX_TOTAL
    Maximum total response-body octets the client will buffer for one
    response.  Bounds ``receive()`` only — ``stream()`` exists so a large
    response need not fit in memory.  What it counts is body octets; what it
    costs is about 2× that at the join.  **Off by default**, unlike the
    server's ``BB_MAX_BODY_SIZE``: that number bounds what strangers push
    into a process, this one bounds what you asked for.
    Default: ``0`` (disabled).
BB_CLIENT_MIN_BODY_RATE
    Minimum sustained rate, in octets per second, at which the client
    requires a response **body** to arrive — what ``BB_CLIENT_BODY_TIMEOUT``
    cannot express, since it returns on any arrival.  The numerator is
    payload only, so framing octets a peer may pad buy no credit, and the
    wait before the first body octet is outside the window.  **Off by
    default**: after the first octet, an event stream and a drip are the
    same observation.  ``0`` disables.
    Default: ``0.0`` (disabled).
BB_CLIENT_MIN_BODY_RATE_GRACE
    Seconds of body-read waiting, after the first body octet, before
    ``BB_CLIENT_MIN_BODY_RATE`` is enforced.  The window rolls forward
    whenever it is satisfied, so a burst buys the window it happened in
    rather than the whole response.
    Default: ``5.0``.
BB_CLIENT_MAX_INTERIM_RESPONSES
    Maximum interim (``1xx``) responses the client reads and discards while
    waiting for the final one.  RFC 9110 §15.2 makes parsing past them a
    MUST, which turns "read one response" into a loop, and a loop over
    peer-supplied messages needs a count — this one, since the head budget
    and head deadline are both *per head*.  ``101`` is not counted: 1xx by
    number, final by meaning.  ``0`` disables the cap.
    Default: ``8``.
BB_CLIENT_RAW_QUEUE_DEPTH
    Frames the client holds for one **raw** HTTP/2 stream — the escape hatch
    where the receive loop hands frames to a registrant instead of the
    request/response machine.  Full resets that stream alone with
    ``ENHANCE_YOUR_CALM``; the connection survives.  Denominated in frames,
    because flow control charges only a DATA payload and a zero-length DATA
    frame would buy depth for free.  ``0`` disables (unbounded).
    Default: ``1024`` — a peer may legally burst its whole 65535-byte window
    as small frames, and a raw stream cannot be dropped without corrupting
    it, so this must not fire on legal traffic.
BB_CLIENT_H2_MAX_FRAME_SIZE
    Maximum octets in one inbound HTTP/2 frame payload the client will read,
    judged from the frame header so a peer-declared number never sizes an
    allocation.  Breach is a connection error of type FRAME_SIZE_ERROR:
    refusing before the read leaves the payload in the socket.  ``0``
    disables.
    Default: ``16384`` — RFC 9113 §6.5.2's initial ``SETTINGS_MAX_FRAME_SIZE``,
    the one value that neither refuses a conforming peer nor accepts what was
    never advertised.
BB_CLIENT_H2_MAX_HEADER_LIST_SIZE
    Maximum octets in one **decoded** field section the client accepts.  One
    number, two effects: advertised as ``SETTINGS_MAX_HEADER_LIST_SIZE`` and
    installed as the HPACK decoder's ``max_header_list_size``, which must
    agree because §6.5.2 makes the announcement advisory and the decoder the
    defence.  The only bound counted in decoded octets, since compression
    decouples the two sizes.  Breach is a connection error of type
    COMPRESSION_ERROR.  ``0`` disables.
    Default: ``65536`` — what hpack enforces unasked, so the default changes
    what the peer is *told*, not what is accepted.
BB_CLIENT_H2_ENABLE_PUSH
    Whether the client permits the peer to push (RFC 9113 §6.5.2).  A
    conformance switch, not a bound.  Enabled advertises nothing (1 is the
    parameter's initial value) and a PUSH_PROMISE is decoded and dropped;
    disabled advertises ``SETTINGS_ENABLE_PUSH=0`` **and** refuses a later
    PUSH_PROMISE with a connection error of type PROTOCOL_ERROR, §6.5.2
    making that refusal a MUST for whoever sends the 0.
    Default: ``true``.
BB_CLIENT_WS_MAX_FRAME_PAYLOAD
    Client WebSocket inbound frame payload cap, in bytes per frame.
    ``BB_CLIENT_WS_MAX_MESSAGE_SIZE`` owns the aggregate message total; the
    client WebSocket recipient has no environment-owned time bound.
    Default: ``67108864`` (64 MiB).
BB_CLIENT_WS_MAX_MESSAGE_SIZE
    Client WebSocket inbound message cap, in bytes per message — the total
    for which ``BB_CLIENT_WS_MAX_FRAME_PAYLOAD`` is the unit.
    Default: ``16777216`` (16 MiB).
"""
import dataclasses
import functools as _functools
import os
import resource
from enum import StrEnum


class Environment(StrEnum):
    """Which deployment the process thinks it is, read from ``BLACKBULL_ENV``.

    Reachable as ``get_settings().env``.  A value that is not one of these
    three resolves to ``DEVELOPMENT`` rather than raising, so a misspelled
    ``production`` gets the *looser* of the two behaviours and says nothing
    about it — worth asserting on at startup if it matters to you.

    Two behaviours turn on it, and both tighten in ``PRODUCTION``:
    [`StaticFiles`][blackbull.middleware.static.StaticFiles] declines to serve
    anything, and the default error handler answers without exception detail.
    The fault-injection servers refuse to start there at all.
    """

    PRODUCTION  = 'production'
    DEVELOPMENT = 'development'
    TEST        = 'test'


def _str_env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _int_env_nonneg(name: str, default: int) -> int:
    """Like _int_env but allows 0 (disables the feature)."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value >= 0 else default


#: Reserved so that running out of descriptors refuses a *new* connection,
#: which the peer can retry, instead of stranding a request already
#: accepted that can no longer open its database connection.
FD_RESERVE = 64


DEFAULT_COMPRESSION_MAX_INFLIGHT = max((os.cpu_count() or 1) * 2, 4)


def resolve_max_connections(raw: str | None) -> int:
    """Resolve ``BB_MAX_CONNECTIONS`` — ``auto``, ``0``, or a number.

    ``auto`` (the default) derives the cap from this process's own
    ``RLIMIT_NOFILE``, less a reserve.  An explicit number is honoured as
    given, *not* clamped to that budget: an operator who names a number
    means it.  ``0`` disables the cap.

    The environment-variable reference argues why a derived default is safe
    to ship, and why this bounds descriptor exhaustion rather than
    event-loop health.
    """
    if raw is None or raw.strip().lower() in ('', 'auto'):
        try:
            soft, _hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        except Exception:  # pragma: no cover - non-POSIX or restricted host
            return 0
        if soft in (resource.RLIM_INFINITY, -1):
            # No fd ceiling to derive from; an arbitrary number here would
            # be a policy the operator never chose.
            return 0
        # Never fall to 0 — in this server's vocabulary 0 means *uncapped*,
        # so an arithmetic slip would turn the tightest host into the least
        # protected one.
        return max(1, soft - FD_RESERVE)
    try:
        value = int(raw)
    except ValueError:
        return resolve_max_connections('auto')
    if value < 0:
        return resolve_max_connections('auto')
    return value


def _float_env_nonneg(name: str, default: float) -> float:
    """Read a non-negative float env var (0.0 is allowed — means disabled)."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value >= 0 else default


def _bool_env(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ('0', 'false', 'no', 'off')


@dataclasses.dataclass(frozen=True)
class Settings:
    """Immutable snapshot of all runtime settings.

    Construct via [`get_settings`][] rather than directly so that
    environment variables are read at the right time.
    """

    env: Environment = Environment.DEVELOPMENT
    workers: int = 1

    #: ``auto`` is what ships: [`resolve_max_connections`][] derives
    #: the cap from ``RLIMIT_NOFILE``.  This literal reaches only a
    #: directly-constructed ``Settings``, and leaves it uncapped rather
    #: than derived so it is not capped by whichever host it runs on.
    max_connections: int = 0
    stream_queue_depth: int = 64
    ws_queue_depth: int = 0
    async_logging: bool = True
    access_log: bool = True
    log_format: str = ''
    log_syslog_addr: str = ''
    log_batch_size: int = 64
    log_batch_timeout_ms: int = 5
    log_file: str = ''
    socket_backlog: int = 1024
    socket_sndbuf: int = 0
    socket_rcvbuf: int = 0

    #: No help for a cold-start connection burst, and measurably a
    #: pessimization there: at cold start every worker is equally cold,
    #: so N per-worker queues starve at once, and the shared queue's
    #: cross-worker load-balancing is given up as well.
    socket_reuseport: bool = False

    #: 5 s, not the conventional 60: a connection parked in
    #: ``readuntil`` inflates the suspended-task count and amplifies
    #: burst-close drain time.  The timer also replaces a per-accept
    #: ``SO_KEEPALIVE`` syscall, measured as a contributor to wrk
    #: c=1024-burst connect-RST errors.
    keep_alive_timeout: float = 5.0
    tcp_user_timeout_ms: int = 0
    request_timeout: float = 0.0
    header_timeout: float = 10.0
    body_timeout: float = 30.0
    write_timeout: float = 30.0
    header_max_line: int = 8192
    header_max_total: int = 65536
    client_head_max_total: int = 65536
    client_head_max_line: int = 8192
    client_head_timeout: float = 30.0
    client_write_timeout: float = 30.0
    client_body_timeout: float = 30.0
    client_body_max_total: int = 0
    client_min_body_rate: float = 0.0
    client_min_body_rate_grace: float = 5.0
    client_ws_max_frame_payload: int = 64 * 1024 * 1024
    client_ws_max_message_size: int = 16 * 1024 * 1024
    client_max_interim_responses: int = 8
    client_raw_queue_depth: int = 1024
    client_h2_max_frame_size: int = 16384
    client_h2_max_header_list_size: int = 65536
    client_h2_enable_push: bool = True
    force_asgi_scope: bool = False
    body_chunk_size: int = 65536
    body_chunk_max: int = 524288
    max_body_size: int = 31457280
    min_body_rate: float = 240.0
    min_body_rate_grace: float = 5.0
    h2_initial_window_size: int = 65535
    h2_connection_window_size: int = 65535
    h2_max_concurrent_streams: int = 100
    h2_enable_websocket: bool = False
    h2_ws_max_streams_per_connection: int = 5
    ws_permessage_deflate: bool = True
    ws_max_frame_payload: int = 64 * 1024 * 1024
    frame_rate_limit: int = 20
    frame_rate_window: float = 1.0
    h2_idle_timeout: float = 300.0
    h2_ping_timeout: float = 30.0
    ws_idle_timeout: float = 300.0
    ws_pong_timeout: float = 30.0
    mqtt_max_packet_size: int = 1024 * 1024
    mqtt_receive_maximum: int = 64
    mqtt_max_queued_messages: int = 1000
    mqtt_broker_inbox_maxsize: int = 1024
    mqtt_broker_inbox_max_bytes: int = 16 * 1024 * 1024
    mqtt_connection_inbox_maxsize: int = 1024
    mqtt_connection_inbox_max_bytes: int = 16 * 1024 * 1024
    mqtt_max_retained: int = 10000
    mqtt_max_subscriptions: int = 1000
    mqtt_max_sessions: int = 10000
    ws_max_message_size: int = 16 * 1024 * 1024
    worker_drain_timeout: float = 8.0

    #: 20 on both paths, because past it concurrency costs more than it
    #: buys on one event loop: uncapped, mux-10 out-throughputs mux-50
    #: on a single worker, and a multi-worker box reaches the same point
    #: at roughly 4 connections x mux-50 per worker.
    h2_active_streams_1w: int = 20
    h2_active_streams: int = 20
    use_uvloop: bool = False
    compression_min_size: int = 100
    compression_executor_threshold: int = 64 * 1024
    compression_max_inflight: int = DEFAULT_COMPRESSION_MAX_INFLIGHT
    brotli_quality: int = 4
    cpu_pinning: str = 'auto'
    frame_yield_every: int = 8


@_functools.cache
def get_settings() -> Settings:
    """Read environment variables and return an immutable [`Settings`][].

    Cached: first call parses env vars and builds the dataclass; subsequent
    calls return the same instance.  Settings are server-process-wide
    configuration, not per-request data — there's no reason to re-parse
    ``os.environ`` on every request.  Profile showed ``_int_env`` and
    ``_int_env_nonneg`` consuming ~5–6% of CPU in the HTTP/1.1 hot path
    before this cache.

    Tests that mutate environment between cases must call
    [`reset_settings_cache`][] in their teardown.
    """
    raw_env = _str_env('BLACKBULL_ENV', 'development').lower()
    try:
        env = Environment(raw_env)
    except ValueError:
        env = Environment.DEVELOPMENT

    return Settings(
        env=env,
        workers=_int_env('BB_WORKERS', 1),
        max_connections=resolve_max_connections(
            os.environ.get('BB_MAX_CONNECTIONS')),
        stream_queue_depth=_int_env('BB_STREAM_QUEUE_DEPTH', 64),
        ws_queue_depth=_int_env('BB_WS_QUEUE_DEPTH', 0),
        async_logging=_bool_env('BB_ASYNC_LOGGING', True),
        access_log=_bool_env('BB_ACCESS_LOG', True),
        log_format=_str_env('BB_LOG_FORMAT', ''),
        log_syslog_addr=_str_env('BB_SYSLOG_ADDR', ''),
        log_batch_size=_int_env('BB_LOG_BATCH_SIZE', 64),
        log_batch_timeout_ms=_int_env('BB_LOG_BATCH_TIMEOUT_MS', 5),
        log_file=_str_env('BB_LOG_FILE', ''),
        socket_backlog=_int_env('BB_SOCKET_BACKLOG', 1024),
        socket_sndbuf=_int_env_nonneg('BB_SOCKET_SNDBUF', 0),
        socket_rcvbuf=_int_env_nonneg('BB_SOCKET_RCVBUF', 0),
        socket_reuseport=_bool_env('BB_SOCKET_REUSEPORT', False),
        keep_alive_timeout=_float_env_nonneg('BB_KEEP_ALIVE_TIMEOUT', 5.0),
        tcp_user_timeout_ms=_int_env_nonneg('BB_TCP_USER_TIMEOUT_MS', 0),
        request_timeout=_float_env_nonneg('BB_REQUEST_TIMEOUT', 0.0),
        header_timeout=_float_env_nonneg('BB_HEADER_TIMEOUT', 10.0),
        body_timeout=_float_env_nonneg('BB_BODY_TIMEOUT', 30.0),
        write_timeout=_float_env_nonneg('BB_WRITE_TIMEOUT', 30.0),
        header_max_line=_int_env_nonneg('BB_HEADER_MAX_LINE', 8192),
        header_max_total=_int_env_nonneg('BB_HEADER_MAX_TOTAL', 65536),
        client_head_max_total=_int_env_nonneg('BB_CLIENT_HEAD_MAX_TOTAL', 65536),
        client_head_max_line=_int_env_nonneg('BB_CLIENT_HEAD_MAX_LINE', 8192),
        client_head_timeout=_float_env_nonneg('BB_CLIENT_HEAD_TIMEOUT', 30.0),
        client_write_timeout=_float_env_nonneg(
            'BB_CLIENT_WRITE_TIMEOUT', 30.0),
        client_body_timeout=_float_env_nonneg('BB_CLIENT_BODY_TIMEOUT', 30.0),
        client_body_max_total=_int_env_nonneg('BB_CLIENT_BODY_MAX_TOTAL', 0),
        client_min_body_rate=_float_env_nonneg('BB_CLIENT_MIN_BODY_RATE', 0.0),
        client_min_body_rate_grace=_float_env_nonneg(
            'BB_CLIENT_MIN_BODY_RATE_GRACE', 5.0),
        client_ws_max_frame_payload=_int_env_nonneg(
            'BB_CLIENT_WS_MAX_FRAME_PAYLOAD', 64 * 1024 * 1024),
        client_ws_max_message_size=_int_env_nonneg(
            'BB_CLIENT_WS_MAX_MESSAGE_SIZE', 16 * 1024 * 1024),
        client_max_interim_responses=_int_env_nonneg(
            'BB_CLIENT_MAX_INTERIM_RESPONSES', 8),
        client_raw_queue_depth=_int_env_nonneg(
            'BB_CLIENT_RAW_QUEUE_DEPTH', 1024),
        client_h2_max_frame_size=_int_env_nonneg(
            'BB_CLIENT_H2_MAX_FRAME_SIZE', 16384),
        client_h2_max_header_list_size=_int_env_nonneg(
            'BB_CLIENT_H2_MAX_HEADER_LIST_SIZE', 65536),
        client_h2_enable_push=_bool_env('BB_CLIENT_H2_ENABLE_PUSH', True),
        force_asgi_scope=_bool_env('BB_FORCE_ASGI_SCOPE', False),
        body_chunk_size=_int_env('BB_BODY_CHUNK_SIZE', 65536),
        body_chunk_max=_int_env('BB_BODY_CHUNK_MAX', 524288),
        max_body_size=_int_env_nonneg('BB_MAX_BODY_SIZE', 31457280),
        min_body_rate=_float_env_nonneg('BB_MIN_BODY_RATE', 240.0),
        min_body_rate_grace=_float_env_nonneg('BB_MIN_BODY_RATE_GRACE', 5.0),
        h2_initial_window_size=_int_env('BB_H2_INITIAL_WINDOW_SIZE', 65535),
        h2_connection_window_size=_int_env('BB_H2_CONNECTION_WINDOW_SIZE', 65535),
        h2_max_concurrent_streams=_int_env('BB_H2_MAX_CONCURRENT_STREAMS', 100),
        h2_enable_websocket=_bool_env('BB_H2_ENABLE_WEBSOCKET', False),
        h2_ws_max_streams_per_connection=_int_env_nonneg(
            'BB_H2_WS_MAX_STREAMS_PER_CONNECTION', 5),
        ws_permessage_deflate=_bool_env('BB_WS_PERMESSAGE_DEFLATE', True),
        ws_max_frame_payload=_int_env_nonneg(
            'BB_WS_MAX_FRAME_PAYLOAD', 64 * 1024 * 1024),
        ws_max_message_size=_int_env_nonneg(
            'BB_WS_MAX_MESSAGE_SIZE', 16 * 1024 * 1024),
        frame_rate_limit=_int_env_nonneg('BB_FRAME_RATE_LIMIT', 20),
        frame_rate_window=_float_env_nonneg('BB_FRAME_RATE_WINDOW', 1.0),
        h2_idle_timeout=_float_env_nonneg('BB_H2_IDLE_TIMEOUT', 300.0),
        h2_ping_timeout=_float_env_nonneg('BB_H2_PING_TIMEOUT', 30.0),
        ws_idle_timeout=_float_env_nonneg('BB_WS_IDLE_TIMEOUT', 300.0),
        ws_pong_timeout=_float_env_nonneg('BB_WS_PONG_TIMEOUT', 30.0),
        mqtt_max_packet_size=_int_env_nonneg(
            'BB_MQTT_MAX_PACKET_SIZE', 1024 * 1024),
        mqtt_receive_maximum=_int_env_nonneg('BB_MQTT_RECEIVE_MAXIMUM', 64),
        mqtt_max_queued_messages=_int_env_nonneg(
            'BB_MQTT_MAX_QUEUED_MESSAGES', 1000),
        mqtt_broker_inbox_maxsize=_int_env(
            'BB_MQTT_BROKER_INBOX_MAXSIZE', 1024),
        mqtt_broker_inbox_max_bytes=_int_env(
            'BB_MQTT_BROKER_INBOX_MAX_BYTES', 16 * 1024 * 1024),
        mqtt_connection_inbox_maxsize=_int_env(
            'BB_MQTT_CONNECTION_INBOX_MAXSIZE', 1024),
        mqtt_connection_inbox_max_bytes=_int_env(
            'BB_MQTT_CONNECTION_INBOX_MAX_BYTES', 16 * 1024 * 1024),
        mqtt_max_retained=_int_env_nonneg('BB_MQTT_MAX_RETAINED', 10000),
        mqtt_max_subscriptions=_int_env_nonneg(
            'BB_MQTT_MAX_SUBSCRIPTIONS', 1000),
        mqtt_max_sessions=_int_env_nonneg('BB_MQTT_MAX_SESSIONS', 10000),
        use_uvloop=_bool_env('BB_UVLOOP', False),
        worker_drain_timeout=_float_env_nonneg('BB_WORKER_DRAIN_TIMEOUT', 8.0),
        h2_active_streams_1w=_int_env_nonneg('BB_H2_ACTIVE_STREAMS_1W', 20),
        h2_active_streams=_int_env_nonneg('BB_H2_ACTIVE_STREAMS', 20),
        compression_min_size=_int_env('BB_COMPRESSION_MIN_SIZE', 100),
        compression_executor_threshold=_int_env_nonneg('BB_COMPRESSION_EXECUTOR_THRESHOLD', 65536),
        compression_max_inflight=_int_env_nonneg(
            'BB_COMPRESSION_MAX_INFLIGHT', DEFAULT_COMPRESSION_MAX_INFLIGHT),
        brotli_quality=_int_env_nonneg('BB_BROTLI_QUALITY', 4),
        frame_yield_every=_int_env_nonneg('BB_FRAME_YIELD_EVERY', 8),
        cpu_pinning=_str_env('BB_CPU_PINNING', 'auto'),
    )


def reset_settings_cache() -> None:
    """Clear the cached [`Settings`][].

    Call this in test teardown if the test mutated env vars that
    [`get_settings`][] reads.  Without this, the cached settings reflect
    whatever environment was visible the first time ``get_settings()`` ran
    in the process.
    """
    get_settings.cache_clear()


def apply_event_loop_policy(cfg: Settings | None = None) -> None:
    """Install uvloop as the asyncio event loop policy if ``BB_UVLOOP=1``.

    Call this once before each ``asyncio.run()`` entry point.  Safe to call
    multiple times (subsequent calls are no-ops when the policy is already set).
    If uvloop is not installed a warning is logged and the standard policy is
    kept; the server still starts.
    """
    import asyncio  # noqa: PLC0415
    import logging  # noqa: PLC0415

    if cfg is None:
        cfg = get_settings()
    if not cfg.use_uvloop:
        return
    try:
        import uvloop  # type: ignore[import-untyped]  # noqa: PLC0415
        if not isinstance(asyncio.get_event_loop_policy(), uvloop.EventLoopPolicy):
            asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
            logging.getLogger(__name__).info('Event loop: uvloop')
    except ImportError:
        logging.getLogger(__name__).warning(
            'BB_UVLOOP=1 but uvloop is not installed; '
            'falling back to standard asyncio loop.  '
            'Run: pip install "blackbull[speed]"'
        )
