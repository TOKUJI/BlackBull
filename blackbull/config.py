"""Immutable startup configuration. Explicit run() arguments take precedence
over BLACKBULL_* environment values, .env entries, AppConfig and defaults.
See docs/guide/configuration.md for sentinel values and BB_* tuning.
"""
from __future__ import annotations

import dataclasses
import logging
import os
from typing import Any

from .env import setting

logger = logging.getLogger('blackbull.config')


def _parse_bool(value: str) -> bool:
    """Parse a ``BLACKBULL_RELOAD``-style truthy string (``1/true/yes/on``)."""
    return value.strip().lower() in ('1', 'true', 'yes', 'on')


_DEPLOYMENT = 'Deployment'


@dataclasses.dataclass(frozen=True)
class AppConfig:
    """Immutable, declarative startup configuration for a BlackBull app.

    Every field corresponds one-to-one with a keyword argument of
    [`blackbull.serve`][blackbull.serve] / [`blackbull.BlackBull.run`][blackbull.BlackBull.run].  Fields left
    at their sentinel default (``None``, or ``0`` for ``port``, or ``False``
    for ``reload``) defer to ``serve``'s own built-in default unless an
    explicit ``run(...)`` argument overrides them.
    """

    certfile: str | None = setting(
        'BLACKBULL_CERT', None, parse=str, section=_DEPLOYMENT,
        doc='TLS certificate file.  Enables HTTPS, and therefore HTTP/2 via ALPN, when paired with the key.')
    keyfile: str | None = setting(
        'BLACKBULL_KEY', None, parse=str, section=_DEPLOYMENT,
        doc='TLS private key file.  Required alongside the certificate for HTTPS and HTTP/2.')
    port: int = setting(
        'BLACKBULL_PORT', 0, parse=int, section=_DEPLOYMENT,
        doc='TCP port to bind.  `0` lets the OS pick a free ephemeral port.')
    unix_path: str | None = setting(
        'BLACKBULL_UNIX_PATH', None, parse=str, section=_DEPLOYMENT,
        doc='AF_UNIX socket path.  Mutually exclusive with a TCP port bind.')

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

    reload: bool = setting(
        'BLACKBULL_RELOAD', False, parse=_parse_bool, section=_DEPLOYMENT,
        doc='Enable auto-reload (`1`, `true`, `yes` or `on`; requires the `[reload]` extra).')

    #: Directories / files to watch under auto-reload.  ``None`` watches the
    #: current working directory.
    reload_paths: list[str] | None = None


#: ``run()`` parameters and their built-in defaults; an ``AppConfig`` field
#: left at its default does not override the environment.
_DEFAULTS: dict[str, Any] = {
    f.name: f.default for f in dataclasses.fields(AppConfig)}

#: The deploy-time settings a ``BLACKBULL_*`` variable can supply.  Tuning
#: knobs come from ``BB_*`` in [`blackbull.env`][blackbull.env] instead.
_ENV_VARS: dict[str, dataclasses.Field] = {
    f.name: f for f in dataclasses.fields(AppConfig) if 'env' in f.metadata}


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
            env_name = _ENV_VARS[name].metadata['env']
            coerce = _ENV_VARS[name].metadata['parse']
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
    """Log non-default deployment settings from env, .env or AppConfig; omit explicit call arguments.
    """
    if not logger.isEnabledFor(logging.INFO):
        return
    for name in _ENV_VARS:
        src = sources.get(name)
        if src in (None, 'argument', 'default'):
            continue
        logger.info('config: %s=%r (from %s)', name, resolved[name], src)
