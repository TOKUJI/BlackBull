# from functools import partial, reduce
from http import HTTPStatus, HTTPMethod
import asyncio
import sys
import traceback

# import from this package
from .utils import Scheme
from .router import Router, ErrorRouter, MethodNotApplicable, PathNotRegistered
from .logger import get_logger_set
from .server.watch import Watcher, force_reload
logger, log = get_logger_set(__name__)


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

    def route(self, methods=[HTTPMethod.GET], path='/', scheme=Scheme.http,
              middlewares=[]):
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
                 logger=logger,
                 log=log,
                 ):
        self._router = router
        self._logger = logger
        self._log = log
        self._error_router = ErrorRouter()

        # Register the comprehensive default handler for every HTTPStatus error
        # and the Exception base class so all unhandled errors are covered.
        for status in HTTPStatus:
            if status.is_client_error or status.is_server_error:
                self._error_router[status] = _default_error_handler
        self._error_router[Exception] = _default_error_handler

        self._loop = loop
        self._certfile = None
        self._keyfile = None
        self._startup_hooks: list = []
        self._shutdown_hooks: list = []

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

    def on_startup(self, fn):
        """Decorator: register an async callable invoked at lifespan startup."""
        self._startup_hooks.append(fn)
        return fn

    def on_shutdown(self, fn):
        """Decorator: register an async callable invoked at lifespan shutdown."""
        self._shutdown_hooks.append(fn)
        return fn

    async def _handle_lifespan(self, scope, receive, send):  # noqa: ARG001
        while True:
            event = await receive()
            if event['type'] == 'lifespan.startup':
                self._logger.debug('lifespan startup')
                for hook in self._startup_hooks:
                    await hook()
                await send({'type': 'lifespan.startup.complete'})
            elif event['type'] == 'lifespan.shutdown':
                self._logger.debug('lifespan shutdown')
                for hook in self._shutdown_hooks:
                    await hook()
                await send({'type': 'lifespan.shutdown.complete'})
                return

    async def __call__(self, scope, receive, send):
        self._logger.debug((scope, receive, send))

        if scope.get('type') == 'lifespan':
            await self._handle_lifespan(scope, receive, send)
            return

        try:
            scheme = Scheme(scope['type'])
        except ValueError:
            self._logger.error(f'Invalid scheme ({scope["type"]}) is requested.')
            raise Exception('Invalid scheme is requested.')

        wrapped = _wrap_send(send)

        if scheme == Scheme.websocket:
            path = scope['path']
            try:
                function = self._router[(path, HTTPMethod.GET, scheme)]
            except (MethodNotApplicable, PathNotRegistered):
                self._logger.warning('No websocket handler registered for %s', path)
                return
            await function(scope, receive, wrapped)
            return

        try:
            method = HTTPMethod(scope['method'].upper())
        except ValueError:
            self._logger.debug("Unknown HTTP method: %r", scope['method'])
            scope.setdefault('state', {})['error_status'] = HTTPStatus.METHOD_NOT_ALLOWED
            await self._error_router[HTTPStatus.METHOD_NOT_ALLOWED](scope, receive, wrapped)
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
            await self._error_router[HTTPStatus.METHOD_NOT_ALLOWED](scope, receive, wrapped)
            return
        except PathNotRegistered:
            self._logger.debug("404 Not Found: path=%r", path)
            scope.setdefault('state', {})['error_status'] = HTTPStatus.NOT_FOUND
            await self._error_router[HTTPStatus.NOT_FOUND](scope, receive, wrapped)
            return

        self._logger.debug((self, function))
        try:
            await function(scope, receive, wrapped)
        except Exception as e:
            self._logger.error(traceback.format_exc())
            scope.setdefault('state', {}).update({
                'error_status': HTTPStatus.INTERNAL_SERVER_ERROR,
                'error_exception': e,
            })
            handler = self._error_router[e]
            if handler is not None:
                await handler(scope, receive, wrapped)

    def route(self, methods=[HTTPMethod.GET], path='/', scheme=Scheme.http,
              functions=[], middlewares=[]):
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
            await asyncio.gather(*tasks, return_exceptions=True)

        except asyncio.exceptions.CancelledError:
            self._logger.info('The tasks have been cancelled.')

        except BaseException:
            self._logger.error(traceback.format_exc())

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
