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
    Maximum simultaneous TCP connections accepted per worker.
    Default: ``500``.
BB_STREAM_QUEUE_DEPTH
    ``asyncio.Queue`` depth for HTTP/2 per-stream request-body events.
    Limits memory growth when an ASGI handler is slower than the client.
    Default: ``64``.
BB_WS_QUEUE_DEPTH
    ``asyncio.Queue`` depth for WebSocket inbound events per connection.
    Default: ``256``.
"""
import dataclasses
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

    #: Maximum simultaneous TCP connections per worker.
    max_connections: int = 500

    #: asyncio.Queue depth for HTTP/2 per-stream request-body events.
    stream_queue_depth: int = 64

    #: asyncio.Queue depth for WebSocket inbound events per connection.
    ws_queue_depth: int = 256


def get_settings() -> Settings:
    """Read environment variables and return an immutable :class:`Settings`."""
    raw_env = _str_env('BLACKBULL_ENV', 'development').lower()
    try:
        env = Environment(raw_env)
    except ValueError:
        env = Environment.DEVELOPMENT

    return Settings(
        env=env,
        workers=_int_env('BB_WORKERS', 1),
        max_connections=_int_env('BB_MAX_CONNECTIONS', 500),
        stream_queue_depth=_int_env('BB_STREAM_QUEUE_DEPTH', 64),
        ws_queue_depth=_int_env('BB_WS_QUEUE_DEPTH', 256),
    )
