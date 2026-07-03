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
``app.run(...)`` (synchronous) is the preferred path for embedded
callers (notebooks, test harnesses, ``examples/*.py``).
"""
from __future__ import annotations

import argparse
import importlib
import os
import sys
import tomllib
from typing import Any

from .app import serve as _serve


# ---------------------------------------------------------------------------
# TOML config loading — Sprint 12c
# ---------------------------------------------------------------------------

# Mapping: (section, key) → BB_* environment variable name.
# Values in the TOML file set the corresponding env var only when the var
# is not already present in os.environ (env vars always win over config).
_TOML_TO_ENV: dict[tuple[str, str], str] = {
    # [server]
    ('server', 'workers'):              'BB_WORKERS',
    ('server', 'stream_queue_depth'):   'BB_STREAM_QUEUE_DEPTH',
    ('server', 'ws_queue_depth'):       'BB_WS_QUEUE_DEPTH',
    ('server', 'socket_backlog'):       'BB_SOCKET_BACKLOG',
    ('server', 'socket_sndbuf'):        'BB_SOCKET_SNDBUF',
    ('server', 'socket_rcvbuf'):        'BB_SOCKET_RCVBUF',
    ('server', 'socket_reuseport'):     'BB_SOCKET_REUSEPORT',
    ('server', 'keep_alive_timeout'):   'BB_KEEP_ALIVE_TIMEOUT',
    ('server', 'tcp_user_timeout_ms'):  'BB_TCP_USER_TIMEOUT_MS',
    ('server', 'frame_yield_every'):    'BB_FRAME_YIELD_EVERY',
    ('server', 'h2_active_streams'):    'BB_H2_ACTIVE_STREAMS',
    ('server', 'h2_active_streams_1w'): 'BB_H2_ACTIVE_STREAMS_1W',
    ('server', 'h2_initial_window_size'):    'BB_H2_INITIAL_WINDOW_SIZE',
    ('server', 'h2_connection_window_size'): 'BB_H2_CONNECTION_WINDOW_SIZE',
    ('server', 'h2_max_concurrent_streams'): 'BB_H2_MAX_CONCURRENT_STREAMS',
    ('server', 'h2_enable_websocket'):  'BB_H2_ENABLE_WEBSOCKET',
    ('server', 'ws_permessage_deflate'): 'BB_WS_PERMESSAGE_DEFLATE',
    # [limits]
    ('limits', 'max_connections'):      'BB_MAX_CONNECTIONS',
    ('limits', 'request_timeout'):      'BB_REQUEST_TIMEOUT',
    ('limits', 'header_timeout'):       'BB_HEADER_TIMEOUT',
    ('limits', 'header_max_line'):      'BB_HEADER_MAX_LINE',
    ('limits', 'header_max_total'):     'BB_HEADER_MAX_TOTAL',
    ('limits', 'compression_min_size'): 'BB_COMPRESSION_MIN_SIZE',
    ('limits', 'compression_executor_threshold'): 'BB_COMPRESSION_EXECUTOR_THRESHOLD',
    # [logging]
    ('logging', 'access_log'):   'BB_ACCESS_LOG',
    ('logging', 'async_logging'): 'BB_ASYNC_LOGGING',
    ('logging', 'log_format'):    'BB_LOG_FORMAT',
    ('logging', 'syslog_addr'):   'BB_SYSLOG_ADDR',
    ('logging', 'batch_size'):    'BB_LOG_BATCH_SIZE',
    ('logging', 'batch_timeout_ms'): 'BB_LOG_BATCH_TIMEOUT_MS',
}


def _load_config(path: str) -> dict[str, str]:
    """Parse a TOML config file and return a ``{BB_VAR: str_value}`` dict.

    Only recognised ``(section, key)`` pairs are returned.  Unknown sections
    and keys are silently ignored so future TOML keys don't break old
    binaries.  Values are converted to their string representation because
    ``os.environ`` only accepts strings; the existing ``_int_env`` /
    ``_bool_env`` helpers in :mod:`blackbull.env` parse them back.

    *path* errors (missing file, permission denied, invalid TOML) raise
    :class:`SystemExit` via the caller.

    The special ``[tls] cert`` and ``[tls] key`` keys are returned under the
    synthetic names ``_certfile`` and ``_keyfile`` (leading underscore marks
    them as CLI-arg fallbacks, not env-var overrides) so the caller can apply
    them to ``args.certfile`` / ``args.keyfile`` when those args are ``None``.
    """
    try:
        with open(path, 'rb') as fh:
            data = tomllib.load(fh)
    except FileNotFoundError:
        raise FileNotFoundError(f"config file not found: {path!r}")
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"config file {path!r} — TOML parse error: {exc}") from exc

    result: dict[str, str] = {}

    for section, table in data.items():
        if not isinstance(table, dict):
            continue

        if section == 'tls':
            if 'cert' in table:
                result['_certfile'] = str(table['cert'])
            if 'key' in table:
                result['_keyfile'] = str(table['key'])
            continue

        for key, value in table.items():
            env_var = _TOML_TO_ENV.get((section, key))
            if env_var is None:
                continue
            if isinstance(value, bool):
                result[env_var] = '1' if value else '0'
            else:
                result[env_var] = str(value)

    return result


#: Parser result for ``--bind``: a tagged tuple where the first element
#: identifies the address kind and the rest is kind-specific.
#:
#:   ``('tcp', host, port)``    — ``--bind 0.0.0.0:8443``
#:   ``('unix', path)``         — ``--bind unix:/run/blackbull.sock``
#:   ``('fd', N)``              — ``--bind fd://3`` (systemd socket activation)
_DEFAULT_BIND = '127.0.0.1:8000'


def _split_bind(spec: str):
    """Parse a ``--bind`` value into a tagged address tuple.

    Recognised schemes:

    * ``host:port`` (and the IPv6 bracketed form ``[::]:port``) →
      ``('tcp', host, port)``.
    * ``unix:/absolute/path`` (or ``unix:relative/path``) →
      ``('unix', path)``.  AF_UNIX path length is OS-bound
      (Linux ≤ 108 bytes); we don't enforce that here so a useful
      bind() error reaches the user.
    * ``fd://N`` → ``('fd', N)``.  Adopts the inherited listening
      socket on file descriptor *N* (systemd-style activation; see
      :func:`blackbull.protocol.rsock.adopt_listening_fd`).

    Raises :class:`ValueError` with a focused message on syntactic
    problems.
    """
    if spec.startswith('fd://'):
        n = spec[len('fd://'):]
        if not n:
            raise ValueError(f"--bind {spec!r} — fd:// requires an fd number")
        try:
            fd = int(n)
        except ValueError:
            raise ValueError(
                f"--bind {spec!r} — fd {n!r} is not an integer"
            ) from None
        if fd < 0:
            raise ValueError(f"--bind {spec!r} — fd {fd} cannot be negative")
        return ('fd', fd)

    if spec.startswith('unix:'):
        path = spec[len('unix:'):]
        if not path:
            raise ValueError(f"--bind {spec!r} — unix: requires a path")
        return ('unix', path)

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
    return ('tcp', host, port)


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
    from . import __version__
    p = argparse.ArgumentParser(
        prog='blackbull',
        description='Run an ASGI 3.0 application with the BlackBull server.',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument(
        '--version', action='version',
        version=f'blackbull {__version__}',
    )
    p.add_argument(
        'app',
        help="Application import path as 'module:attribute' (e.g. 'myapp:app').",
    )
    p.add_argument(
        '--config', default=None, metavar='PATH',
        help='TOML configuration file.  Keys mirror BB_* env vars; env vars '
             'take precedence over the file.  See docs for the full schema.',
    )
    p.add_argument(
        '--bind', default=_DEFAULT_BIND, metavar='SPEC',
        help='Address to bind.  Either host:port (TCP — host is advisory '
             'in v1, BlackBull binds dual-stack) or unix:/abs/path (AF_UNIX). '
             'fd://N adopts an already-bound fd (systemd socket activation).',
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


# ---------------------------------------------------------------------------
# ``blackbull serve`` — zero-code static file server (Sprint 49, B4)
# ---------------------------------------------------------------------------

def _build_serve_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog='blackbull serve',
        description='Serve a directory of static files over HTTP/1.1 — and '
                    'HTTP/2 when TLS is supplied — with ETag / conditional '
                    'requests out of the box.  No application code required; '
                    'a drop-in upgrade over "python -m http.server".',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument(
        'directory', nargs='?', default='.', metavar='DIR',
        help='Directory to serve.  Defaults to the current directory.',
    )
    p.add_argument(
        '--bind', default=_DEFAULT_BIND, metavar='SPEC',
        help='Address to bind: host:port (TCP) or unix:/abs/path (AF_UNIX).',
    )
    p.add_argument(
        '--certfile', default=None, metavar='PATH',
        help='TLS certificate file.  Supplying --certfile and --keyfile '
             'enables HTTPS and HTTP/2 (via ALPN).',
    )
    p.add_argument(
        '--keyfile', default=None, metavar='PATH',
        help='TLS private key file.  Required alongside --certfile.',
    )
    p.add_argument(
        '--index', default='index.html', metavar='NAME',
        help="Filename served for directory requests.  Pass '' to disable.",
    )
    p.add_argument(
        '--no-etag', dest='etag', action='store_false',
        help='Disable ETag / If-None-Match (304) conditional responses.',
    )
    p.add_argument(
        '--cache', action='store_true',
        help='Hold file bodies in an in-memory LRU (faster, but does not '
             'pick up on-disk edits until the per-entry stat TTL expires).',
    )
    p.add_argument(
        '--workers', type=int, default=None, metavar='N',
        help='Number of worker processes.  0 = os.cpu_count().',
    )
    return p


def _build_static_app(directory: str, *, etag: bool, cache: bool,
                      index: str | None):
    """Build a :class:`blackbull.BlackBull` app that serves *directory*.

    Raises :class:`FileNotFoundError` / :class:`NotADirectoryError` when
    *directory* is not a usable directory so the caller can report a
    focused error rather than starting a server that 404s everything.
    """
    if not os.path.isdir(directory):
        if os.path.exists(directory):
            raise NotADirectoryError(f'not a directory: {directory!r}')
        raise FileNotFoundError(f'directory not found: {directory!r}')

    from . import BlackBull  # noqa: PLC0415

    app = BlackBull()
    if etag:
        # Cache is the outermost middleware (registered first) so a matching
        # If-None-Match short-circuits to 304 before StaticFiles touches the
        # disk; it also injects the generated ETag on the storing pass.
        from .middleware.cache import Cache  # noqa: PLC0415
        app.use(Cache())
    app.static('/', directory, cache=cache, index=index or None)
    return app


def _serve_static(argv: list[str]) -> int:
    parser = _build_serve_parser()
    args = parser.parse_args(argv)

    try:
        addr = _split_bind(args.bind)
    except ValueError as exc:
        print(f'blackbull: {exc}', file=sys.stderr)
        return 1

    port = 0
    unix_path: str | None = None
    if addr[0] == 'tcp':
        _, host, port = addr
        if host not in ('', '0.0.0.0', '::', '127.0.0.1', 'localhost'):
            print(
                f'blackbull: --bind host {host!r} is advisory in v1; '
                f'binding dual-stack on port {port} for now.',
                file=sys.stderr,
            )
    elif addr[0] == 'unix':
        _, unix_path = addr
    else:  # fd:// makes no sense for the zero-code static server
        print('blackbull: serve does not support fd:// binds; use host:port '
              'or unix:/path.', file=sys.stderr)
        return 1

    try:
        app = _build_static_app(
            args.directory, etag=args.etag, cache=args.cache, index=args.index)
    except (FileNotFoundError, NotADirectoryError) as exc:
        print(f'blackbull: {exc}', file=sys.stderr)
        return 1

    scheme = 'https' if args.certfile else 'http'
    where = unix_path if unix_path else f'{scheme}://127.0.0.1:{port}'
    print(f'blackbull: serving {os.path.abspath(args.directory)} on {where}',
          file=sys.stderr)

    try:
        _serve(
            app,
            certfile=args.certfile,
            keyfile=args.keyfile,
            port=port,
            unix_path=unix_path,
            workers=args.workers,
        )
    except RuntimeError as exc:
        print(f'blackbull: {exc}', file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    """Entry point invoked by the ``blackbull`` console script.

    Two forms are dispatched here:

    * ``blackbull serve [DIR] …`` — the zero-code static file server
      (see :func:`_serve_static`);
    * ``blackbull module:attr …`` — run an ASGI 3.0 application.

    Returns a process exit code.  argparse handles ``--help`` and
    invalid-flag exits itself (status 2); application-level errors
    (bad import path, bind syntax) print to stderr and exit 1.
    """
    if argv is None:
        argv = sys.argv[1:]
    if argv and argv[0] == 'serve':
        return _serve_static(argv[1:])

    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.config is not None:
        try:
            cfg = _load_config(args.config)
        except (FileNotFoundError, ValueError) as exc:
            print(f'blackbull: {exc}', file=sys.stderr)
            return 1
        # Set BB_* env vars as defaults (existing env vars take precedence).
        for var, val in cfg.items():
            if not var.startswith('_'):
                os.environ.setdefault(var, val)
        # TLS paths: apply only when CLI did not specify them explicitly.
        if args.certfile is None and '_certfile' in cfg:
            args.certfile = cfg['_certfile']
        if args.keyfile is None and '_keyfile' in cfg:
            args.keyfile = cfg['_keyfile']

    try:
        addr = _split_bind(args.bind)
    except ValueError as exc:
        print(f'blackbull: {exc}', file=sys.stderr)
        return 1

    port = 0
    unix_path: str | None = None
    inherited_fd: int | None = None
    if addr[0] == 'tcp':
        _, host, port = addr
        if host not in ('', '0.0.0.0', '::', '127.0.0.1', 'localhost'):
            # v1 of the CLI honours only the port — BlackBull's socket
            # layer binds dual-stack on all interfaces.  Warn so users
            # don't think their interface filter is in effect.
            print(
                f'blackbull: --bind host {host!r} is advisory in v1; '
                f'binding dual-stack on port {port} for now.',
                file=sys.stderr,
            )
    elif addr[0] == 'unix':
        _, unix_path = addr
    elif addr[0] == 'fd':
        _, inherited_fd = addr

    try:
        app = _import_app(args.app)
    except (ImportError, AttributeError, ValueError) as exc:
        print(f'blackbull: {exc}', file=sys.stderr)
        return 1

    try:
        _serve(
            app,
            certfile=args.certfile,
            keyfile=args.keyfile,
            port=port,
            unix_path=unix_path,
            inherited_fd=inherited_fd,
            workers=args.workers,
            max_connections=args.max_connections,
            stream_queue_depth=args.stream_queue_depth,
            ws_queue_depth=args.ws_queue_depth,
            reload=args.reload,
            reload_paths=args.reload_paths,
        )
    except RuntimeError as exc:
        # adopt_listening_fd / create_unix_socket raise RuntimeError on
        # well-defined failure modes (bad LISTEN_PID, fd out of window,
        # path collision).  These belong in stderr, not a traceback.
        print(f'blackbull: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':  # pragma: no cover
    raise SystemExit(main())
