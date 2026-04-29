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
import warnings
from collections.abc import Iterable
from http import HTTPStatus, HTTPMethod
from pathlib import Path
import asyncio
import sys
import traceback

# import from this package
import logging
from .event import Event, EventDispatcher
from .utils import Scheme
from .router import Router, ErrorRouter, MethodNotApplicable, PathNotRegistered
from .server.watch import Watcher, force_reload
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


async def _default_error_handler(scope, receive, send):  # noqa: ARG001
    """Comprehensive fallback error handler registered at BlackBull construction.

    Reads from scope['state']:
      - 'error_status'    : HTTPStatus  (default: INTERNAL_SERVER_ERROR)
      - 'error_exception' : exception instance (optional)
      - 'allowed_methods' : iterable of method names (for 405 Allow header)

    Returns a plain-text response with the status code, status phrase,
    and — when an exception is present — its class name and message.
    """
    state = scope.get('state', {})
    status = state.get('error_status', HTTPStatus.INTERNAL_SERVER_ERROR)
    exc = state.get('error_exception')
    allowed = state.get('allowed_methods', ())

    headers = []
    if allowed:
        headers.append((b'allow', ', '.join(m.upper() for m in allowed).encode()))

    lines = [f"{status} {status.phrase}"]
    if exc is not None:
        lines.append(f"{type(exc).__name__}: {exc}")
    body = '\n'.join(lines).encode()

    await send(body, status, headers)


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
              middlewares: list = []):
        return self._app.route(
            methods=methods,
            path=path,
            scheme=scheme,
            middlewares=self._group_mw + list(middlewares),
        )


class BlackBull:
    def __init__(self,
                 router=Router(),
                 loop=None,
                 ):
        self._router = router
        self._logger = logger
        self._error_router = ErrorRouter()

        # Register the comprehensive default handler for every HTTPStatus error
        # and the Exception base class so all unhandled errors are covered.
        for status in HTTPStatus:
            if status.is_client_error or status.is_server_error:
                self._error_router[status] = _default_error_handler
        self._error_router[Exception] = _default_error_handler

        self._dispatcher = EventDispatcher()
        self._loop = loop
        self._certfile = None
        self._keyfile = None
        self._wsprotocols = None
        self._global_middlewares: list = []
        self._static_roots: list[tuple[str, Path]] = []

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

    def on_startup(self, fn):
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

    def on_shutdown(self, fn):
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

    def on(self, event_name: str):
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

    def intercept(self, event_name: str):
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
                await self._dispatcher.emit(Event('app_startup'))
                await send({'type': 'lifespan.startup.complete'})
            elif event['type'] == 'lifespan.shutdown':
                self._logger.debug('lifespan shutdown')
                await self._dispatcher.emit(Event('app_shutdown'))
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
            method = HTTPMethod(scope['method'].upper())
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
            self._logger.debug("405 Method Not Allowed: path=%r method=%r allowed=%r",
                         path, method, e.allowed_methods)
            scope.setdefault('state', {}).update({
                'error_status': HTTPStatus.METHOD_NOT_ALLOWED,
                'allowed_methods': e.allowed_methods,
            })
            handler = self._error_router[HTTPStatus.METHOD_NOT_ALLOWED]
            if handler is not None:
                await handler(scope, receive, send)
            return
        except PathNotRegistered:
            self._logger.debug("404 Not Found: path=%r", path)
            scope.setdefault('state', {})['error_status'] = HTTPStatus.NOT_FOUND
            handler = self._error_router[HTTPStatus.NOT_FOUND]
            if handler is not None:
                await handler(scope, receive, send)
            return

        self._logger.debug((self, function))
        try:
            await function(scope, receive, send)
        except Exception as e:
            self._logger.error(traceback.format_exc())
            scope.setdefault('state', {}).update({
                'error_status': HTTPStatus.INTERNAL_SERVER_ERROR,
                'error_exception': e,
            })
            handler = self._error_router[e]
            if handler is not None:
                await handler(scope, receive, send)

    async def __call__(self, scope, receive, send):
        if scope.get('type') == 'lifespan':
            await self._handle_lifespan(scope, receive, send)
            return

        wrapped = _wrap_send(send)

        endpoint = self._dispatch
        for mw in reversed(self._global_middlewares):
            endpoint = functools.partial(mw, call_next=endpoint)

        await endpoint(scope, receive, wrapped)

    def route(self, methods: HTTPMethod | Iterable[HTTPMethod] = [HTTPMethod.GET],
              path: str = '/', scheme: Scheme | Iterable[Scheme] = Scheme.http,
              functions: list = [], middlewares: list = []):
        """Register a route handler, optionally wrapping it in middlewares."""
        return self._router.route(
            methods=methods,
            path=path,
            scheme=scheme,
            functions=functions,
            middlewares=middlewares,
            )

    def group(self, middlewares=[]) -> 'RouteGroup':
        """Return a RouteGroup that prepends *middlewares* to every route."""
        return RouteGroup(self, middlewares)

    def use(self, mw) -> None:
        """Register a global middleware applied to every non-lifespan request."""
        if not (inspect.isfunction(mw)
                or inspect.iscoroutinefunction(mw)
                or isinstance(mw, functools.partial)):
            from .middleware.base import StreamingAwareMiddleware
            if not isinstance(mw, StreamingAwareMiddleware):
                warnings.warn(
                    f"Class-based middleware {type(mw).__name__!r} does not inherit from "
                    f"StreamingAwareMiddleware.  If its __call__ wraps the 'send' callable, "
                    f"it will silently buffer streaming responses.  "
                    f"Inherit from blackbull.middleware.StreamingAwareMiddleware to opt in "
                    f"to automatic streaming-safe behaviour.",
                    UserWarning,
                    stacklevel=2,
                )
        self._global_middlewares.append(mw)

    def static(self, url_prefix: str, root_dir: str | Path) -> None:
        """Serve static files from *root_dir* under *url_prefix* via global middleware."""
        from blackbull.middleware.static import StaticFiles
        root = Path(root_dir).resolve()
        self._static_roots.append((url_prefix, root))
        self._global_middlewares.append(StaticFiles(url_prefix=url_prefix, root_dir=root))

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

    def has_server(self):
        return hasattr(self, 'server')

    def create_server(self, certfile=None, keyfile=None, port=0, ):
        from .server import ASGIServer
        self.server = ASGIServer(
            self,
            certfile=certfile,
            keyfile=keyfile,
            loop=self.loop)
        self.server.open_socket(port)
        self._logger.info(self.server)

    async def run(self, certfile=None, keyfile=None, port=0, debug=False):
        self._logger.info('Run is called.')
        tasks = []

        if not self.has_server():
            self.create_server(
                certfile=certfile,
                keyfile=keyfile,
                port=port,
                )

        if debug:
            watcher = Watcher(loop=self.loop)
            watcher.add_watch(sys.argv[0], self.reload)
            watcher.add_watch('blackbull', self.reload)
            tasks.append(watcher.watch())

        tasks.append(self.server.run(port=port))

        try:
            async with asyncio.TaskGroup() as tg:
                for coro in tasks:
                    tg.create_task(coro)

        except* asyncio.CancelledError:
            self._logger.info('Tasks cancelled.')

        except* BaseException as eg:
            self._logger.error('Task error: %s', eg)

    def wait_for_port(self, timeout: float = 10.0, poll_interval: float = 0.1):
        self.server.wait_for_port(timeout=timeout, poll_interval=poll_interval)

    def reload(self):
        self.stop()
        force_reload(sys.argv[0])

    def stop(self):
        self.server.close()

    @property
    def port(self):
        return self.server.port
