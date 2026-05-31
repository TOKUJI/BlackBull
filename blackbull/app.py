"""BlackBull application object — the user-facing ASGI 3.0 entry point.

Exposes the ``BlackBull`` class which wraps a ``Router``, an ``ErrorRouter``,
lifespan hooks, per-route and global middleware chains, and (via
``app.static``) static-file serving.  ``BlackBull.__call__`` is the ASGI
callable: it dispatches lifespan events to ``_handle_lifespan`` and routes
HTTP / WebSocket scopes through the global-middleware chain ending in
``_dispatch``.

Companion definitions live in this module to avoid circular imports:

- ``RouteGroup`` — returned by ``app.group(middlewares=[...])`` to share a
  middleware prefix across routes.
- ``_default_error_handler`` — registered on every ``HTTPStatus`` error and
  on ``Exception`` so unhandled errors produce a sensible plain-text reply.
- ``_wrap_send`` — adapts the ASGI ``send`` callable so handlers may pass
  ``Response`` objects directly.
"""
import functools
import inspect
from collections.abc import Awaitable, Callable, Iterable
from http import HTTPStatus, HTTPMethod
from pathlib import Path
import asyncio
import traceback

# import from this package
import logging
from .event import Event, EventDispatcher, EventHandler
from .utils import Scheme
from .router import Router, ErrorRouter, MethodNotApplicable, PathNotRegistered, ConfigurationError, has_middleware_param
logger = logging.getLogger(__name__)


def _wrap_send(raw_send):
    """Wrap an ASGI send callable to also accept Response objects.

    When a Response (or JSONResponse) is passed, unpacks it to
    (body, status, headers) for the underlying sender.  All other arguments
    (dicts, bytes) are forwarded unchanged.
    """
    from .response import Response as _Response

    async def _send(event, status=HTTPStatus.OK, headers=[]):
        if isinstance(event, _Response):
            await raw_send(event.body, event.status, event.headers)
        else:
            await raw_send(event, status, headers)

    return _send


def _wants_html(scope) -> bool:
    """True when the request's Accept header indicates an HTML preference."""
    for k, v in scope.get('headers', ()):
        if k.lower() == b'accept':
            val = v.lower()
            return b'text/html' in val or b'application/xhtml' in val
    return False


def _render_error_html(status, exc, tb_text: str | None, scope) -> bytes:
    """Build the DEV-mode HTML error page.  Traceback only when exc is set."""
    from html import escape
    method = escape(scope.get('method', '') or '')
    path = escape(scope.get('path', '') or '')
    title = f"{int(status)} {status.phrase}"
    tb_block = (
        f"<pre>{escape(tb_text)}</pre>" if tb_text else ''
    )
    exc_line = (
        f"<p><strong>{escape(type(exc).__name__)}</strong>: {escape(str(exc))}</p>"
        if exc is not None else ''
    )
    return (
        '<!doctype html><html><head><meta charset="utf-8">'
        f'<title>{escape(title)}</title>'
        '<style>'
        'body{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;'
        'margin:2em;color:#222;background:#fafafa}'
        'h1{color:#c00;border-bottom:1px solid #ccc;padding-bottom:.3em}'
        'pre{background:#fff;border:1px solid #ddd;padding:1em;overflow:auto;'
        'font-size:13px;line-height:1.4}'
        '.req{color:#666;font-size:13px}'
        '</style></head><body>'
        f'<h1>{escape(title)}</h1>'
        f'<p class="req">{method} {path}</p>'
        f'{exc_line}{tb_block}'
        '</body></html>'
    ).encode()


async def _default_error_handler(scope, receive, send):  # noqa: ARG001
    """Comprehensive fallback error handler registered at BlackBull construction.

    Reads from scope['state']:
      - 'error_status'    : HTTPStatus  (default: INTERNAL_SERVER_ERROR)
      - 'error_exception' : exception instance (optional)
      - 'allowed_methods' : iterable of method names (for 405 Allow header)

    Output adapts to ``BLACKBULL_ENV`` and the request ``Accept`` header:

    * ``development`` — include the full Python traceback when an exception
      is present, so users debugging locally see the failure inline.
      ``Accept: text/html`` returns a styled HTML page; everything else
      gets text/plain.
    * ``production`` — terse: status code + phrase only.  No exception
      class or message is leaked.  Browsers get a minimal HTML page;
      curl-style clients get text/plain.
    """
    # Imported here to avoid a circular import at module load (env -> app).
    from .env import get_settings, Environment

    state = scope.get('state', {})
    status = state.get('error_status', HTTPStatus.INTERNAL_SERVER_ERROR)
    exc = state.get('error_exception')
    allowed = state.get('allowed_methods', ())

    is_dev = get_settings().env == Environment.DEVELOPMENT
    html_ok = _wants_html(scope)

    headers = []
    if allowed:
        headers.append((b'allow', ', '.join(m.upper() for m in allowed).encode()))

    tb_text = None
    if is_dev and exc is not None:
        tb_text = ''.join(
            traceback.format_exception(type(exc), exc, exc.__traceback__))

    if html_ok:
        body = _render_error_html(status, exc if is_dev else None,
                                  tb_text, scope)
        headers.append((b'content-type', b'text/html; charset=utf-8'))
    else:
        lines = [f"{status} {status.phrase}"]
        if is_dev and exc is not None:
            lines.append(f"{type(exc).__name__}: {exc}")
            if tb_text:
                lines.append('')
                lines.append(tb_text.rstrip())
        body = '\n'.join(lines).encode()
        headers.append((b'content-type', b'text/plain; charset=utf-8'))

    headers.append((b'content-length', str(len(body)).encode()))
    await send({'type': 'http.response.start', 'status': status, 'headers': headers})
    await send({'type': 'http.response.body', 'body': body, 'more_body': False})


class RouteGroup:
    """A subset of routes that share a common middleware prefix.

    Obtain via ``app.group(middlewares=[...])``.  Every route registered
    through the group automatically prepends the group middlewares before
    any per-route middlewares.
    """
    def __init__(self, app: 'BlackBull', middlewares):
        self._app = app
        self._group_mw = list(middlewares)

    def route(self, methods: HTTPMethod | Iterable[HTTPMethod] = [HTTPMethod.GET],
              path: str = '/', scheme: Scheme | Iterable[Scheme] = Scheme.http,
              middlewares: list = [], name: str | None = None):
        return self._app.route(
            methods=methods,
            path=path,
            scheme=scheme,
            middlewares=self._group_mw + list(middlewares),
            name=name,
        )


class BlackBull:
    def __init__(self,
                 loop=None,
                 observer_shutdown_timeout: float = 5.0,
                 trusted_proxies: list[str] | str | None = None,
                 ):
        self._router = Router()
        self._logger = logger
        self._error_router = ErrorRouter()

        # Register the comprehensive default handler for every HTTPStatus error
        # and the Exception base class so all unhandled errors are covered.
        for status in HTTPStatus:
            if status.is_client_error or status.is_server_error:
                self._error_router[status] = _default_error_handler
        self._error_router[Exception] = _default_error_handler

        self._dispatcher = EventDispatcher(shutdown_timeout=observer_shutdown_timeout)
        self._loop = loop
        self._certfile = None
        self._keyfile = None
        self._wsprotocols = None
        self._global_middlewares: list = []
        self._static_roots: list[tuple[str, Path]] = []
        self._chain = None  # cached global middleware chain; rebuilt on first request

        if trusted_proxies is not None:
            from .middleware.proxy import TrustedProxy  # noqa: PLC0415
            self.use(TrustedProxy(trusted_proxies))

    @property
    def loop(self):
        if self._loop is None:
            try:
                self._loop = asyncio.get_running_loop()
            except RuntimeError:
                # No event loop is running yet (e.g. called from synchronous
                # setup code).  Return None so callers that don't need the
                # loop won't crash; asyncio will provide the loop later when
                # the coroutines actually run.
                return None
        return self._loop

    @property
    def certfile(self):
        return self._certfile

    @property
    def keyfile(self):
        return self._keyfile
    
    @property
    def available_ws_protocols(self) -> list[bytes]:
        return self._wsprotocols or []

    @available_ws_protocols.setter
    def available_ws_protocols(self, value: list) -> None:
        self._wsprotocols = [
            v.encode() if isinstance(v, str) else v for v in value
        ]

    def on_startup(self, fn: Callable[[], Awaitable[None]]) -> Callable[[], Awaitable[None]]:
        """Register a zero-argument coroutine to run at lifespan startup.

        The handler is wrapped in an adapter and registered as an
        ``'app_startup'`` interception handler so it runs before the ASGI
        server receives the ``lifespan.startup.complete`` acknowledgement.
        Startup handlers run in registration order; an exception aborts the
        remaining handlers and prevents the completion event from being sent.

        Args:
            fn: Async callable that takes no arguments.

        Returns:
            fn unchanged, so the decorator can be stacked or the function
            used normally after registration.

        Example:
            ```python
            @app.on_startup
            async def open_db():
                await db.connect()
            ```
        """
        async def _adapter(_event: Event) -> None:
            await fn()
        self._dispatcher.intercept('app_startup', _adapter)
        return fn

    def on_shutdown(self, fn: Callable[[], Awaitable[None]]) -> Callable[[], Awaitable[None]]:
        """Register a zero-argument coroutine to run at lifespan shutdown.

        The handler is wrapped in an adapter and registered as an
        ``'app_shutdown'`` interception handler so it runs before the ASGI
        server receives the ``lifespan.shutdown.complete`` acknowledgement.
        Shutdown handlers run in registration order; an exception aborts the
        remaining handlers.

        Args:
            fn: Async callable that takes no arguments.

        Returns:
            fn unchanged, so the decorator can be stacked or the function
            used normally after registration.

        Example:
            ```python
            @app.on_shutdown
            async def close_db():
                await db.disconnect()
            ```
        """
        async def _adapter(_event: Event) -> None:
            await fn()
        self._dispatcher.intercept('app_shutdown', _adapter)
        return fn

    def on(self, event_name: str) -> Callable[[EventHandler], EventHandler]:
        """Decorate a handler to observe ``event_name`` (fire-and-forget).

        The handler is scheduled as an independent ``asyncio.Task`` each time
        the event fires.  Exceptions raised by the handler are caught and
        logged; they do not propagate to the emitter or affect other handlers.

        Args:
            event_name: Name of the event to observe (e.g. ``'app_startup'``).

        Returns:
            A decorator that registers the wrapped coroutine and returns it
            unchanged.

        Example:
            ```python
            @app.on('app_startup')
            async def handler(event: Event):
                print(event.detail)
            ```
        """
        def decorator(handler):
            self._dispatcher.on(event_name, handler)
            return handler
        return decorator

    def intercept(self, event_name: str) -> Callable[[EventHandler], EventHandler]:
        """Decorate a handler to intercept ``event_name`` synchronously.

        The handler is awaited in registration order when the event fires.
        Exceptions propagate to the emitter and abort subsequent interceptors
        registered for the same event.

        Args:
            event_name: Name of the event to intercept (e.g. ``'app_startup'``).

        Returns:
            A decorator that registers the wrapped coroutine and returns it
            unchanged.

        Example:
            ```python
            @app.intercept('app_startup')
            async def handler(event: Event):
                await setup()
            ```
        """
        def decorator(handler):
            self._dispatcher.intercept(event_name, handler)
            return handler
        return decorator

    async def _handle_lifespan(self, scope, receive, send):  # noqa: ARG001
        while True:
            event = await receive()
            if event['type'] == 'lifespan.startup':
                self._logger.debug('lifespan startup')
                mw_errors = [
                    f"Global middleware {mw!r} has no 'call_next' parameter"
                    for mw in self._global_middlewares
                    if not has_middleware_param(mw)
                ]
                if mw_errors:
                    exc = ConfigurationError('\n'.join(mw_errors))
                    self._logger.error('Middleware configuration error:\n%s', exc)
                    await send({'type': 'lifespan.startup.failed', 'message': str(exc)})
                    return
                try:
                    self._router.validate()
                except ConfigurationError as exc:
                    self._logger.error('Route configuration error:\n%s', exc)
                    await send({'type': 'lifespan.startup.failed', 'message': str(exc)})
                    return
                await self._dispatcher.emit(Event('app_startup'))
                await send({'type': 'lifespan.startup.complete'})
            elif event['type'] == 'lifespan.shutdown':
                self._logger.debug('lifespan shutdown')
                await self._dispatcher.emit(Event('app_shutdown'))
                await self._dispatcher.aclose()
                await send({'type': 'lifespan.shutdown.complete'})
                return

    async def _dispatch(self, scope, receive, send):
        """Route and dispatch a single non-lifespan request."""
        self._logger.debug((scope, receive, send))

        try:
            scheme = Scheme(scope['type'])
        except ValueError:
            self._logger.error(f'Invalid scheme ({scope["type"]}) is requested.')
            raise Exception('Invalid scheme is requested.')

        if scheme == Scheme.websocket:
            path = scope['path']
            try:
                function = self._router[(path, HTTPMethod.GET, scheme)]
            except (MethodNotApplicable, PathNotRegistered):
                self._logger.warning('No websocket handler registered for %s', path)
                return
            await function(scope, receive, send)
            return

        try:
            # RFC 9110 §9.1 — methods are case-sensitive.  Looking up
            # ``scope['method'].upper()`` would silently fold a lowercase
            # ``get`` to ``GET`` and dispatch to the wrong handler.  Use
            # the value verbatim; HTTPMethod() rejects non-uppercase forms.
            method = HTTPMethod(scope['method'])
        except ValueError:
            self._logger.debug("Unknown HTTP method: %r", scope['method'])
            scope.setdefault('state', {})['error_status'] = HTTPStatus.METHOD_NOT_ALLOWED
            handler = self._error_router[HTTPStatus.METHOD_NOT_ALLOWED]
            if handler is not None:
                await handler(scope, receive, send)
            return

        path = scope['path']
        self._logger.debug((path, scheme))

        try:
            function = self._router[(path, method, scheme)]
        except MethodNotApplicable as e:
            self._logger.debug("%s: path=%r method=%r allowed=%r",
                         HTTPStatus.METHOD_NOT_ALLOWED.phrase, path, method, e.allowed_methods)
            scope.setdefault('state', {}).update({
                'error_status': HTTPStatus.METHOD_NOT_ALLOWED,
                'allowed_methods': e.allowed_methods,
            })
            handler = self._error_router[HTTPStatus.METHOD_NOT_ALLOWED]
            if handler is not None:
                await handler(scope, receive, send)
            return
        except PathNotRegistered:
            self._logger.debug("%s: path=%r", HTTPStatus.NOT_FOUND.phrase, path)
            scope.setdefault('state', {})['error_status'] = HTTPStatus.NOT_FOUND
            handler = self._error_router[HTTPStatus.NOT_FOUND]
            if handler is not None:
                await handler(scope, receive, send)
            return

        self._logger.debug((self, function))
        exc_caught: Exception | None = None
        try:
            await self._dispatcher.emit(Event('before_handler', detail={
                'scope':     scope,
                'client_ip': scope['client'][0] if scope.get('client') else '',
                'method':    scope.get('method', ''),
                'path':      scope.get('path', ''),
                'handler':   function.__name__,
            }))
            await function(scope, receive, send)
        except Exception as e:
            exc_caught = e
            self._logger.error(traceback.format_exc())
        finally:
            await self._dispatcher.emit(Event('after_handler', detail={
                'scope':     scope,
                'client_ip': scope['client'][0] if scope.get('client') else '',
                'method':    scope.get('method', ''),
                'path':      scope.get('path', ''),
                'handler':   function.__name__,
                'exception': exc_caught,
            }))

        if exc_caught is not None:
            scope.setdefault('state', {}).update({
                'error_status': HTTPStatus.INTERNAL_SERVER_ERROR,
                'error_exception': exc_caught,
            })
            handler = self._error_router[exc_caught]
            if handler is not None:
                await handler(scope, receive, send)

    def _build_chain(self):
        chain = self._dispatch
        for mw in reversed(self._global_middlewares):
            chain = functools.partial(mw, call_next=chain)
        self._chain = chain

    async def __call__(self, scope, receive, send):
        if scope.get('type') == 'lifespan':
            await self._handle_lifespan(scope, receive, send)
            return

        if self._chain is None:
            self._build_chain()

        await self._chain(scope, receive, _wrap_send(send))

    def route(self, methods: HTTPMethod | Iterable[HTTPMethod] = [HTTPMethod.GET],
              path: str = '/', scheme: Scheme | Iterable[Scheme] = Scheme.http,
              functions: list = [], middlewares: list = [],
              name: str | None = None):
        """Register a route handler, optionally wrapping it in middlewares."""
        return self._router.route(
            methods=methods,
            path=path,
            scheme=scheme,
            functions=functions,
            middlewares=middlewares,
            name=name,
            )

    def group(self, middlewares=[]) -> 'RouteGroup':
        """Return a RouteGroup that prepends *middlewares* to every route."""
        return RouteGroup(self, middlewares)

    def use(self, mw) -> None:
        """Register a global middleware applied to every non-lifespan request."""
        self._global_middlewares.append(mw)
        self._chain = None  # invalidate cached chain

    def static(self, url_prefix: str, root_dir: str | Path) -> None:
        """Serve static files from *root_dir* under *url_prefix* via global middleware."""
        from blackbull.middleware.static import StaticFiles
        root = Path(root_dir).resolve()
        self._static_roots.append((url_prefix, root))
        self._global_middlewares.append(StaticFiles(url_prefix=url_prefix, root_dir=root))
        self._chain = None  # invalidate cached chain

    def on_error(self, key):
        """Register a custom error handler for an HTTPStatus or exception class.

        Usage::

            @app.on_error(HTTPStatus.FORBIDDEN)
            async def handle_403(scope, receive, send):
                ...

            @app.on_error(ValueError)
            async def handle_value_error(scope, receive, send):
                ...

        The handler receives (scope, receive, send).
        scope['state'] contains:
          - 'error_status'    : HTTPStatus
          - 'error_exception' : exception instance (when triggered by an exception)
          - 'allowed_methods' : allowed method names (for 405)
        """
        return self._error_router(key)

    def url_path_for(self, name: str, /, **params) -> str:
        """Return the path for the named route with *params* substituted."""
        return self._router.url_path_for(name, **params)

    def enable_openapi(self, *,
                       title: str = 'BlackBull API',
                       version: str = '0.1.0',
                       description: str | None = None,
                       spec_path: str = '/openapi.json',
                       docs_path: str | None = '/docs') -> None:
        """Auto-publish an OpenAPI 3.1 spec and Swagger UI for the app.

        Registers two routes:

        * ``spec_path`` (default ``/openapi.json``) returns the spec as JSON.
          The spec is regenerated on every request so new routes added after
          this call are reflected.
        * ``docs_path`` (default ``/docs``) returns an HTML page hosting
          Swagger UI pointed at ``spec_path``.  Pass ``docs_path=None`` to
          skip the UI route and serve only the JSON spec.

        Call once, after the rest of the app's routes have been registered.
        """
        from .openapi import generate_spec, swagger_ui_html  # noqa: PLC0415
        from .response import Response, JSONResponse  # noqa: PLC0415

        async def _openapi_spec(scope, receive, send):  # noqa: ARG001
            spec = generate_spec(self, title=title, version=version,
                                 description=description)
            await send(JSONResponse(spec))
        # Mark before registration so the original handler stored in
        # ``Router._route_info`` carries the flag (functools.wraps does not
        # copy arbitrary attributes onto the wrapper, so setting it post-hoc
        # on the decorated form would not propagate back to the original).
        _openapi_spec.__blackbull_openapi_internal__ = True
        self.route(methods=HTTPMethod.GET, path=spec_path)(_openapi_spec)

        if docs_path is not None:
            ui_title = f'{title} — Swagger UI'

            async def _swagger_ui(scope, receive, send):  # noqa: ARG001
                html = swagger_ui_html(spec_path, title=ui_title)
                await send(Response(html))
            _swagger_ui.__blackbull_openapi_internal__ = True
            self.route(methods=HTTPMethod.GET, path=docs_path)(_swagger_ui)

    def run(self, certfile=None, keyfile=None, port=0,
            unix_path: str | None = None,
            inherited_fd: int | None = None,
            workers: int | None = None,
            max_connections: int | None = None,
            stream_queue_depth: int | None = None,
            ws_queue_depth: int | None = None,
            reload: bool = False,
            reload_paths: list | None = None) -> None:
        """Run the app under BlackBull's own server (single- or multi-worker).

        This is the synchronous, fire-and-forget entry point — callers
        write ``app.run(port=8000)``, not ``asyncio.run(app.run(...))``.
        For ``workers > 1`` *or* ``reload=True`` the master pre-binds
        sockets, forks workers, and blocks until SIGTERM / SIGINT.

        For embedded use under an existing event loop, or for pre-binding
        a socket before forking a test subprocess, instantiate
        :class:`blackbull.server.ASGIServer` directly.  Any external
        ASGI server (uvicorn / hypercorn / granian / …) can drive the
        :class:`BlackBull` instance via its ASGI 3.0 ``__call__``.

        Example::

            app.run(port=8000)
            app.run(port=8443, certfile='cert.pem', keyfile='key.pem', workers=4)
            app.run(port=8443, certfile='cert.pem', keyfile='key.pem', reload=True)
            app.run(unix_path='/run/blackbull.sock')
        """
        serve(
            self,
            certfile=certfile, keyfile=keyfile, port=port,
            unix_path=unix_path,
            inherited_fd=inherited_fd,
            workers=workers,
            max_connections=max_connections,
            stream_queue_depth=stream_queue_depth,
            ws_queue_depth=ws_queue_depth,
            reload=reload,
            reload_paths=reload_paths,
        )


def serve(app, *,
          certfile=None, keyfile=None, port=0,
          unix_path: str | None = None,
          inherited_fd: int | None = None,
          workers: int | None = None,
          max_connections: int | None = None,
          stream_queue_depth: int | None = None,
          ws_queue_depth: int | None = None,
          reload: bool = False,
          reload_paths: list | None = None) -> None:
    """Synchronous entry point for any ASGI 3.0 callable.

    Works for a :class:`BlackBull` instance *and* for any plain ASGI
    callable (uvicorn/hypercorn-style ``async def app(scope, receive,
    send): …``).  This is what the ``blackbull`` console script calls
    after resolving ``module:attr``; :meth:`BlackBull.serve` is a thin
    shim around it.

    For ``workers=1`` without reload the server runs in the current
    process via ``asyncio.run``.  For ``workers > 1`` *or*
    ``reload=True`` the master pre-binds sockets, forks workers, and
    blocks until SIGTERM / SIGINT — or in reload mode, until a
    watched file changes (master then re-execs itself, see
    :mod:`blackbull.server.reload`).

    All integer parameters default to their corresponding ``BB_*``
    environment variables (see :mod:`blackbull.env`).
    """
    from .env import get_settings as _get_settings  # noqa: PLC0415
    import os as _os  # noqa: PLC0415
    _cfg = _get_settings()
    workers = workers if workers is not None else _cfg.workers
    workers = workers or (_os.cpu_count() or 1)
    max_connections = max_connections if max_connections is not None else _cfg.max_connections
    stream_queue_depth = (stream_queue_depth if stream_queue_depth is not None
                          else _cfg.stream_queue_depth)
    ws_queue_depth = ws_queue_depth if ws_queue_depth is not None else _cfg.ws_queue_depth

    # BlackBull instances expose a router whose configuration is worth
    # validating up-front (catches routes that reference unbound names
    # before workers fork).  Plain ASGI callables skip this — their
    # validity is the app author's problem.
    if isinstance(app, BlackBull):
        app._router.validate()

    # Reload requires the master+worker structure so a long-lived
    # supervisor can hold the listening sockets across worker recycles.
    # Single-worker reload is supported by promoting to workers=1
    # under the multi-worker path.
    if workers == 1 and not reload:
        import logging as _logging  # noqa: PLC0415
        from .logger import setup_async_logging, teardown_async_logging  # noqa: PLC0415
        from .env import apply_event_loop_policy  # noqa: PLC0415
        from .server import ASGIServer  # noqa: PLC0415
        apply_event_loop_policy(_cfg)
        if _cfg.async_logging:
            setup_async_logging()
        if not _cfg.access_log:
            _logging.getLogger('blackbull.access').setLevel(_logging.WARNING)
        try:
            asyncio.run(_run_single(
                app,
                certfile=certfile, keyfile=keyfile, port=port,
                unix_path=unix_path,
                inherited_fd=inherited_fd,
                max_connections=max_connections,
                stream_queue_depth=stream_queue_depth,
                ws_queue_depth=ws_queue_depth,
            ))
        finally:
            teardown_async_logging()
        return

    from .server import ASGIServer  # noqa: PLC0415
    from .server.multiworker import MultiWorkerServer  # noqa: PLC0415

    # Bind sockets in the master process; workers inherit them via fork.
    # When a reload re-execed us, ASGIServer.open_socket adopts the
    # inherited fds instead of binding.
    master_server = ASGIServer(app, certfile=certfile, keyfile=keyfile,
                               max_connections=max_connections,
                               stream_queue_depth=stream_queue_depth,
                               ws_queue_depth=ws_queue_depth)
    master_server.open_socket(port, unix_path=unix_path, inherited_fd=inherited_fd)
    addr = (f'unix:{master_server.unix_path}'
            if master_server.unix_path else
            f'port {master_server.port}')
    logger.info(
        'Starting %d worker(s) on %s%s', workers, addr,
        ' [auto-reload]' if reload else '',
    )

    MultiWorkerServer(
        app,
        master_server.raw_sockets,
        master_server.ssl_context,
        workers=workers,
        max_connections=max_connections,
        stream_queue_depth=stream_queue_depth,
        ws_queue_depth=ws_queue_depth,
        reload=reload,
        reload_paths=reload_paths,
    ).run()

    master_server.close_socket()


async def _run_single(app, *, certfile, keyfile, port, unix_path, inherited_fd,
                      max_connections, stream_queue_depth, ws_queue_depth):
    """Single-worker server loop — invoked from :func:`serve`."""
    from .server import ASGIServer  # noqa: PLC0415
    server = ASGIServer(app, certfile=certfile, keyfile=keyfile,
                        max_connections=max_connections,
                        stream_queue_depth=stream_queue_depth,
                        ws_queue_depth=ws_queue_depth)
    server.open_socket(port, unix_path=unix_path, inherited_fd=inherited_fd)
    await server.run(port=port)
