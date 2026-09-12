"""Runtime configuration sourced from environment variables.

All server settings live in [`Settings`][].  [`get_settings`][] reads the
environment once and returns an immutable snapshot; [`reset_settings_cache`][]
drops it, which is what a test that changes the environment needs.

The environment-variable reference lists every variable, its default and what
it bounds -- server settings, the separately-defaulted ``BB_CLIENT_*`` block,
and the few knobs read straight off the environment rather than through
[`Settings`][].  ``scripts/check_env_table.py`` fails when the code reads a
variable that page does not list, or lists one nothing reads.
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
