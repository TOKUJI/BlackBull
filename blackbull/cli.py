"""``blackbull`` console-script entry point.

Resolves a ``module:attribute`` import path to an ASGI 3.0 callable
(typically a :class:`blackbull.BlackBull` instance, but any ASGI app
works) and serves it via :func:`blackbull.app.serve`.

Usage::

    blackbull myapp:app --bind 0.0.0.0:8443 \\
              --certfile cert.pem --keyfile key.pem --workers 4

Flags mirror :func:`blackbull.app.serve` kwargs; anything left
unspecified falls back to the matching ``BB_*`` environment variable
(see :mod:`blackbull.env`).

This module is registered as the ``blackbull`` console script via
``[project.scripts]`` in ``pyproject.toml``.  The in-Python entry
``app.serve(...)`` is unchanged and remains the preferred path for
embedded callers (notebooks, test harnesses, ``examples/*.py``).
"""
from __future__ import annotations

import argparse
import importlib
import sys
from typing import Any

from .app import serve as _serve


# ``host:port`` is the only address syntax in v1 of the CLI.  Sprint 12
# extends this to ``unix:/path/to/sock`` and ``fd://N`` — keep the
# parser permissive enough to add those without a flag rename.
_DEFAULT_BIND = '127.0.0.1:8000'


def _split_bind(spec: str) -> tuple[str, int]:
    """Parse ``host:port`` into ``(host, port)``.

    Accepts bracketed IPv6 (``[::]:8443``) and empty host (``:8443``,
    meaning "all interfaces").  BlackBull binds dual-stack on every
    interface for any host today; specific-interface binding lands in
    Sprint 12 with the UDS / fd-inheritance work.
    """
    if spec.startswith('['):
        # IPv6 literal: [::]:port
        end = spec.find(']')
        if end == -1 or not spec[end + 1:].startswith(':'):
            raise ValueError(f"--bind {spec!r} — bracketed IPv6 must be [host]:port")
        host = spec[1:end]
        port_s = spec[end + 2:]
    else:
        if ':' not in spec:
            raise ValueError(f"--bind {spec!r} — expected host:port")
        host, _, port_s = spec.rpartition(':')
    try:
        port = int(port_s)
    except ValueError:
        raise ValueError(f"--bind {spec!r} — port {port_s!r} is not an integer") from None
    if not 0 <= port <= 65535:
        raise ValueError(f"--bind {spec!r} — port {port} out of range")
    return host, port


def _import_app(spec: str) -> Any:
    """Resolve ``module.path:attribute`` into the named attribute.

    Mirrors uvicorn / hypercorn / granian convention.  The attribute
    may be any ASGI callable; we don't introspect or wrap.  Raises
    :class:`SystemExit` (via the caller's ``main`` wrapper) on import
    or attribute failure with a focused message — argparse's own error
    output is reserved for syntactic problems.
    """
    if ':' not in spec:
        raise ValueError(
            f"{spec!r} is not a valid app path — expected 'module:attribute' "
            "(e.g. 'myapp:app' or 'bench.peers.asgi_app:app')"
        )
    module_path, _, attr = spec.partition(':')
    if not module_path or not attr:
        raise ValueError(
            f"{spec!r} is not a valid app path — both module and attribute "
            "must be non-empty"
        )

    # Match uvicorn / hypercorn / granian: a CLI invocation expects to
    # import from the working directory.  Prepend '' (== cwd at lookup
    # time) so ``blackbull myapp:app`` works without PYTHONPATH gymnastics.
    if '' not in sys.path:
        sys.path.insert(0, '')

    try:
        module = importlib.import_module(module_path)
    except ImportError as exc:
        raise ImportError(
            f"could not import module {module_path!r}: {exc}"
        ) from exc
    if not hasattr(module, attr):
        raise AttributeError(
            f"module {module_path!r} has no attribute {attr!r}"
        )
    return getattr(module, attr)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog='blackbull',
        description='Run an ASGI 3.0 application with the BlackBull server.',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument(
        'app',
        help="Application import path as 'module:attribute' (e.g. 'myapp:app').",
    )
    p.add_argument(
        '--bind', default=_DEFAULT_BIND, metavar='HOST:PORT',
        help='Address to bind.  Host is advisory in v1 (BlackBull binds '
             'dual-stack on all interfaces); only the port is honoured.  '
             'Sprint 12 adds unix:/path and fd://N.',
    )
    p.add_argument(
        '--certfile', default=None, metavar='PATH',
        help='TLS certificate file.  Required for HTTPS / HTTP/2.',
    )
    p.add_argument(
        '--keyfile', default=None, metavar='PATH',
        help='TLS private key file.  Required for HTTPS / HTTP/2.',
    )
    p.add_argument(
        '--workers', type=int, default=None, metavar='N',
        help='Number of worker processes.  0 = os.cpu_count().  '
             'Defaults to BB_WORKERS (=1).',
    )
    p.add_argument(
        '--max-connections', type=int, default=None, metavar='N',
        help='Per-worker connection cap.  0 = unlimited.  '
             'Defaults to BB_MAX_CONNECTIONS (=0).',
    )
    p.add_argument(
        '--stream-queue-depth', type=int, default=None, metavar='N',
        help='asyncio.Queue depth for HTTP/2 per-stream events.  '
             'Defaults to BB_STREAM_QUEUE_DEPTH (=64).',
    )
    p.add_argument(
        '--ws-queue-depth', type=int, default=None, metavar='N',
        help='asyncio.Queue depth for inbound WebSocket events per '
             'connection.  Defaults to BB_WS_QUEUE_DEPTH (=256).',
    )
    p.add_argument(
        '--reload', action='store_true',
        help='Enable auto-reload (watchfiles).  Requires the [reload] extra. '
             "Master watches *.py files under --reload-path and re-execs "
             'itself with listening sockets preserved.',
    )
    p.add_argument(
        '--reload-path', dest='reload_paths', action='append', default=None,
        metavar='PATH',
        help='Directory or file to watch.  May be passed multiple times.  '
             'Default: the current working directory.',
    )
    return p


def main(argv: list[str] | None = None) -> int:
    """Entry point invoked by the ``blackbull`` console script.

    Returns a process exit code.  argparse handles ``--help`` and
    invalid-flag exits itself (status 2); application-level errors
    (bad import path, bind syntax) print to stderr and exit 1.
    """
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        host, port = _split_bind(args.bind)
    except ValueError as exc:
        print(f'blackbull: {exc}', file=sys.stderr)
        return 1

    if host not in ('', '0.0.0.0', '::', '127.0.0.1', 'localhost'):
        # v1 of the CLI honours only the port — BlackBull's socket
        # layer binds dual-stack on all interfaces.  Warn so users
        # don't think their interface filter is in effect.
        print(
            f'blackbull: --bind host {host!r} is advisory in v1; '
            f'binding dual-stack on port {port} for now.',
            file=sys.stderr,
        )

    try:
        app = _import_app(args.app)
    except (ImportError, AttributeError, ValueError) as exc:
        print(f'blackbull: {exc}', file=sys.stderr)
        return 1

    _serve(
        app,
        certfile=args.certfile,
        keyfile=args.keyfile,
        port=port,
        workers=args.workers,
        max_connections=args.max_connections,
        stream_queue_depth=args.stream_queue_depth,
        ws_queue_depth=args.ws_queue_depth,
        reload=args.reload,
        reload_paths=args.reload_paths,
    )
    return 0


if __name__ == '__main__':  # pragma: no cover
    raise SystemExit(main())
