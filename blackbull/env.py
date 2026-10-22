"""Immutable environment settings snapshot, declared once per setting.

Each ``Settings`` field is the whole declaration of one environment variable:
its name, type, default, range, config-file key and description.
[`get_settings`][], ``blackbull run --config`` and the reference page all read
them from here.  After changing the environment in a test, call
reset_settings_cache().
"""
import dataclasses
import functools as _functools
import os
import resource
from collections.abc import Callable
from enum import StrEnum
from typing import Any


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


#: Reserved so that running out of descriptors refuses a *new* connection,
#: which the peer can retry, instead of stranding a request already
#: accepted that can no longer open its database connection.
FD_RESERVE = 64


DEFAULT_COMPRESSION_MAX_INFLIGHT = max((os.cpu_count() or 1) * 2, 4)


class DerivedCap(int):
    """A connection cap ``auto`` derived from ``RLIMIT_NOFILE``, not one an
    operator named; the server may lower it to the descriptors left."""


def resolve_max_connections(raw: str | None) -> int:
    """Resolve ``BB_MAX_CONNECTIONS`` — ``auto``, ``0``, or a number.

    ``auto`` (the default) derives the cap from this process's own
    ``RLIMIT_NOFILE``, less a reserve.  An explicit number is honoured as
    given, *not* clamped to that budget: an operator who names a number
    means it.  ``0`` disables the cap.

    Why a derived default is safe to ship, and what it does not bound, is
    argued in ``docs/about/security-model.md`` (qualification 2).
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
        return DerivedCap(max(1, soft - FD_RESERVE))
    try:
        value = int(raw)
    except ValueError:
        return resolve_max_connections('auto')
    if value < 0:
        return resolve_max_connections('auto')
    return value


def _environment(raw: str | None) -> 'Environment':
    try:
        return Environment((raw or Environment.DEVELOPMENT).lower())
    except ValueError:
        return Environment.DEVELOPMENT


def setting(env: str, default: Any, *, section: str, doc: str,
            minimum: int | float | None = None,
            parse: Callable[[str | None], Any] | None = None,
            config: tuple[str, str] | None = None,
            shown: str | None = None) -> Any:
    """Declare one environment variable as a ``Settings`` field.

    A value that does not parse, or is below *minimum*, falls back to
    *default*.  *parse* replaces that rule and also receives ``None`` when the
    variable is unset.  *config* is the ``[section] key`` that ``blackbull run
    --config`` maps onto *env*; *shown* is the default as the reference page
    states it, when it is a rule rather than a value.
    """
    return dataclasses.field(default=default, metadata={
        'env': env, 'section': section, 'doc': doc, 'minimum': minimum,
        'parse': parse, 'config': config, 'shown': shown})


@dataclasses.dataclass(frozen=True)
class Settings:
    """Immutable snapshot of all runtime settings.

    Construct via [`get_settings`][] rather than directly so that
    environment variables are read at the right time.  A directly
    constructed ``Settings`` leaves ``max_connections`` uncapped.
    """

    _section = 'Runtime and processes'
    env: Environment = setting(
        'BLACKBULL_ENV', Environment.DEVELOPMENT, parse=_environment, section=_section,
        doc='`production` | `development` | `test`.  In `production`, `StaticFiles` declines to serve files (production should sit behind nginx/Caddy for static assets), and the default error handler returns a terse response without exception details.')
    workers: int = setting(
        'BB_WORKERS', 1, minimum=1, config=('server', 'workers'), section=_section,
        doc='Pre-fork worker count.  `0` resolves to `os.cpu_count()`.  Combine with `BB_SOCKET_REUSEPORT=1`, except under `--reload` or on a port-bound non-ASGI protocol, which keep one listener.  See [Workers](../deployment/workers.md).')
    use_uvloop: bool = setting(
        'BB_UVLOOP', False, section=_section,
        doc="Install `uvloop`'s asyncio policy at startup.  Requires `pip install 'blackbull[speed]'`; falls back to the standard loop with a warning when uvloop is missing.")
    force_asgi_scope: bool = setting(
        'BB_FORCE_ASGI_SCOPE', False, section=_section,
        doc='Exercise the native-to-ASGI compatibility conversion on every request. Leave off for normal native serving.')
    cpu_pinning: str = setting(
        'BB_CPU_PINNING', 'auto', section=_section,
        doc='Linux multi-worker event-loop CPU placement. `auto` assigns workers within the inherited CPU mask; `off` disables pinning; a taskset-style list intersects that mask (`0` means CPU 0). Executor threads keep the full mask. Single-worker servers are not pinned. Use `off` when placement is managed externally.')

    _section = 'Warm-up'
    warmup_budget_s: float = setting(
        'BB_WARMUP_BUDGET_S', 60.0, minimum=0, section=_section,
        doc='Hard wall-clock cap (seconds) on total warm-up.  A hook that overruns is cancelled and the master proceeds to bind — warm-up is best-effort and never blocks boot indefinitely.')
    warmup_tls_n: int = setting(
        'BB_WARMUP_TLS_N', 64, minimum=0, section=_section,
        doc="Number of in-memory (`ssl.MemoryBIO`) TLS handshakes the framework performs to prime the OpenSSL/RSA/ALPN path, when the listener terminates TLS.  Runs automatically after the app's own warm-up hooks; `0` disables it.")

    _section = 'Connection limits and timeouts'
    max_connections: int = setting(
        'BB_MAX_CONNECTIONS', 0, parse=resolve_max_connections, shown='auto', config=('limits', 'max_connections'), section=_section,
        doc='Concurrent TCP connections **per worker**. `auto` subtracts a 64-descriptor reserve and already-open descriptors from `RLIMIT_NOFILE`; `0` disables the cap. Explicit numbers are not clamped to the descriptor budget. Counted from accept, including TLS handshakes. Excess connections wait in the accept queue; HTTP/1.1 and prior-knowledge h2c receive best-effort `503` with `Retry-After: 1`; ALPN-h2 and raw connections close silently. During lifespan startup the backlog applies instead. Set an explicit cap if the application opens descriptors later. See [startup admission](../deployment/unix-and-fd.md#the-startup-window).')
    request_timeout: float = setting(
        'BB_REQUEST_TIMEOUT', 0.0, minimum=0, config=('limits', 'request_timeout'), section=_section,
        doc='Seconds allowed for HTTP/1.1 handler dispatch or an HTTP/2 stream handler. A breach cancels the handler; HTTP/1.1 answers 408 and closes, HTTP/2 sends RST_STREAM(CANCEL) while sibling streams survive. `0` disables. Choose a budget compatible with intentional long-lived requests.')
    header_timeout: float = setting(
        'BB_HEADER_TIMEOUT', 10.0, minimum=0, config=('limits', 'header_timeout'), section=_section,
        doc="Seconds to complete an HTTP/1.1 request head (408 and close), an HTTP/2 field block (GOAWAY ENHANCE_YOUR_CALM), or a TLS handshake. TLS connections count from accept, including port-bound protocols. `0` disables this deadline; TLS then uses the event loop's own handshake timeout.")
    body_timeout: float = setting(
        'BB_BODY_TIMEOUT', 30.0, minimum=0, section=_section,
        doc="Seconds per HTTP/1.1 request-body read after the head. A breach surfaces `http.disconnect` and tears down the connection; `0` disables. Does not bound an HTTP/2 stream's body reads: use its request deadline and body rate/size bounds.")
    write_timeout: float = setting(
        'BB_WRITE_TIMEOUT', 30.0, minimum=0, section=_section,
        doc='Seconds the server will wait for a single response socket drain, `sendfile` chunk, or HTTP/2 flow-control wait.  This is per progress wait, not a whole response total; `0` disables.')
    keep_alive_timeout: float = setting(
        'BB_KEEP_ALIVE_TIMEOUT', 5.0, minimum=0, config=('server', 'keep_alive_timeout'), section=_section,
        doc='Seconds an idle HTTP/1.1 keep-alive connection is held open after a complete response.  Lower for high-fan-in deployments; higher for chatty clients on slow links.')
    tcp_user_timeout_ms: int = setting(
        'BB_TCP_USER_TIMEOUT_MS', 0, minimum=0, config=('server', 'tcp_user_timeout_ms'), section=_section,
        doc='`TCP_USER_TIMEOUT` socket option (Linux): per-connection upper bound on how long an unacknowledged sent segment lingers before the kernel kills the connection, evicting dead peers behind NATs without waiting for keepalives.  `0` keeps the kernel default.  See [Per-path scope](../deployment/workers.md#socket-options-across-bind-paths).')
    header_max_line: int = setting(
        'BB_HEADER_MAX_LINE', 8192, minimum=0, config=('limits', 'header_max_line'), section=_section,
        doc='Maximum bytes in an HTTP/1.1 request-line or field line. Oversized request lines receive 400; oversized header fields receive 431.')
    header_max_total: int = setting(
        'BB_HEADER_MAX_TOTAL', 65536, minimum=0, config=('limits', 'header_max_total'), section=_section,
        doc='Maximum bytes in the HTTP/1.1 request-line and header block. A request-line budget breach yields `400`; header-field overflow yields `431`.')
    body_chunk_size: int = setting(
        'BB_BODY_CHUNK_SIZE', 65536, minimum=1, section=_section,
        doc='Maximum read slice in bytes for chunked HTTP/1.1 request bodies, independent of peer-declared chunk size. Must be positive; invalid values use the default. Content-Length bodies use `BB_BODY_CHUNK_MAX` instead.')
    body_chunk_max: int = setting(
        'BB_BODY_CHUNK_MAX', 524288, minimum=1, section=_section,
        doc='Maximum bytes materialized by one Content-Length body read. Reads return available bytes without waiting to fill the slice. Does not cap the total body or the chunked path. `0` is raised to `1`.')
    max_body_size: int = setting(
        'BB_MAX_BODY_SIZE', 31457280, minimum=0, section=_section,
        doc='Total request-body bytes on HTTP/1.1 and HTTP/2. Declared over-cap lengths are refused before reading; undeclared totals are refused while accumulating. HTTP/1.1 sends 413 and closes. HTTP/2 sends 413 plus RST_STREAM(NO_ERROR) for a declared excess, or RST_STREAM(ENHANCE_YOUR_CALM) for a streaming excess; sibling streams survive. `0` disables.')
    min_body_rate: float = setting(
        'BB_MIN_BODY_RATE', 240.0, minimum=0, section=_section,
        doc='Minimum request-body bytes per second over a sliding grace-width window. Below the floor: HTTP/1.1 disconnects and closes; HTTP/2 resets the stream with ENHANCE_YOUR_CALM. A per-read timeout alone does not stop a trickle. `0` disables.')
    min_body_rate_grace: float = setting(
        'BB_MIN_BODY_RATE_GRACE', 5.0, minimum=0, section=_section,
        doc='Seconds before the request-body rate floor applies; also its sliding-window width. HTTP/1.1 counts transport wait time, excluding slow handler work. HTTP/2 counts wall time from first DATA but exempts peers blocked by our closed inbound window.')
    stream_queue_depth: int = setting(
        'BB_STREAM_QUEUE_DEPTH', 64, minimum=1, config=('server', 'stream_queue_depth'), section=_section,
        doc='Frame-count queue depth for HTTP/2 bodies without consume-time credit (direct/push fallback). Native request streams instead use the advertised byte window plus a frame-count abuse cap of 16 times this depth; this knob does not replace those bounds. A stream past either is reset with `RST_STREAM ENHANCE_YOUR_CALM`.')
    ws_queue_depth: int = setting(
        'BB_WS_QUEUE_DEPTH', 0, minimum=1, config=('server', 'ws_queue_depth'), section=_section,
        doc='WebSocket read-ahead depth. `0` reads inline in the handler task; positive values buffer up to N messages in a background reader. A websocket_message observer does not force read-ahead: the idle watchdog starts a deferred reader when the handler goes quiet. See [WebSockets](../guide/websockets.md).')

    _section = 'Async HTTP client'
    client_head_max_total: int = setting(
        'BB_CLIENT_HEAD_MAX_TOTAL', 65536, minimum=0, section=_section,
        doc='HTTP/1.1 response-head bytes including status line and terminator; excess raises `ResponseTooLarge`. HTTP/2 separately caps encoded field-block accumulation (connection COMPRESSION_ERROR) and decoded fields across informational, final and trailer sections on one stream (stream refusal). Per-section decoded HPACK accounting is `BB_CLIENT_H2_MAX_HEADER_LIST_SIZE`. `0` disables.')
    client_head_max_line: int = setting(
        'BB_CLIENT_HEAD_MAX_LINE', 8192, minimum=0, section=_section,
        doc='Bytes per HTTP/1.1 status, field, chunk-size or trailer line. Excess raises `ResponseTooLarge`. `0` disables; `BB_CLIENT_HEAD_MAX_TOTAL` separately bounds head accumulation.')
    client_head_timeout: float = setting(
        'BB_CLIENT_HEAD_TIMEOUT', 30.0, minimum=0, section=_section,
        doc='Seconds per HTTP/1.1 response head; expiry raises `TimeoutError` and retires the connection. On HTTP/2 it separately bounds request-on-wire to final HEADERS (stream CANCEL), and an opened field block to END_HEADERS (connection ENHANCE_YOUR_CALM). Interims do not restart the HTTP/2 response-start clock. Long polls or work before headers must fit this budget. `0` disables all three waits.')
    client_body_timeout: float = setting(
        'BB_CLIENT_BODY_TIMEOUT', 30.0, minimum=0, section=_section,
        doc='Seconds per response-body progress wait, not per whole response. HTTP/1.1 covers one payload arrival or complete framing operation. HTTP/2 applies per stream from final HEADERS, restarting only on non-empty DATA. Expiry raises `TimeoutError`; a trickle needs `BB_CLIENT_MIN_BODY_RATE`. `0` disables.')
    client_write_timeout: float = setting(
        'BB_CLIENT_WRITE_TIMEOUT', 30.0, minimum=0, section=_section,
        doc="Seconds per client socket drain or HTTP/2 flow-control credit wait, separate from server `BB_WRITE_TIMEOUT`. No whole-upload deadline: repeated small credit grants can extend an upload indefinitely. H2 writes use the peer's frame-size setting; `BB_CLIENT_H2_MAX_FRAME_SIZE` is receive-only. `0` disables.")
    client_body_max_total: int = setting(
        'BB_CLIENT_BODY_MAX_TOTAL', 0, minimum=0, section=_section,
        doc='Buffered response-body bytes on HTTP/1.1 and HTTP/2; excess raises `ResponseTooLarge`. Declared excess is refused before reading, other excess before retaining it. `0` disables. Budget at least twice this value plus per-slice overhead per concurrent response because joining retains both slices and result. HTTP/1.1 `stream()` bypasses accumulation and this cap but exposes neither status nor headers; HTTP/2 has no streaming API.')
    client_min_body_rate: float = setting(
        'BB_CLIENT_MIN_BODY_RATE', 0.0, minimum=0, section=_section,
        doc='Minimum sustained response-body payload rate in **bytes per second**, after `BB_CLIENT_MIN_BODY_RATE_GRACE`.  The numerator is DATA payload only (HTTP/2 padding and frame overhead are excluded); the denominator is per-stream wait time, including empty/padded DATA and terminal DATA/trailing HEADERS settlement.  The window starts at the first non-empty body payload, so think time after final HEADERS is excluded.  HTTP/1.1 abandons its connection; HTTP/2 resets only the offending stream with `RST_STREAM(CANCEL)`.  **Off by default**; `0` disables.')
    client_min_body_rate_grace: float = setting(
        'BB_CLIENT_MIN_BODY_RATE_GRACE', 5.0, minimum=0, section=_section,
        doc='Seconds of body-read waiting, after the first body octet, before `BB_CLIENT_MIN_BODY_RATE` is enforced.  Also the width of the window the rate is averaged over: it rolls forward whenever it is satisfied, so a burst buys the window it happened in rather than the whole response.  Under HTTP/2 this is per response stream.')
    client_ws_max_frame_payload: int = setting(
        'BB_CLIENT_WS_MAX_FRAME_PAYLOAD', 67108864, minimum=0, section=_section,
        doc='Client WebSocket inbound payload bytes per frame (unit).  Excess closes with 1009 and raises `ProtocolError` from `receive()`.  `BB_CLIENT_WS_MAX_MESSAGE_SIZE` owns the aggregate message total.  There is no client-owned WebSocket time bound here: the H1 session has no environment time owner, and H2 per-call receive timeouts are caller arguments, not this setting.  `0` disables this unit cap.')
    client_ws_max_message_size: int = setting(
        'BB_CLIENT_WS_MAX_MESSAGE_SIZE', 16777216, minimum=0, section=_section,
        doc='Client WebSocket inbound payload bytes per reassembled/inflated message (total).  Excess closes with 1009 and raises `ProtocolError` from `receive()`.  `BB_CLIENT_WS_MAX_FRAME_PAYLOAD` owns the per-frame unit.  There is no client-owned WebSocket time bound here: the H1 session has no environment time owner, and H2 per-call receive timeouts are caller arguments, not this setting.  `0` disables this total cap.')
    client_max_interim_responses: int = setting(
        'BB_CLIENT_MAX_INTERIM_RESPONSES', 8, minimum=0, section=_section,
        doc='Interim 1xx heads discarded before the final response on either transport; 101 is final and not counted. Excess raises `ResponseTooLarge`. `0` disables. HTTP/1.1 spends the head byte/time budgets afresh per interim; HTTP/2 counts empty sections too and keeps its response-start deadline running.')
    client_raw_queue_depth: int = setting(
        'BB_CLIENT_RAW_QUEUE_DEPTH', 1024, minimum=0, section=_section,
        doc='Frames queued per raw HTTP/2 stream. Only DATA, RST_STREAM and HEADERS enter it. Overflow discards backlog, returns connection credit and resets only that stream with ENHANCE_YOUR_CALM, waking its consumer. DATA bytes remain consume-credited; depth additionally bounds empty frames. This is not a rate bound. `0` disables.')
    client_h2_max_frame_size: int = setting(
        'BB_CLIENT_H2_MAX_FRAME_SIZE', 16384, minimum=0, section=_section,
        doc='Inbound HTTP/2 frame payload bytes checked before reading. Excess ends the connection with FRAME_SIZE_ERROR; unread payload cannot safely be skipped as a stream error. `0` disables. The client does not advertise a changed frame size: keep the RFC initial value outside fault-injection scenarios.')
    client_h2_max_header_list_size: int = setting(
        'BB_CLIENT_H2_MAX_HEADER_LIST_SIZE', 65536, minimum=0, section=_section,
        doc='Decoded HTTP/2 bytes per field section, counted as name + value + 32 per field. Advertised as SETTINGS_MAX_HEADER_LIST_SIZE and enforced inside HPACK decoding; excess ends the connection with COMPRESSION_ERROR. `0` disables advertisement and opens the decoder limit. Separate from per-stream `BB_CLIENT_HEAD_MAX_TOTAL`.')
    client_h2_enable_push: bool = setting(
        'BB_CLIENT_H2_ENABLE_PUSH', True, section=_section,
        doc='HTTP/2 push policy. `1` decodes and discards promises without advertising a change. `0` advertises ENABLE_PUSH=0 and refuses promises with connection PROTOCOL_ERROR after its SETTINGS acknowledgement; in-flight promises before ACK remain legal. Promised blocks are always decoded to preserve shared HPACK state. No push-consuming API is provided.')

    _section = 'Fault injection'
    production: bool = setting(
        'BB_PRODUCTION', False, section=_section,
        doc='Set to `1` to make the fault-injection servers refuse to start, in addition to the refusal that `BLACKBULL_ENV=production` already triggers.')

    _section = 'Socket tuning'
    socket_backlog: int = setting(
        'BB_SOCKET_BACKLOG', 1024, minimum=1, config=('server', 'socket_backlog'), section=_section,
        doc="`listen()` backlog depth, sized for connection bursts; Linux caps the effective value at `net.core.somaxconn`.  Where BlackBull binds, **this value bounds the accept queue**; an adopted fd keeps its creator's `Backlog=` until accepting begins; see [The startup window](../deployment/unix-and-fd.md#the-startup-window).  During that window a TCP client beyond the backlog is served late and a non-blocking `AF_UNIX` client is refused ([details](../deployment/unix-and-fd.md#what-a-client-beyond-the-backlog-observes)); an `AF_UNIX` queue found full when accepting opens is logged on `blackbull.caps`.")
    socket_reuseport: bool = setting(
        'BB_SOCKET_REUSEPORT', False, config=('server', 'socket_reuseport'), section=_section,
        doc='Give each worker a separate TCP listening socket on supported systems. AF_UNIX is not rebound and cannot use this shape with multiple workers unless reload keeps a shared listener. An adopted fd whose supervisor retains ownership cannot be split this way. `0` keeps the kernel default. See [socket scope](../deployment/workers.md#socket-options-across-bind-paths).')
    socket_sndbuf: int = setting(
        'BB_SOCKET_SNDBUF', 0, minimum=0, config=('server', 'socket_sndbuf'), section=_section,
        doc="`SO_SNDBUF` (bytes) on BlackBull's own listeners and the TCP connections accepted from them; `0` keeps the kernel default.  Linux clamps at `net.core.wmem_max` and doubles.  See [Per-path scope](../deployment/workers.md#socket-options-across-bind-paths).")
    socket_rcvbuf: int = setting(
        'BB_SOCKET_RCVBUF', 0, minimum=0, config=('server', 'socket_rcvbuf'), section=_section,
        doc='`SO_RCVBUF` (bytes) in the same scope as `BB_SOCKET_SNDBUF`, against `net.core.rmem_max` instead and doubled by the same rule.  `0` keeps the kernel default.')

    _section = 'Logging'
    access_log: bool = setting(
        'BB_ACCESS_LOG', True, config=('logging', 'access_log'), section=_section,
        doc='Emit one record on the `blackbull.access` logger per completed request.  Set to `0` to skip access-log formatting (useful during benchmarks).')
    async_logging: bool = setting(
        'BB_ASYNC_LOGGING', True, config=('logging', 'async_logging'), section=_section,
        doc='Install a `QueueHandler` on the `blackbull` logger so `logger.debug/info` calls from the event loop are non-blocking.')
    log_format: str = setting(
        'BB_LOG_FORMAT', '', config=('logging', 'log_format'), section=_section,
        doc='Set to `json` to emit one structured JSON object per log line.  Access-log records expose `client_ip`, `method`, `path`, `http_version`, `status`, `response_bytes`, `duration_ms` (plus `close_code` on WebSocket disconnect) as top-level keys; every record carries `timestamp`, `level`, `logger`, `message`.  Applies to the default sink installed by async logging.  Unset means plain text.')
    log_syslog_addr: str = setting(
        'BB_SYSLOG_ADDR', '', config=('logging', 'syslog_addr'), section=_section,
        doc='`host:port` of a syslog/UDP collector (e.g. `127.0.0.1:514`).  When set, the async-logging sink ships records via a UDP `SysLogHandler` instead of `stderr`.  Composes with `BB_LOG_FORMAT=json`.  An unparseable value falls back to `stderr` with a warning.')
    log_file: str = setting(
        'BB_LOG_FILE', '', config=('logging', 'file'), section=_section,
        doc='Append-file path for the async logging sink, opened separately in each worker after fork. Ignored with syslog. An unopenable path warns and falls back to stderr. Composes with JSON formatting and batch size.')
    log_batch_size: int = setting(
        'BB_LOG_BATCH_SIZE', 64, minimum=1, config=('logging', 'batch_size'), section=_section,
        doc='Records per async stream/file batch, floored at 2. Partial batches flush after `BB_LOG_BATCH_TIMEOUT_MS`. Use `BB_ASYNC_LOGGING=0` for synchronous per-record flushes. Syslog remains one datagram per record.')
    log_batch_timeout_ms: int = setting(
        'BB_LOG_BATCH_TIMEOUT_MS', 5, minimum=1, config=('logging', 'batch_timeout_ms'), section=_section,
        doc='Max time a partial batch waits before it is flushed, bounding log-visibility latency at low request rates.')

    _section = 'Static files'
    static_stat_ttl_s: float = setting(
        'BB_STATIC_STAT_TTL_S', 1.0, minimum=0, section=_section,
        doc='Seconds between cached-body validations in `StaticFiles`. Requests still stat the target; its changed body may remain cached until the TTL expires. `0` validates each request. Applies only with `cache=True`, separately in each worker.')

    _section = 'HTTP/2 internals'
    h2_initial_window_size: int = setting(
        'BB_H2_INITIAL_WINDOW_SIZE', 65535, minimum=1, config=('server', 'h2_initial_window_size'), section=_section,
        doc='Per-stream inbound flow-control window advertised in initial SETTINGS. Larger values allow more in-flight data per stream before WINDOW_UPDATE.')
    h2_connection_window_size: int = setting(
        'BB_H2_CONNECTION_WINDOW_SIZE', 65535, minimum=1, config=('server', 'h2_connection_window_size'), section=_section,
        doc='Inbound connection-level flow-control window opened by initial WINDOW_UPDATE. Values below 65535 are ignored.')
    h2_max_concurrent_streams: int = setting(
        'BB_H2_MAX_CONCURRENT_STREAMS', 100, minimum=1, config=('server', 'h2_max_concurrent_streams'), section=_section,
        doc='`SETTINGS_MAX_CONCURRENT_STREAMS` (RFC 9113 §6.5.2 id `0x3`).  Streams beyond the cap receive `RST_STREAM REFUSED_STREAM` and are not dispatched.')
    worker_drain_timeout: float = setting(
        'BB_WORKER_DRAIN_TIMEOUT', 8.0, minimum=0, section=_section,
        doc="Seconds to let accepted connections finish after SIGTERM before cancelling remaining work, in single- and multi-worker serving. `0` cancels immediately. Keep this below the supervisor's stop budget. See [Shutdown](../deployment/workers.md#shutdown).")
    h2_active_streams: int = setting(
        'BB_H2_ACTIVE_STREAMS', 20, minimum=0, config=('server', 'h2_active_streams'), section=_section,
        doc='Per-connection `asyncio.Semaphore` cap on stream handlers actually running concurrently, under multi-worker.  Prevents one high-mux connection from saturating a single event loop.  `0` disables (no cap beyond `BB_H2_MAX_CONCURRENT_STREAMS`).')
    h2_active_streams_1w: int = setting(
        'BB_H2_ACTIVE_STREAMS_1W', 20, minimum=0, config=('server', 'h2_active_streams_1w'), section=_section,
        doc='Same as above, but used when `BB_WORKERS=1`.')
    frame_yield_every: int = setting(
        'BB_FRAME_YIELD_EVERY', 8, minimum=0, config=('server', 'frame_yield_every'), section=_section,
        doc='Stream tasks spawned before the HTTP/2 frame loop yields cooperatively. `0` disables yielding.')

    _section = 'WebSocket'
    ws_permessage_deflate: bool = setting(
        'BB_WS_PERMESSAGE_DEFLATE', True, config=('server', 'ws_permessage_deflate'), section=_section,
        doc='Negotiate `permessage-deflate` (RFC 7692) on the inbound handshake when the peer offers it.')
    ws_max_frame_payload: int = setting(
        'BB_WS_MAX_FRAME_PAYLOAD', 67108864, minimum=0, section=_section,
        doc='Maximum declared payload length of a single inbound frame, checked against the header **before** any payload byte is read — RFC 6455 §5.2 permits a peer to advertise 2<sup>63</sup>−1.  Exceeding it closes with 1009 (Message Too Big).')
    ws_max_message_size: int = setting(
        'BB_WS_MAX_MESSAGE_SIZE', 16777216, minimum=0, section=_section,
        doc='Inbound WebSocket message bytes after reassembly and decompression. Excess closes with 1009 and logs ws_max_message_size without materializing the oversized message. `0` disables. Lower this if the application does not need large messages; a frame cap alone cannot bound inflated or fragmented totals.')
    ws_idle_timeout: float = setting(
        'BB_WS_IDLE_TIMEOUT', 300.0, minimum=0, section=_section,
        doc='Seconds of WebSocket silence before a server liveness PING. Any inbound frame answers; no answer within `BB_WS_PONG_TIMEOUT` closes with 1001. Server-side only; clients do not probe. `0` disables.')
    ws_pong_timeout: float = setting(
        'BB_WS_PONG_TIMEOUT', 30.0, minimum=0, section=_section,
        doc='Seconds to wait for any inbound frame after a liveness PING, then close with 1001. Applies only with a non-zero `BB_WS_IDLE_TIMEOUT`.')
    h2_enable_websocket: bool = setting(
        'BB_H2_ENABLE_WEBSOCKET', False, config=('server', 'h2_enable_websocket'), section=_section,
        doc='Advertise ENABLE_CONNECT_PROTOCOL=1 for RFC 8441 WebSocket. Review `BB_MAX_CONNECTIONS` and `BB_H2_WS_MAX_STREAMS_PER_CONNECTION` before exposing this path. A proxy forwarding HTTP/1.1 does not provide Extended CONNECT.')
    frame_rate_limit: int = setting(
        'BB_FRAME_RATE_LIMIT', 20, minimum=0, section=_section,
        doc='Control frames per type, per connection, per rolling window. Meters H2 RST_STREAM (inbound and server-emitted), PING, SETTINGS, empty CONTINUATION/DATA and WebSocket controls. Excess: H2 GOAWAY(ENHANCE_YOUR_CALM), WS close 1008, and frame_rate cap log. `0` disables.')
    frame_rate_window: float = setting(
        'BB_FRAME_RATE_WINDOW', 1.0, minimum=0, section=_section,
        doc='Width in seconds of the rolling window `BB_FRAME_RATE_LIMIT` counts within.')
    h2_idle_timeout: float = setting(
        'BB_H2_IDLE_TIMEOUT', 300.0, minimum=0, section=_section,
        doc='Seconds of HTTP/2 silence before a server liveness PING. Any inbound frame answers. Idle connections are probed, not closed immediately. `0` disables probing.')
    h2_ping_timeout: float = setting(
        'BB_H2_PING_TIMEOUT', 30.0, minimum=0, section=_section,
        doc='Seconds to wait for any frame after a liveness PING before concluding the peer is gone and closing with `GOAWAY(NO_ERROR)`.  Only meaningful when `BB_H2_IDLE_TIMEOUT` is non-zero.')
    h2_ws_max_streams_per_connection: int = setting(
        'BB_H2_WS_MAX_STREAMS_PER_CONNECTION', 5, minimum=0, section=_section,
        doc='Maximum concurrent WebSocket (RFC 8441 Extended CONNECT) streams per HTTP/2 connection.  Caps the per-connection blast radius of WS-over-H2 stream-exhaustion attacks.  Excess streams receive `RST_STREAM REFUSED_STREAM`.  `0` disables the cap (no upper bound beyond `BB_H2_MAX_CONCURRENT_STREAMS`).  Only meaningful when `BB_H2_ENABLE_WEBSOCKET=1`.')

    _section = 'MQTT'
    mqtt_max_packet_size: int = setting(
        'BB_MQTT_MAX_PACKET_SIZE', 1048576, minimum=0, section=_section,
        doc='Maximum inbound packet size including its fixed header, advertised as `Maximum Packet Size` (§3.2.2.3.6) and checked before waiting for the body. Exceeding it closes silently before CONNECT admission, otherwise with `DISCONNECT` **0x95 (Packet Too Large)**. `0` disables.')
    mqtt_broker_inbox_maxsize: int = setting(
        'BB_MQTT_BROKER_INBOX_MAXSIZE', 1024, minimum=1, section=_section,
        doc="Positive bound on messages waiting in the worker's MQTT broker inbox. Readers await admission at capacity; the broker logs the cap hit. Independent of session QoS backlog. Invalid values, including zero, use the default.")
    mqtt_broker_inbox_max_bytes: int = setting(
        'BB_MQTT_BROKER_INBOX_MAX_BYTES', 16777216, minimum=1, section=_section,
        doc="Positive wire-size budget for broker inbox contents, alongside the count cap. Compact ACK/lifecycle envelopes charge one byte and one slot. A single packet exceeding the budget closes the connection: silently before CONNECT admission, otherwise with `DISCONNECT 0x97`. Not a heap budget; active processing and each reader's admission candidate are outside the queue. Invalid values use the default.")
    mqtt_connection_inbox_maxsize: int = setting(
        'BB_MQTT_CONNECTION_INBOX_MAXSIZE', 1024, minimum=1, section=_section,
        doc='Positive bound on all packets waiting for each MQTT writer, including QoS 0 and ACKs. The producer yields to the writer at capacity, then logs and ends only that connection if it cannot admit the packet; it never waits for socket progress inside the broker. Invalid values, including zero, use the default.')
    mqtt_connection_inbox_max_bytes: int = setting(
        'BB_MQTT_CONNECTION_INBOX_MAX_BYTES', 16777216, minimum=1, section=_section,
        doc='Positive encoded-byte budget for each MQTT writer inbox. The packet object is retained alongside encoded bytes. One active write is outside this queue budget and uses `BB_WRITE_TIMEOUT`. Tune with the accepted packet size; an individually larger packet cannot fit even in an empty queue.  Like the count cap, a packet that cannot be admitted ends only that connection. Invalid values use the default.')
    mqtt_receive_maximum: int = setting(
        'BB_MQTT_RECEIVE_MAXIMUM', 64, minimum=0, section=_section,
        doc="Advertised inbound QoS>0 in-flight maximum. This relies on client compliance and is not an enforced admission gate. Outbound delivery enforces the client's Receive Maximum and uses `BB_MQTT_MAX_QUEUED_MESSAGES` for backlog.")
    mqtt_max_queued_messages: int = setting(
        'BB_MQTT_MAX_QUEUED_MESSAGES', 1000, minimum=0, section=_section,
        doc="QoS>0 backlog per session while the client's Receive Maximum window is full. At the cap, refuse newest and log; retain older promised messages. `0` disables.")
    mqtt_max_retained: int = setting(
        'BB_MQTT_MAX_RETAINED', 10000, minimum=0, section=_section,
        doc='Retained topics. At capacity refuse new stored topics; updates, deletion and live delivery still work. QoS 1/2 report 0x97 in PUBACK/PUBREC; QoS 0 has no acknowledgement. Use QoS ≥ 1 to learn whether storage succeeded. Every refusal logs. `0` disables.')
    mqtt_max_subscriptions: int = setting(
        'BB_MQTT_MAX_SUBSCRIPTIONS', 1000, minimum=0, section=_section,
        doc='Topic Filters per session. New filters at capacity receive SUBACK 0x97 and a cap log; replacing an existing filter remains allowed. `0` disables.')
    mqtt_max_sessions: int = setting(
        'BB_MQTT_MAX_SESSIONS', 10000, minimum=0, section=_section,
        doc='Retained sessions after expired entries are swept. At capacity unknown Client Identifiers receive CONNACK 0x97 and close; resuming an existing session remains allowed. Never-expiring sessions consume this total without a clock bound. `0` disables.')

    _section = 'gRPC'
    grpc_max_message_size: int = setting(
        'BB_GRPC_MAX_MESSAGE_SIZE', 4194304, minimum=0, section=_section,
        doc="Largest gRPC message accepted on a call.  Matches grpcio's own default receive limit, so a client tuned against grpcio meets the same ceiling here.  A value that does not parse falls back to the default rather than failing the import.")
    grpc_stream_batch_bytes: int = setting(
        'BB_GRPC_STREAM_BATCH_BYTES', 16384, minimum=0, section=_section,
        doc='Response-message bytes gathered before a streaming write. Flush partial batches when the producer suspends, at call end and before trailing status; DATA boundaries do not delimit gRPC messages.')
    grpc_compress_min_bytes: int = setting(
        'BB_GRPC_COMPRESS_MIN_BYTES', 1024, minimum=0, section=_section,
        doc="Responses below this size are sent uncompressed.  gzip's header and trailer can make a small message *larger*, so the threshold avoids spending CPU for no bandwidth.  Set it very high to disable response compression outright.")

    _section = 'Compression'
    compression_min_size: int = setting(
        'BB_COMPRESSION_MIN_SIZE', 100, minimum=1, config=('limits', 'compression_min_size'), section=_section,
        doc='Minimum body size in bytes below which the `Compression` middleware skips compression entirely.')
    compression_executor_threshold: int = setting(
        'BB_COMPRESSION_EXECUTOR_THRESHOLD', 65536, minimum=0, config=('limits', 'compression_executor_threshold'), section=_section,
        doc='Body size above which compression is offloaded to a thread-pool executor so the event loop stays responsive during the (CPU-bound) compress call.  `0` always compresses on the event loop.')
    compression_max_inflight: int = setting(
        'BB_COMPRESSION_MAX_INFLIGHT', DEFAULT_COMPRESSION_MAX_INFLIGHT, minimum=0, shown='max(cpu_count() * 2, 4)', section=_section,
        doc='Maximum concurrent compression offloads. At the cap, serve eligible responses uncompressed rather than queue them. `0` disables admission limiting.')
    brotli_quality: int = setting(
        'BB_BROTLI_QUALITY', 4, minimum=0, section=_section,
        doc='Dynamic-response Brotli quality (0–11). Reserve the highest levels for build-time precompression, not request handling.')

    _section = 'Diagnostic timing'
    phase_trace: bool = setting(
        'BB_PHASE_TRACE', False, section=_section,
        doc='Record per-request wall-clock and CPU checkpoints in access-log `phases`. Off by default; enable for diagnostics.')
    deadline_tick_ms: float = setting(
        'BB_DEADLINE_TICK_MS', 300.0, minimum=0, section=_section,
        doc='Milliseconds between shared deadline-scanner ticks. Smaller values tighten enforcement granularity at a CPU cost; larger values allow more deadline slack.')

    del _section

    @property
    def header_deadline(self) -> float | None:
        """``header_timeout`` as a deadline: ``None`` when it is disabled."""
        return self.header_timeout or None


def _read(field: dataclasses.Field) -> Any:
    meta = field.metadata
    raw = os.environ.get(meta['env'])
    if meta['parse'] is not None:
        return meta['parse'](raw)
    if raw is None:
        return field.default
    if field.type is bool:
        return raw.strip().lower() not in ('0', 'false', 'no', 'off')
    if field.type is str:
        return raw
    try:
        value = field.type(raw)
    except ValueError:
        return field.default
    minimum = meta['minimum']
    return value if minimum is None or value >= minimum else field.default


_FIELDS = {f.name: f for f in dataclasses.fields(Settings)}


def read_setting(name: str) -> Any:
    """One ``Settings`` field read from the environment now, uncached.

    Use it at import and in logging: [`get_settings`][] at import would cache
    values before ``blackbull run --config`` exports them.
    """
    return _read(_FIELDS[name])


@_functools.cache
def get_settings() -> Settings:
    """Return cached process-wide settings.

    Environment changes after the first call have no effect until
    reset_settings_cache(); tests changing them must reset in teardown.
    """
    return Settings(**{name: _read(f) for name, f in _FIELDS.items()})


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
