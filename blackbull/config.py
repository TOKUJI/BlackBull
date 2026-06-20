"""Declarative application configuration — :class:`AppConfig`.

``AppConfig`` is a small, typed, immutable holder for the server-facing
settings that :meth:`blackbull.BlackBull.run` (and :func:`blackbull.serve`)
already accept as keyword arguments.  It lets an application declare those
settings once::

    from blackbull import BlackBull, AppConfig

    app = BlackBull(config=AppConfig(
        port=8443,
        certfile='cert.pem',
        keyfile='key.pem',
        workers=4,
    ))

    if __name__ == '__main__':
        app.run()          # picks up the config; no flags to thread through

Resolution precedence in :meth:`BlackBull.run` is, highest to lowest:

1.  an explicit keyword argument to ``run(...)`` — ``app.run(port=9000)``
    always wins;
2.  the value declared on the bound :class:`AppConfig` (if any);
3.  the built-in default baked into :func:`blackbull.serve`.

``AppConfig`` deliberately mirrors *only* the parameters ``serve`` already
exposes — it is not a general-purpose settings store.  Per-request and
server-tuning knobs (window sizes, timeouts, queue depths beyond the two
listed here, …) continue to live in :mod:`blackbull.env` and are sourced
from ``BB_*`` environment variables.

``host`` is intentionally absent: BlackBull's socket layer binds dual-stack
on all interfaces (see :meth:`blackbull.server.ASGIServer.open_socket`), so
a per-interface ``host`` field would silently do nothing.  Use ``unix_path``
or ``inherited_fd`` for non-TCP binds.
"""
from __future__ import annotations

import dataclasses


@dataclasses.dataclass(frozen=True)
class AppConfig:
    """Immutable, declarative startup configuration for a BlackBull app.

    Every field corresponds one-to-one with a keyword argument of
    :func:`blackbull.serve` / :meth:`blackbull.BlackBull.run`.  Fields left
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

    #: ``asyncio.Queue`` depth for inbound WebSocket events per connection.
    #: ``None`` defers to ``BB_WS_QUEUE_DEPTH``.
    ws_queue_depth: int | None = None

    #: Enable auto-reload (requires the ``[reload]`` extra).
    reload: bool = False

    #: Directories / files to watch under auto-reload.  ``None`` watches the
    #: current working directory.
    reload_paths: list[str] | None = None
