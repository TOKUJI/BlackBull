"""Runtime configuration sourced from environment variables.

All server settings live in :class:`Settings`.  Retrieve the current
configuration with :func:`get_settings`, which reads environment variables
once and returns an immutable snapshot.

Environment variables
---------------------
BLACKBULL_ENV
    ``production`` | ``development`` (default) | ``test``
BB_WORKERS
    Number of worker processes.  ``0`` resolves to ``os.cpu_count()``.
    Default: ``1``.
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
BB_STREAM_QUEUE_DEPTH
    ``asyncio.Queue`` depth for HTTP/2 per-stream request-body events.
    Limits memory growth when an ASGI handler is slower than the client.
    Default: ``64``.
BB_WS_QUEUE_DEPTH
    ``asyncio.Queue`` depth for WebSocket inbound events per connection.
    Default: ``256``.
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
BB_LOG_BATCH_SIZE
    When > 1, the ``stderr`` async-logging sink coalesces up to this many
    formatted lines into a single ``write()``.  ``1`` (default) is one write per
    record.  Ignored for the syslog sink.
    Default: ``1``.
BB_LOG_BATCH_TIMEOUT_MS
    Max milliseconds a partial log batch waits before flush.  Only meaningful
    when ``BB_LOG_BATCH_SIZE`` > 1.
    Default: ``5``.
BB_SOCKET_BACKLOG
    ``listen()`` backlog depth for the server socket.  Increasing this reduces
    silent connection drops during burst traffic when the accept loop falls
    behind.  Capped by ``net.core.somaxconn`` on Linux.
    Default: ``128`` (matches the Linux ``net.core.somaxconn`` traditional
    default).  Bump to 4096 for production traffic — see
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
    per connection, multiplied by ``BB_MAX_CONNECTIONS`` (default 0 =
    unbounded).  Default: ``5``.
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
    meaningful when ``BB_WS_IDLE_TIMEOUT`` is non-zero.  Default:
    ``30.0`` — as ``BB_H2_PING_TIMEOUT``, and for the same reason.
BB_MQTT_MAX_PACKET_SIZE
    Maximum size (bytes) of a single inbound MQTT control packet,
    advertised to clients as the ``Maximum Packet Size`` property in
    CONNACK (§3.2.2.3.6) so a conforming client never sends one.  The
    check runs on the declared Remaining Length as soon as the fixed
    header is readable, so an over-size packet is refused **without
    buffering its payload** — MQTT 5 permits a peer to declare
    268,435,455 bytes (256 MiB) and dribble them.  Over the cap the
    broker answers ``DISCONNECT`` with reason code **0x95 (Packet Too
    Large)** and closes.  ``0`` disables the cap.  Default:
    ``1048576`` (1 MiB) — MQTT payloads are overwhelmingly small, so a
    limit that admits a megabyte still admits every realistic message
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
    ``blackbull.caps`` log regardless.  ``0`` disables the cap.  Default:
    ``10000``.
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
    :class:`~blackbull.middleware.compression.Compression` skips
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
    ``0`` disables backpressure (unbounded queue, pre-0.29 behaviour).
    Default: ``os.cpu_count() * 2``.
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
    burst size.  ``0`` disables cooperative yielding (legacy behaviour).
    Default: ``8``.
BB_UVLOOP
    Install the ``uvloop`` event loop policy before each
    ``asyncio.run()`` when the optional ``[speed]`` extra is installed.
    Falls back to the standard asyncio loop with a warning if uvloop is
    not importable.  Default: ``false``.
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
"""
import dataclasses
import functools as _functools
import os
import resource
from enum import StrEnum


# ---------------------------------------------------------------------------
# Environment enum (unchanged public API)
# ---------------------------------------------------------------------------

class Environment(StrEnum):
    PRODUCTION  = 'production'
    DEVELOPMENT = 'development'
    TEST        = 'test'


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

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


#: File descriptors held back from the connection budget when
#: ``BB_MAX_CONNECTIONS`` derives its value: the listening sockets, the
#: event loop's own selector, log files, and whatever the application
#: keeps open (a database pool being the usual case).  Handing every
#: descriptor to connections would move the failure from "a connection is
#: refused" — which the peer can retry — to "a request already accepted
#: cannot open its database connection", which it cannot.
FD_RESERVE = 64


def resolve_max_connections(raw: str | None) -> int:
    """Resolve ``BB_MAX_CONNECTIONS`` — ``auto``, ``0``, or a number.

    ``auto`` (the default) derives the cap from this process's own
    ``RLIMIT_NOFILE``.  A cap above the file-descriptor budget is
    decorative: ``accept()`` fails with ``EMFILE`` before the cap is
    consulted, so the peer gets a dropped connection instead of the
    ``503 + Retry-After`` the mechanism exists to send.  Derived, the cap
    can only refuse connections the OS was going to refuse anyway — which
    is what makes a finite default safe to ship — and it tracks the
    operator's own intent, since raising the fd limit is how an operator
    states how large this process may become.

    An explicit number is honoured as given, *not* clamped to the fd
    budget: an operator who names a number means it, and silently running
    a different one would make the live configuration differ from the
    configured one with nothing to show for it.  ``0`` disables the cap.

    Note this bounds *descriptor exhaustion*, not event-loop health.  A
    ceiling reflecting what one asyncio loop serves well is a policy
    number that depends on the workload — set it explicitly; 1024 is a
    typical single-loop value.
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


# ---------------------------------------------------------------------------
# Settings dataclass
# ---------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class Settings:
    """Immutable snapshot of all runtime settings.

    Construct via :func:`get_settings` rather than directly so that
    environment variables are read at the right time.
    """

    env: Environment = Environment.DEVELOPMENT

    #: Number of worker processes (0 → resolved to ``os.cpu_count()`` by the
    #: caller; stored as-is here).
    workers: int = 1

    #: Maximum simultaneous TCP connections per worker.  When the cap
    #: is reached, new connections receive HTTP/1.1 ``503 Service
    #: Unavailable`` with ``Retry-After: 1`` before close (well-formed
    #: response so load-balancers / health-checks can interpret it
    #: correctly).  ``0`` disables the cap entirely — relies on the OS
    #: file-descriptor limit instead.
    #:
    #: Capped rather than unbounded, for event-loop integrity:
    #: unbounded per-worker concurrency lets a single
    #: client (or burst, or slowloris-class workload) park thousands of
    #: suspended-readuntil tasks on the event loop, amplifying drain
    #: time on burst-close and inflating worst-case latency.  Set
    #: ``BB_MAX_CONNECTIONS`` to a finite ceiling on untrusted hosts;
    #: 1024 is a typical single-asyncio-loop ceiling, and multi-worker
    #: deployments multiply (so ``workers=8`` × ``BB_MAX_CONNECTIONS=1024``
    #: → 8K connections per process).
    #:
    #: Resolved by :func:`resolve_max_connections` — the default is
    #: ``auto``, derived from ``RLIMIT_NOFILE``.  The dataclass default
    #: below is only the fallback for a directly-constructed ``Settings``
    #: (tests); ``0`` there keeps such a construction unbounded rather
    #: than silently capped by whatever host the test happens to run on.
    max_connections: int = 0

    #: asyncio.Queue depth for HTTP/2 per-stream request-body events.
    stream_queue_depth: int = 64

    #: WebSocket inbound read-ahead depth.  0 (default) reads inline in the
    #: app's own task — no reader task, no per-message queue hop.  A positive
    #: value restores the background reader with a queue of that depth, which
    #: buys control-frame servicing between the app's receive() calls.
    ws_queue_depth: int = 0

    #: Install QueueHandler on the blackbull logger so event-loop log calls are non-blocking.
    async_logging: bool = True

    #: Emit one access log record per completed request on blackbull.access.
    access_log: bool = True

    #: Async-logging sink format: '' → plain text (default), 'json' → one
    #: structured JSON object per line (approach 3).
    log_format: str = ''

    #: host:port of a syslog/UDP collector (approach 6).  '' keeps the stderr
    #: sink.  When set, records ship via a UDP SysLogHandler.
    log_syslog_addr: str = ''

    #: Coalescing width of the async-logging sink (approach 4 / O2): up to N
    #: formatted records are joined into a single write+flush.  Async logging is
    #: batch logging — the sink always coalesces (min 2); the per-record flush of
    #: a plain StreamHandler is the dominant access-log cost, so it is not an
    #: async option.  Default 64.  To force per-record flush, disable async
    #: logging (the synchronous path) instead.
    log_batch_size: int = 64

    #: Max milliseconds a partial log batch waits before flush — bounds the
    #: visibility latency of the async sink at low request rates.
    log_batch_timeout_ms: int = 5

    #: Path for the async-logging sink to write to (append mode, approach 2).
    #: '' (default) keeps the stderr sink.  Composes with log_format/batch; each
    #: worker opens its own append stream post-fork.  Ignored for the syslog sink.
    log_file: str = ''

    #: listen() backlog depth for the server socket.  1024 is a sane
    #: default for servers facing connection bursts — 128 (the traditional
    #: ``SOMAXCONN``) is shallow next to peers like nginx (511) and Node
    #: (511).  The kernel still caps the effective queue at
    #: ``net.core.somaxconn``, so raise that too for very high fan-in.
    #: See docs/reference/env-vars.md "Performance recommendations".
    socket_backlog: int = 1024

    #: SO_SNDBUF for accepted sockets (0 = leave kernel default).
    socket_sndbuf: int = 0

    #: SO_RCVBUF for accepted sockets (0 = leave kernel default).
    socket_rcvbuf: int = 0

    #: Use SO_REUSEPORT to give each worker its own kernel accept queue.
    #: Off by default — only meaningful under ``workers > 1``.  Production
    #: multi-worker deployments should enable it; see
    #: docs/reference/env-vars.md "Performance recommendations".
    #:
    #: NOTE: for the cold-start connection-burst workload SO_REUSEPORT is a
    #: *pessimization* — it does not prevent accept-starvation (at cold start
    #: every worker is equally cold, so N per-worker queues starve at once) and
    #: it removes the shared queue's cross-worker load-balancing.
    socket_reuseport: bool = False

    #: Idle timeout (seconds) on a keep-alive connection that is awaiting
    #: the *next* request.  Replaces per-accept ``SO_KEEPALIVE`` syscalls
    #: with an application-level timer — same ghost-eviction guarantee,
    #: zero syscall cost per accept (which was a measurable contributor
    #: to wrk c=1024-burst connect-RST errors).  Combined with
    #: ``TCP_USER_TIMEOUT`` on the listening socket (inherits to accepted)
    #: which handles the *active-but-stuck* case.  0 disables the timer.
    #:
    #: Kept short for event-loop integrity: a conventional 60 s
    #: parks ghost / idle connections in the loop's
    #: ``readuntil`` for far longer than necessary, inflating the
    #: suspended-task count and amplifying burst-close drain time.
    #: 5 s is a common short-idle value for request-pipeline keep-alive.
    #: Long-lived clients on slow links should set
    #: ``BB_KEEP_ALIVE_TIMEOUT`` explicitly to a higher value.
    keep_alive_timeout: float = 5.0

    #: ``TCP_USER_TIMEOUT`` value in **milliseconds** for accepted sockets.
    #: Linux-only; set on the listening socket and inherited by accepted.
    #: Forces a connection-level error if a peer fails to ACK in this
    #: window — protects against dead-mid-write peers that ``SO_KEEPALIVE``
    #: misses.  0 leaves the kernel default unchanged.
    tcp_user_timeout_ms: int = 0

    #: Per-request timeout in seconds for HTTP/2 streams (0 = disabled).
    request_timeout: float = 0.0

    #: Maximum seconds an HTTP/1.1 client has to send the complete header
    #: block (request-line + headers + CRLFCRLF).  When the deadline
    #: elapses, the server answers with 408 Request Timeout and closes.
    #: Primary defence against slowloris — an attacker can otherwise hold
    #: a connection open indefinitely by dripping bytes.  0 = disabled
    #: (legacy behaviour; only recommended for trusted local clients).
    header_timeout: float = 10.0

    #: Maximum seconds an HTTP/1.1 client has to deliver the complete
    #: request body once headers are parsed.  Mirrors ``header_timeout``
    #: for the body half — slowloris attackers can otherwise hold a
    #: ``Content-Length: N`` connection open by dripping body bytes after
    #: the headers have arrived.  When the deadline elapses the recipient
    #: returns ``http.disconnect`` and the server tears the connection
    #: down.  0 = disabled (legacy behaviour).
    body_timeout: float = 30.0

    #: Maximum seconds the server will wait for a single write to be
    #: flushed to the peer (via ``StreamWriter.drain()``).  Defends
    #: against the *slow-read* shape of slowloris: a client that reads
    #: the response 1 byte/sec eventually fills the kernel send buffer
    #: and our ``drain()`` blocks indefinitely waiting for the peer's
    #: TCP window to reopen.  Without this timeout the server's write
    #: coroutine — and the connection slot it holds — is parked
    #: forever.  When the deadline elapses we close the transport;
    #: the sender treats the failure the same as a peer-side
    #: ``ConnectionResetError``.  0 = disabled.
    write_timeout: float = 30.0

    #: Maximum bytes in a single HTTP/1.1 request-line or header line.
    #: A pathological 1 GB ``X-foo: ...`` header would otherwise live in
    #: ``readuntil``'s internal buffer.  Enforced before parsing so an
    #: attacker cannot exhaust memory.  Default 8 KiB matches Apache
    #: ``LimitRequestLine`` / nginx ``large_client_header_buffers``.
    header_max_line: int = 8192

    #: Maximum total bytes in the entire request header block
    #: (request-line + all headers + CRLFCRLF).  Default 64 KiB matches
    #: typical reverse-proxy defaults.
    header_max_total: int = 65536

    #: Dual-path conformance lane.  When true, every request
    #: round-trips the native :class:`~blackbull.connection.Connection` through
    #: ``as_scope()`` + ``from_scope()`` before dispatch, so the ASGI compat
    #: conversion is exercised on the self-hosted path and cannot silently
    #: bitrot.  Off by default (the native path skips the extra round-trip);
    #: turned on in CI via ``BB_FORCE_ASGI_SCOPE=1``.
    force_asgi_scope: bool = False

    #: Slice size (bytes) for a ``Transfer-Encoding: chunked`` request body:
    #: each chunk in progress is delivered in reads of at most this many
    #: bytes, so a peer-declared ``chunk-size`` never sets the read size.
    #: 64 KiB sits below the backpressure high-water mark, which is what lets
    #: the pause work.  The ``Content-Length`` path is transport-paced instead
    #: — its per-read bound is ``body_chunk_max``.  Must be > 0.
    body_chunk_size: int = 65536

    #: Per-read bound (bytes) for a ``Content-Length`` request body.  Reads
    #: are up-to-n and transport-paced: each returns whatever the peer has
    #: delivered so far, up to this cap, and never blocks waiting to fill it.
    #: A slow peer therefore yields small slices (no read is ever a latency
    #: commitment ``body_timeout`` might not deliver) while a fast one earns
    #: fewer, larger ones — a large upload costs proportionally fewer receive
    #: round-trips (8.5 MB: ~130 reads at 64 KiB → ~17 capped at 512 KiB).
    #:
    #: The cap is a memory bound, not a latency one: it limits how much a
    #: single read may materialise per connection.  Raise it for large uploads
    #: over fast links.  Values below ``body_chunk_size`` are raised to it.
    body_chunk_max: int = 524288

    #: Maximum total request-body octets accepted for one request.  Over the
    #: cap the server answers **413 Content Too Large** and closes: a declared
    #: ``Content-Length`` is refused at head time, before a body byte is read,
    #: and a ``chunked`` body is refused the moment the running total passes
    #: the cap.  Without it a peer chooses how much memory the server spends —
    #: ``conn.body()`` accumulates whatever arrives, and the per-read bound
    #: (``body_chunk_max``) limits one read, not the sum of them.
    #:
    #: The connection always closes on a refusal, on both framings: the
    #: unread octets are attacker-chosen, so parsing whatever follows them as
    #: the next request is the request-smuggling shape.
    #:
    #: 30 MiB is the same class as Kestrel's ``MaxRequestBodySize`` (30,000,000
    #: bytes = 28.6 MiB — near, not equal: this one is a round binary value);
    #: nginx defaults to 1 MB, axum to 2 MB.  Raise it for an upload endpoint,
    #: or set ``0`` to
    #: disable the cap entirely (uvicorn's behaviour — the app then owns the
    #: 413 decision).
    max_body_size: int = 31457280

    #: Minimum sustained request-body delivery rate in **bytes per second**.
    #: Below it, past the grace period, the connection is abandoned the same
    #: way ``body_timeout`` abandons a silent one.  The rate is averaged over a
    #: sliding window one grace period wide (``min_body_rate_grace``): a peer
    #: that delivered early and then stalled is judged on the stalled window,
    #: not on the lifetime average, so a burst cannot shelter a subsequent
    #: drip.
    #:
    #: This is the anti-trickle half of the body defence, and it exists
    #: because a transport-paced read cannot be one: each read returns
    #: whatever has arrived, so ``body_timeout`` degrades from "fill a slice
    #: in 30 s" to "send *something* every 30 s" — which a one-byte drip
    #: always satisfies, holding a connection open indefinitely.  A rate is
    #: the thing a drip cannot fake.
    #:
    #: 240 B/s over a 5 s grace matches Kestrel's ``MinRequestBodyDataRate``
    #: defaults.  ``0`` disables the detector.
    min_body_rate: float = 240.0

    #: Seconds of body-read waiting before ``min_body_rate`` starts being
    #: enforced — the slow-start allowance, so a connection is never judged
    #: on its first few packets.
    #:
    #: Only time spent *waiting on the transport* counts, never time the
    #: handler spent between reads: the rate is evidence about the peer, and
    #: a handler that writes each chunk to a slow disk must not be mistaken
    #: for one.
    min_body_rate_grace: float = 5.0

    #: Per-stream HTTP/2 flow-control window advertised in the server's SETTINGS.
    #: 65535 is the RFC 9113 §6.9.2 default.  Production deployments serving
    #: large responses should raise this — see
    #: docs/reference/env-vars.md "Performance recommendations".
    h2_initial_window_size: int = 65535

    #: Connection-level HTTP/2 flow-control window advertised via WINDOW_UPDATE(stream_id=0).
    #: 65535 is the RFC 9113 §6.9.2 connection-window minimum.  Production
    #: deployments should raise this — see env-vars.md recommendations.
    h2_connection_window_size: int = 65535

    #: Maximum concurrent HTTP/2 streams per connection (SETTINGS_MAX_CONCURRENT_STREAMS).
    h2_max_concurrent_streams: int = 100

    #: Advertise SETTINGS_ENABLE_CONNECT_PROTOCOL=1 (RFC 8441 §3) so peers may
    #: bootstrap WebSocket over HTTP/2 via Extended CONNECT.  Off by default —
    #: this path has fewer conformance tests than the HTTP/1.1 upgrade path,
    #: and few clients use it in practice (Cloudflare's edge stack is the
    #: main consumer).  Set ``BB_H2_ENABLE_WEBSOCKET=1`` to turn it on.
    h2_enable_websocket: bool = False

    #: Maximum concurrent WebSocket (RFC 8441 Extended CONNECT) streams per
    #: HTTP/2 connection.  Limits the per-connection blast radius of WS-over-H2
    #: stream-exhaustion attacks — without this cap, an attacker can hold
    #: ``h2_max_concurrent_streams`` (default 100) WS streams open per
    #: connection across ``max_connections`` (default 0 = unbounded)
    #: connections.  ``0`` disables the per-connection cap.  Only meaningful
    #: when ``h2_enable_websocket=True``.
    h2_ws_max_streams_per_connection: int = 5

    #: Negotiate ``permessage-deflate`` (RFC 7692) on incoming WebSocket
    #: handshakes when the peer offers it.  On by default — matches modern
    #: browsers and the major library defaults (`ws` for Node, Python
    #: `websockets`, aiohttp).  Set ``BB_WS_PERMESSAGE_DEFLATE=0`` to disable.
    ws_permessage_deflate: bool = True

    #: Maximum declared payload length (bytes) for a single inbound
    #: WebSocket frame.  See BB_WS_MAX_FRAME_PAYLOAD docstring above for
    #: the security rationale.  Default 64 MiB.
    ws_max_frame_payload: int = 64 * 1024 * 1024

    #: Per-type, per-connection budget for metered control frames.  See
    #: BB_FRAME_RATE_LIMIT above; ``0`` disables all frame-rate metering.
    frame_rate_limit: int = 20

    #: Width in seconds of the frame-rate window.  See BB_FRAME_RATE_WINDOW.
    frame_rate_window: float = 1.0

    #: Seconds of silence on an HTTP/2 connection before probing the peer
    #: with a PING; ``0`` disables the probe.  See BB_H2_IDLE_TIMEOUT above.
    h2_idle_timeout: float = 300.0

    #: Seconds to wait for any frame after a liveness PING before closing
    #: with GOAWAY(NO_ERROR).  See BB_H2_PING_TIMEOUT above.
    h2_ping_timeout: float = 30.0

    #: Seconds of silence on a WebSocket connection before probing the peer
    #: with a PING.  See BB_WS_IDLE_TIMEOUT above.
    ws_idle_timeout: float = 300.0

    #: Seconds to wait for any inbound frame after a WebSocket liveness PING
    #: before closing with 1001.  See BB_WS_PONG_TIMEOUT above.
    ws_pong_timeout: float = 30.0

    #: Maximum size (bytes) of one inbound MQTT control packet, checked on
    #: the declared Remaining Length before the payload is buffered and
    #: advertised in CONNACK.  See BB_MQTT_MAX_PACKET_SIZE above.
    mqtt_max_packet_size: int = 1024 * 1024

    #: The broker's own Receive Maximum (§3.2.2.3.3), advertised in CONNACK.
    mqtt_receive_maximum: int = 64

    #: Per-session bound on QoS>0 messages held while the client's Receive
    #: Maximum window is full.  See BB_MQTT_MAX_QUEUED_MESSAGES above.
    mqtt_max_queued_messages: int = 1000

    #: Maximum number of topics holding a retained message.  See
    #: BB_MQTT_MAX_RETAINED above.
    mqtt_max_retained: int = 10000

    #: Per-session bound on the number of Topic Filters a session holds.
    #: See BB_MQTT_MAX_SUBSCRIPTIONS above.
    mqtt_max_subscriptions: int = 1000

    #: Total bound on the number of sessions the broker retains.  See
    #: BB_MQTT_MAX_SESSIONS above.
    mqtt_max_sessions: int = 10000

    #: Maximum size (bytes) of a message as the *application* receives it —
    #: post-reassembly, post-inflation.  The frame cap above bounds one
    #: compressed frame on the wire; this bounds what that frame becomes.
    #: See BB_WS_MAX_MESSAGE_SIZE above.  Default 16 MiB, ``0`` disables.
    ws_max_message_size: int = 16 * 1024 * 1024

    #: Per-connection asyncio.Semaphore cap on running stream handlers when
    #: running with a single worker (0 = disabled).  Defaults to 20 so that
    #: high-mux connections (e.g. -m 50) do not saturate the single event loop
    #: with too many concurrent tasks — benchmarks show mux-10 outperforms mux-50
    #: on a single worker without this cap.
    h2_active_streams_1w: int = 20

    #: Per-connection asyncio.Semaphore cap on running stream handlers when
    #: running with multiple workers (0 = disabled).  SO_REUSEPORT distributes
    #: connections across workers, but each worker still runs a single event loop.
    #: At mux-50 with ~4 connections per worker the uncapped task count (4×50=200)
    #: exceeds the optimum and causes scheduler overhead similar to single-worker.
    #: Default 20 matches BB_H2_ACTIVE_STREAMS_1W so both paths behave consistently.
    h2_active_streams: int = 20

    #: Use uvloop as the asyncio event loop (requires ``pip install blackbull[speed]``).
    #: When True the uvloop EventLoopPolicy is installed before each ``asyncio.run()``
    #: call.  Falls back to the standard asyncio loop with a warning if uvloop is not
    #: installed.
    use_uvloop: bool = False

    #: Minimum body size (bytes) for CompressionMiddleware to bother compressing.
    compression_min_size: int = 100

    #: Body size threshold (bytes) above which compression runs in a thread-pool
    #: executor so the event loop stays responsive.  0 = always on event loop (disable offloading).
    compression_executor_threshold: int = 65536  # 64 KiB

    #: Max concurrent compression offloads to the asyncio executor.  When at
    #: this cap, eligible responses are served **uncompressed** rather than
    #: queued.  0 disables the cap (unbounded queue — pre-0.29 behaviour;
    #: vulnerable to executor saturation under burst load).
    #: Default is set in get_settings() to ``os.cpu_count() * 2``.
    compression_max_inflight: int = 0

    #: Brotli quality level (0–11) for dynamic-response compression.  The
    #: brotli library's own default is 11 (max compression, designed for
    #: build-time / static pre-compression) — too expensive on the request
    #: path for tiny dynamic payloads.  4 matches Google's and Cloudflare's
    #: recommendation for dynamic content; 5 matches Apache mod_brotli's
    #: default; 6 matches nginx ngx_brotli's default.  Raise to 11 only when
    #: producing pre-compressed sibling assets out-of-band, not on live
    #: responses.
    brotli_quality: int = 4

    #: Per-worker CPU pinning policy.  ``auto`` (default) gives worker *i* the
    #: *i*-th CPU of the mask the process already carries; ``off`` leaves
    #: placement to the operator; an explicit ``taskset``-style list
    #: (``2,4,6-9`` — ``0`` is CPU 0, not the off switch) confines workers to
    #: those CPUs.  Multi-worker only — a single-worker server is never
    #: pinned.  See blackbull/server/affinity.py.
    cpu_pinning: str = 'auto'

    #: Cooperative yield interval for the HTTP/2 frame loop.  After this many
    #: stream tasks are spawned without a natural yield, ``asyncio.sleep(0)``
    #: is inserted so the event loop can dispatch queued tasks.
    #: 0 = disabled (legacy behaviour).
    frame_yield_every: int = 8


@_functools.cache
def get_settings() -> Settings:
    """Read environment variables and return an immutable :class:`Settings`.

    Cached: first call parses env vars and builds the dataclass; subsequent
    calls return the same instance.  Settings are server-process-wide
    configuration, not per-request data — there's no reason to re-parse
    ``os.environ`` on every request.  Profile showed ``_int_env`` and
    ``_int_env_nonneg`` consuming ~5–6% of CPU in the HTTP/1.1 hot path
    before this cache.

    Tests that mutate environment between cases must call
    :func:`reset_settings_cache` in their teardown.
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
        # Defaults match the Linux kernel baseline.  See
        # docs/reference/env-vars.md "Performance recommendations"
        # for the values to override these with on a tuned deployment.
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
        force_asgi_scope=_bool_env('BB_FORCE_ASGI_SCOPE', False),
        body_chunk_size=_int_env('BB_BODY_CHUNK_SIZE', 65536),
        body_chunk_max=_int_env('BB_BODY_CHUNK_MAX', 524288),
        max_body_size=_int_env_nonneg('BB_MAX_BODY_SIZE', 31457280),
        min_body_rate=_float_env_nonneg('BB_MIN_BODY_RATE', 240.0),
        min_body_rate_grace=_float_env_nonneg('BB_MIN_BODY_RATE_GRACE', 5.0),
        # RFC 9113 §6.9.2 default initial window size.  See
        # docs/reference/env-vars.md "Performance recommendations" for the
        # values commonly used on tuned production deployments.
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
        mqtt_max_retained=_int_env_nonneg('BB_MQTT_MAX_RETAINED', 10000),
        mqtt_max_subscriptions=_int_env_nonneg(
            'BB_MQTT_MAX_SUBSCRIPTIONS', 1000),
        mqtt_max_sessions=_int_env_nonneg('BB_MQTT_MAX_SESSIONS', 10000),
        use_uvloop=_bool_env('BB_UVLOOP', False),
        h2_active_streams_1w=_int_env_nonneg('BB_H2_ACTIVE_STREAMS_1W', 20),
        h2_active_streams=_int_env_nonneg('BB_H2_ACTIVE_STREAMS', 20),
        compression_min_size=_int_env('BB_COMPRESSION_MIN_SIZE', 100),
        compression_executor_threshold=_int_env_nonneg('BB_COMPRESSION_EXECUTOR_THRESHOLD', 65536),
        compression_max_inflight=_int_env_nonneg(
            'BB_COMPRESSION_MAX_INFLIGHT', max((os.cpu_count() or 1) * 2, 4)),
        brotli_quality=_int_env_nonneg('BB_BROTLI_QUALITY', 4),
        frame_yield_every=_int_env_nonneg('BB_FRAME_YIELD_EVERY', 8),
        cpu_pinning=_str_env('BB_CPU_PINNING', 'auto'),
    )


def reset_settings_cache() -> None:
    """Clear the cached :class:`Settings`.

    Call this in test teardown if the test mutated env vars that
    :func:`get_settings` reads.  Without this, the cached settings reflect
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
