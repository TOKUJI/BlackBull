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
    response, not a silent reset).  ``0`` disables the cap and relies
    on the OS file-descriptor limit instead.  Default: ``0`` (uncapped).
    Production deployments on untrusted hosts should set this to a
    finite ceiling — 1024 is a sensible single-loop value; multi-worker
    deployments multiply (so ``workers=8`` × ``BB_MAX_CONNECTIONS=1024``
    → 8K connections per process).
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
BB_WS_PERMESSAGE_DEFLATE
    Negotiate ``permessage-deflate`` (RFC 7692) on incoming WebSocket
    handshakes when the peer offers it.  Matches modern browsers and
    major WebSocket libraries.  Default: ``true``.
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
BB_SESSION_SECRET
    HMAC secret used by the :class:`~blackbull.middleware.session.Session`
    middleware to sign cookies.  Pass ``secret=`` to the constructor or
    set this env var; if neither is set, ``Session()`` construction
    raises (no insecure default).  Default: *(unset)*.
"""
import dataclasses
import functools as _functools
import os
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
    #: Default raised from 0 (disabled) to 1024 in Sprint 30 (event-loop
    #: integrity).  Unbounded per-worker concurrency lets a single
    #: client (or burst, or slowloris-class workload) park thousands of
    #: suspended-readuntil tasks on the event loop, amplifying drain
    #: time on burst-close and inflating worst-case latency.  Set
    #: ``BB_MAX_CONNECTIONS`` to a finite ceiling on untrusted hosts;
    #: 1024 is a typical single-asyncio-loop ceiling, and multi-worker
    #: deployments multiply (so ``workers=8`` × ``BB_MAX_CONNECTIONS=1024``
    #: → 8K connections per process).
    max_connections: int = 0

    #: asyncio.Queue depth for HTTP/2 per-stream request-body events.
    stream_queue_depth: int = 64

    #: asyncio.Queue depth for WebSocket inbound events per connection.
    ws_queue_depth: int = 256

    #: Install QueueHandler on the blackbull logger so event-loop log calls are non-blocking.
    async_logging: bool = True

    #: Emit one access log record per completed request on blackbull.access.
    access_log: bool = True

    #: listen() backlog depth for the server socket.  128 is the
    #: traditional kernel ``SOMAXCONN`` default.  Production deployments
    #: under burst load should raise this — see
    #: docs/reference/env-vars.md "Performance recommendations".
    socket_backlog: int = 128

    #: SO_SNDBUF for accepted sockets (0 = leave kernel default).
    socket_sndbuf: int = 0

    #: SO_RCVBUF for accepted sockets (0 = leave kernel default).
    socket_rcvbuf: int = 0

    #: Use SO_REUSEPORT to give each worker its own kernel accept queue.
    #: Off by default — only meaningful under ``workers > 1``.  Production
    #: multi-worker deployments should enable it; see
    #: docs/reference/env-vars.md "Performance recommendations".
    socket_reuseport: bool = False

    #: Idle timeout (seconds) on a keep-alive connection that is awaiting
    #: the *next* request.  Replaces per-accept ``SO_KEEPALIVE`` syscalls
    #: with an application-level timer — same ghost-eviction guarantee,
    #: zero syscall cost per accept (which was a measurable contributor
    #: to wrk c=1024-burst connect-RST errors).  Combined with
    #: ``TCP_USER_TIMEOUT`` on the listening socket (inherits to accepted)
    #: which handles the *active-but-stuck* case.  0 disables the timer.
    #:
    #: Default lowered from 60 s to 5 s in Sprint 30 (event-loop
    #: integrity).  60 s parks ghost / idle connections in the loop's
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

    #: Negotiate ``permessage-deflate`` (RFC 7692) on incoming WebSocket
    #: handshakes when the peer offers it.  On by default — matches modern
    #: browsers and the major library defaults (`ws` for Node, Python
    #: `websockets`, aiohttp).  Set ``BB_WS_PERMESSAGE_DEFLATE=0`` to disable.
    ws_permessage_deflate: bool = True

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
        max_connections=_int_env_nonneg('BB_MAX_CONNECTIONS', 0),
        stream_queue_depth=_int_env('BB_STREAM_QUEUE_DEPTH', 64),
        ws_queue_depth=_int_env('BB_WS_QUEUE_DEPTH', 256),
        async_logging=_bool_env('BB_ASYNC_LOGGING', True),
        access_log=_bool_env('BB_ACCESS_LOG', True),
        # Defaults match the Linux kernel baseline.  See
        # docs/reference/env-vars.md "Performance recommendations"
        # for the values to override these with on a tuned deployment.
        socket_backlog=_int_env('BB_SOCKET_BACKLOG', 128),
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
        # RFC 9113 §6.9.2 default initial window size.  See
        # docs/reference/env-vars.md "Performance recommendations" for the
        # values commonly used on tuned production deployments.
        h2_initial_window_size=_int_env('BB_H2_INITIAL_WINDOW_SIZE', 65535),
        h2_connection_window_size=_int_env('BB_H2_CONNECTION_WINDOW_SIZE', 65535),
        h2_max_concurrent_streams=_int_env('BB_H2_MAX_CONCURRENT_STREAMS', 100),
        h2_enable_websocket=_bool_env('BB_H2_ENABLE_WEBSOCKET', False),
        ws_permessage_deflate=_bool_env('BB_WS_PERMESSAGE_DEFLATE', True),
        use_uvloop=_bool_env('BB_UVLOOP', False),
        h2_active_streams_1w=_int_env_nonneg('BB_H2_ACTIVE_STREAMS_1W', 20),
        h2_active_streams=_int_env_nonneg('BB_H2_ACTIVE_STREAMS', 20),
        compression_min_size=_int_env('BB_COMPRESSION_MIN_SIZE', 100),
        compression_executor_threshold=_int_env_nonneg('BB_COMPRESSION_EXECUTOR_THRESHOLD', 65536),
        compression_max_inflight=_int_env_nonneg(
            'BB_COMPRESSION_MAX_INFLIGHT', max((os.cpu_count() or 1) * 2, 4)),
        brotli_quality=_int_env_nonneg('BB_BROTLI_QUALITY', 4),
        frame_yield_every=_int_env_nonneg('BB_FRAME_YIELD_EVERY', 8),
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
