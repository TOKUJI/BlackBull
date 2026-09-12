"""Declarative application configuration -- [`AppConfig`][].

A small, typed, immutable holder for the server-facing settings that
[`blackbull.BlackBull.run`][blackbull.BlackBull.run] and
[`blackbull.serve`][blackbull.serve] accept as keyword arguments, so an
application can declare them once instead of threading flags through.

An explicit argument still wins over the config, and an environment variable
wins over both; the Configuration guide gives the order in full and shows
what each source looks like.
"""
from __future__ import annotations

import dataclasses
import logging
import os
from collections.abc import Callable
from typing import Any

logger = logging.getLogger('blackbull.config')


@dataclasses.dataclass(frozen=True)
class AppConfig:
    """Immutable, declarative startup configuration for a BlackBull app.

    Every field corresponds one-to-one with a keyword argument of
    [`blackbull.serve`][blackbull.serve] / [`blackbull.BlackBull.run`][blackbull.BlackBull.run].  Fields left
    at their sentinel default (``None``, or ``0`` for ``port``, or ``False``
    for ``reload``) defer to ``serve``'s own built-in default unless an
    explicit ``run(...)`` argument overrides them.
    """

    #: TLS certificate file.  Enables HTTPS — and therefore HTTP/2 via ALPN —
    #: when paired with ``keyfile``.
    certfile: str | None = None

    #: TLS private key file.  Required alongside ``certfile`` for HTTPS / HTTP/2.
    keyfile: str | None = None

    #: TCP port to bind.  ``0`` lets the OS pick a free ephemeral port.
    port: int = 0

    #: AF_UNIX socket path.  Mutually exclusive with a TCP ``port`` bind.
    unix_path: str | None = None

    #: Inherited listening socket fd (systemd-style socket activation).
    inherited_fd: int | None = None

    #: Number of worker processes.  ``None`` defers to ``BB_WORKERS``;
    #: ``0`` resolves to ``os.cpu_count()``.
    workers: int | None = None

    #: Per-worker connection cap.  ``None`` defers to ``BB_MAX_CONNECTIONS``.
    max_connections: int | None = None

    #: ``asyncio.Queue`` depth for HTTP/2 per-stream events.  ``None`` defers
    #: to ``BB_STREAM_QUEUE_DEPTH``.
    stream_queue_depth: int | None = None

    #: WebSocket inbound read-ahead depth.  ``0`` reads inline in the handler
    #: task; a positive value runs a background reader with a queue of that
    #: depth.  ``None`` defers to ``BB_WS_QUEUE_DEPTH`` (default ``0``).
    ws_queue_depth: int | None = None

    #: Enable auto-reload (requires the ``[reload]`` extra).
    reload: bool = False

    #: Directories / files to watch under auto-reload.  ``None`` watches the
    #: current working directory.
    reload_paths: list[str] | None = None


def _parse_bool(value: str) -> bool:
    """Parse a ``BLACKBULL_RELOAD``-style truthy string (``1/true/yes/on``)."""
    return value.strip().lower() in ('1', 'true', 'yes', 'on')


# Deploy-time ``run()`` settings resolvable from ``BLACKBULL_*`` environment
# variables: ``{field: (env var, coercer)}``.  Server-tuning knobs (``workers``,
# ``max_connections``, the queue depths, timeouts, window sizes) are sourced
# from ``BB_*`` in [`blackbull.env`][blackbull.env] and are deliberately *not* duplicated
# here — ``BLACKBULL_*`` is the deployment namespace, ``BB_*`` the tuning one.
_ENV_VARS: dict[str, tuple[str, Callable[[str], Any]]] = {
    'certfile':  ('BLACKBULL_CERT', str),
    'keyfile':   ('BLACKBULL_KEY', str),
    'port':      ('BLACKBULL_PORT', int),
    'unix_path': ('BLACKBULL_UNIX_PATH', str),
    'reload':    ('BLACKBULL_RELOAD', _parse_bool),
}

# Sentinel defaults for every ``run()`` parameter, mirroring ``serve``'s own
# built-in defaults.  Used as the lowest-precedence fallback and to decide
# whether an ``AppConfig`` field was actually declared (vs. left at its default).
_DEFAULTS: dict[str, Any] = {
    'certfile': None, 'keyfile': None, 'port': 0, 'unix_path': None,
    'inherited_fd': None, 'workers': None, 'max_connections': None,
    'stream_queue_depth': None, 'ws_queue_depth': None,
    'reload': False, 'reload_paths': None,
}


def _load_dotenv_values() -> dict[str, str]:
    """Return ``.env`` values from the working directory.

    Reads the file without mutating ``os.environ`` so the real process
    environment keeps precedence (``env`` > ``.env``).  Returns ``{}`` when
    ``python-dotenv`` is not installed (the ``[dotenv]`` extra) or no ``.env``
    file is present — ``BLACKBULL_*`` resolution from the real environment still
    works either way.
    """
    try:
        from dotenv import dotenv_values  # noqa: PLC0415
    except ImportError:
        return {}
    return {k: v for k, v in dotenv_values('.env').items() if v is not None}


def resolve_run_config(
    explicit: dict[str, Any],
    config: AppConfig | None,
) -> tuple[dict[str, Any], dict[str, str]]:
    """Resolve every ``run()`` setting and record where each value came from.

    Precedence, highest to lowest:

    1. an explicit ``run(...)`` keyword argument (anything not ``None``);
    2. a ``BLACKBULL_*`` environment variable (for the deploy-time settings in
       ``_ENV_VARS``);
    3. a ``BLACKBULL_*`` entry in a ``.env`` file (``[dotenv]`` extra);
    4. the bound [`AppConfig`][] field, if declared (non-default);
    5. the built-in default.

    Returns ``(resolved, sources)`` where *resolved* maps each ``run()``
    parameter to its final value and *sources* maps it to a short provenance
    label (``argument`` / ``$BLACKBULL_PORT`` / ``.env`` / ``AppConfig`` /
    ``default``) for startup logging.
    """
    dotenv = _load_dotenv_values()
    resolved: dict[str, Any] = {}
    sources: dict[str, str] = {}

    for name, default in _DEFAULTS.items():
        given = explicit.get(name)
        if given is not None:
            resolved[name], sources[name] = given, 'argument'
            continue

        if name in _ENV_VARS:
            env_name, coerce = _ENV_VARS[name]
            raw = os.environ.get(env_name)
            if raw is not None:
                resolved[name], sources[name] = coerce(raw), f'${env_name}'
                continue
            if env_name in dotenv:
                resolved[name], sources[name] = coerce(dotenv[env_name]), '.env'
                continue

        if config is not None:
            cfg_val = getattr(config, name)
            if cfg_val != default:
                resolved[name], sources[name] = cfg_val, 'AppConfig'
                continue

        resolved[name], sources[name] = default, 'default'

    return resolved, sources


def log_config_sources(resolved: dict[str, Any], sources: dict[str, str]) -> None:
    """Log one INFO line per deploy setting that was configured non-trivially.

    Only the ``BLACKBULL_*``-resolvable deploy settings whose value came from
    an environment variable, ``.env``, or an [`AppConfig`][] are reported —
    explicit call-site arguments (the author already knows them) and plain
    defaults are left silent to keep startup quiet.  Paths are logged; secrets
    are not (a ``keyfile`` path is configuration, its contents never touch the
    log).
    """
    if not logger.isEnabledFor(logging.INFO):
        return
    for name in _ENV_VARS:
        src = sources.get(name)
        if src in (None, 'argument', 'default'):
            continue
        logger.info('config: %s=%r (from %s)', name, resolved[name], src)
