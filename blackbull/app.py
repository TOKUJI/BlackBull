"""BlackBull application object — the user-facing ASGI 3.0 entry point.

``RouteGroup``, the default error handler and the handler-boundary send
adapter live here rather than in the modules they belong to: importing them
from there would close an import cycle back to this one.
"""
import functools
from collections.abc import Awaitable, Callable, Iterable
from http import HTTPStatus, HTTPMethod
from pathlib import Path
import asyncio
import re
import traceback

import logging
from .event import Event, EventDispatcher, EventHandler
from .utils import Scheme, is_client_error, is_server_error
from .router import Router, RouteInfo, ErrorRouter, MethodNotApplicable, PathNotRegistered, ConfigurationError, HTTPException, has_middleware_param
from .request import ClientDisconnected
from .connection import Connection, disconnected, CONNECTION_STASH_KEY
from .native import NativeResponse
from .response import wrap_native_send
from .asgi import ASGIReceiveCallable, ASGISendCallable
from .config import AppConfig
from .logger import debug_gate  # noqa: E402
from .server.protocol_registry import RawBinding

logger = logging.getLogger(__name__)
_DEBUG = debug_gate(logger)

#: Value-to-member lookups without ``Enum.__call__``'s two Python frames; see
#: ``_PSEUDO_BY_BYTES`` in ``blackbull/protocol/frame_types.py``.
_SCHEME_BY_VALUE: dict[str, Scheme] = {m.value: m for m in Scheme}
_METHOD_BY_VALUE: dict[str, HTTPMethod] = {m.value: m for m in HTTPMethod}



def _wrap_send_native(raw_send: ASGISendCallable):
    """Install [`blackbull.response.wrap_native_send`][blackbull.response.wrap_native_send] at the handler
    boundary, which is the innermost wrap.
    """
    return wrap_native_send(raw_send)


def _to_asgi_boundary(send: ASGISendCallable):
    """Wrap an external ASGI host's ``send`` with native→ASGI conversion.

    [`BlackBull.__call__`][BlackBull.__call__] decides when to install it.
    """
    from .native import asgi_send_boundary  # noqa: PLC0415

    return asgi_send_boundary(send)


def _inject_response_headers(raw_send, extra_headers):
    """Wrap *raw_send* to append *extra_headers* to the response.

    A route may declare headers (via the ``_bb_response_headers`` hook) that
    must appear on all of its responses — success and the centrally-rendered
    error alike.  Every event but the header arm passes straight through.
    """
    # ``event`` is a NativeResponse.  Nested defs in a per-request factory
    # stay unannotated — tests/architecture/test_per_request_closure_annotations.py.
    async def _send(event):
        if (isinstance(event, NativeResponse)
                and (event._extension is None or event.push is None)):
            view = event.header
            if view is not None:
                view.append(extra_headers)
        await raw_send(event)

    return _send


def _wants_html(conn) -> bool:
    """True when the request's Accept header indicates an HTML preference."""
    accept = conn.headers.get(b'accept', b'').lower()
    return b'text/html' in accept or b'application/xhtml' in accept


def _render_error_html(status, exc, tb_text: str | None, conn) -> bytes:
    """Build the DEV-mode HTML error page.  Traceback only when exc is set."""
    from html import escape
    method = escape(conn.method or '')
    path = escape(conn.path or '')
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


async def _default_error_handler(conn, receive, send):  # noqa: ARG001
    """Fallback error handler registered on every status at construction.

    Reads ``error_status`` / ``error_exception`` / ``allowed_methods`` off
    ``conn.state`` and renders per ``BLACKBULL_ENV`` and ``Accept``.  The
    dev/prod matrix, and why a 4xx [`HTTPException`][] keeps its detail
    line but not its traceback, are in ``docs/guide/error-handling.md``.
    """
    # Imported here to avoid a circular import at module load (env -> app).
    from .env import get_settings, Environment

    state = conn.state
    status = state.get('error_status', HTTPStatus.INTERNAL_SERVER_ERROR)
    exc = state.get('error_exception')
    allowed = state.get('allowed_methods', ())

    is_dev = get_settings().env == Environment.DEVELOPMENT
    html_ok = _wants_html(conn)

    headers = []
    if allowed:
        headers.append((b'allow', ', '.join(m.upper() for m in allowed).encode()))

    tb_text = None
    if is_dev and exc is not None:
        if not (isinstance(exc, HTTPException) and is_client_error(exc.status)):
            tb_text = ''.join(
                traceback.format_exception(type(exc), exc, exc.__traceback__))

    if html_ok:
        body = _render_error_html(status, exc if is_dev else None,
                                  tb_text, conn)
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
    from .response import _emit_response
    await _emit_response(send, body, status, headers)


class RouteGroup:
    """A subset of routes that share a common middleware prefix.

    Obtain via ``app.group(middlewares=[...])``.  Every route registered
    through the group automatically prepends the group middlewares before
    any per-route middlewares.
    """
    def __init__(self, app: 'BlackBull', middlewares):
        self._app = app
        self._group_mw = list(middlewares)

    def route(self, methods: str | HTTPMethod | Iterable[str | HTTPMethod] = [HTTPMethod.GET],
              path: str | re.Pattern = '/', scheme: Scheme | Iterable[Scheme] = Scheme.http,
              middlewares: list = [], name: str | None = None,
              accept_query: Iterable[str] | None = None):
        """Register a route on this group, prepending the group middlewares.

        Same parameters as [`BlackBull.route`][BlackBull.route].  ``accept_query`` is the
        RFC 10008 list of request **media types** the route accepts (the
        ``Accept-Query`` response header + Content-Type enforcement) — it is a
        content-negotiation policy, **not** a switch for the QUERY *method*
        (a method is accepted purely by listing it in ``methods``).
        """
        return self._app.route(
            methods=methods,
            path=path,
            scheme=scheme,
            middlewares=self._group_mw + list(middlewares),
            name=name,
            accept_query=accept_query,
        )


class BlackBull:
    """The application: a router, its error handlers, and the ASGI callable.

    Register routes with [`route`][blackbull.app.BlackBull.route], or with [`group`][blackbull.app.BlackBull.group] when several
    should share a middleware prefix; global middleware with [`use`][blackbull.app.BlackBull.use];
    lifecycle hooks with [`on_startup`][blackbull.app.BlackBull.on_startup], [`on_shutdown`][blackbull.app.BlackBull.on_shutdown] and
    [`on_warmup`][blackbull.app.BlackBull.on_warmup].  [`run`][blackbull.app.BlackBull.run] serves the app on BlackBull's own server,
    and the instance is itself a plain ASGI 3.0 callable, so an external host
    can drive it instead.

    Protocols other than HTTP and WebSocket attach through
    [`add_extension`][blackbull.app.BlackBull.add_extension] or [`register_protocol_handler`][blackbull.app.BlackBull.register_protocol_handler]; nothing here
    is added by subclassing.

    Attributes:
        extensions: Installed extensions by name.  The Extensions guide says
            what an extension puts there and how to reach it.
    """

    def __init__(self,
                 loop: asyncio.AbstractEventLoop | None = None,
                 observer_shutdown_timeout: float = 5.0,
                 trusted_proxies: list[str] | str | None = None,
                 config: AppConfig | None = None,
                 cache_max: int | None = None,
                 asgi: bool = False,
                 ):
        """Build an application.

        Args:
            loop: Event loop to bind to.  Left unset,
                [`loop`][blackbull.app.BlackBull.loop] picks up the running
                loop once there is one.
            observer_shutdown_timeout: Seconds to wait at shutdown for
                detached ``@app.on`` observers to finish.
            trusted_proxies: Addresses or CIDRs whose forwarded headers are
                honoured; installs
                [`TrustedProxy`][blackbull.middleware.proxy.TrustedProxy].
            config: Deploy settings [`run`][blackbull.app.BlackBull.run]
                falls back to.  The Configuration guide gives the resolution
                order.
            cache_max: Route-resolution cache size.  ``None`` keeps the
                router's own default.
            asgi: Mark the app as driven by an external ASGI host, so
                [`__call__`][blackbull.app.BlackBull.__call__] converts at the
                boundary in both directions.
        """
        self._asgi = asgi
        self._config = config
        self._router = Router() if cache_max is None else Router(cache_max=cache_max)
        self._logger = logger
        self._error_router = ErrorRouter(default=_default_error_handler)

        self._dispatcher = EventDispatcher(shutdown_timeout=observer_shutdown_timeout)
        self._loop = loop
        self._wsprotocols = None
        self._global_middlewares: list = []
        self._static_roots: list[tuple[str, Path]] = []
        self._chain = None

        self._warmup_hooks: list = []
        self.extensions: dict[str, object] = {}

        # Built on the first registration, so an HTTP-only app allocates no
        # registry and binds no extra listener.
        self._protocol_registry = None

        self._grpc_registry = None

        if trusted_proxies is not None:
            from .middleware.proxy import TrustedProxy  # noqa: PLC0415
            self.use(TrustedProxy(trusted_proxies))

    @property
    def loop(self):
        """The bound event loop, or ``None`` when none is running yet.

        Read from synchronous setup code this answers ``None`` rather than
        raising, so a caller that does not need a loop still works; asyncio
        supplies one once the coroutines run.
        """
        if self._loop is None:
            try:
                self._loop = asyncio.get_running_loop()
            except RuntimeError:
                return None
        return self._loop

    @property
    def available_ws_protocols(self) -> list[bytes]:
        """WebSocket subprotocols this app offers, as bytes.

        Assigning a list of ``str`` encodes each entry.
        """
        return self._wsprotocols or []

    @available_ws_protocols.setter
    def available_ws_protocols(self, value: list) -> None:
        self._wsprotocols = [
            v.encode() if isinstance(v, str) else v for v in value
        ]

    def on_startup(self, fn: Callable[[], Awaitable[None]]) -> Callable[[], Awaitable[None]]:
        """Register a zero-argument coroutine; return it unchanged.

        Run in registration order before startup.complete. An exception aborts
        remaining hooks and withholds the acknowledgement.
        """
        return self._on_lifecycle_event('app_startup', fn)

    def _on_lifecycle_event(self, event_name: str,
                            fn: Callable[[], Awaitable[None]]) -> Callable[[], Awaitable[None]]:
        """Register *fn* as a zero-argument interceptor for *event_name*."""
        async def _adapter(_event: Event) -> None:
            await fn()
        self._dispatcher.intercept(event_name, _adapter)
        return fn

    def on_shutdown(self, fn: Callable[[], Awaitable[None]]) -> Callable[[], Awaitable[None]]:
        """Register a zero-argument coroutine; return it unchanged.

        Run in registration order before shutdown.complete. An exception aborts
        remaining hooks.
        """
        return self._on_lifecycle_event('app_shutdown', fn)

    def on_warmup(self, fn: Callable[['BlackBull'], Awaitable[None]]
                  ) -> Callable[['BlackBull'], Awaitable[None]]:
        """Register a coroutine to warm the app **before it binds or forks**.

        The hook runs once in the master, before the listening socket is
        created and before workers fork; [`on_startup`][blackbull.app.BlackBull.on_startup] runs per worker,
        after both.  In single-worker mode the one process is warmed before it
        binds.

        Hooks receive the ``app`` and must do **pure warming only** — drive hot
        code paths, prime codecs/TLS — and acquire **no** per-worker resources
        (DB pools, sockets, live connections); those belong in
        [`on_startup`][blackbull.app.BlackBull.on_startup].  Use [`warm_request`][] to exercise the ASGI
        dispatch/handler path in-process, and
        [`blackbull.server.warmup.warm_tls`][blackbull.server.warmup.warm_tls] to prime the TLS handshake.

        Warm-up is best-effort: a hook's exception is logged and swallowed
        (the master degrades to a cold start), and total warm-up time is capped
        by ``BB_WARMUP_BUDGET_S`` (default 60 s).  Multiple hooks run in
        registration order.  The Workers deployment page shows a hook and
        explains what makes a warmed master's heap survive the fork.
        """
        self._warmup_hooks.append(fn)
        return fn

    async def warm_request(self, conn: Connection, *, body: bytes = b'', n: int = 1
                           ) -> None:
        """Invoke the native app n times without sockets, discarding output.

        Each iteration copies conn with independent body/state/receive bindings.
        The synthetic receive yields body once. Intended for on_warmup hooks.
        """
        async def _send(_event) -> None:
            pass

        for _ in range(n):
            sent = False

            async def _receive():
                nonlocal sent
                if not sent:
                    sent = True
                    return {'type': 'http.request', 'body': body,
                            'more_body': False}
                return {'type': 'http.request', 'body': b'', 'more_body': False}

            fresh = Connection(
                method=conn.method, path=conn.path, raw_path=conn.raw_path,
                headers=conn.headers, query_string=conn.query_string,
                http_version=conn.http_version, scheme=conn.scheme, type=conn.type,
            )
            await self(fresh, _receive, _send)

    def on(self, event_name: str, *, blocking: bool = False
           ) -> Callable[[EventHandler], EventHandler]:
        """Observe an event; the decorated coroutine is returned unchanged.

        The default schedules a detached task. blocking=True awaits observers in
        registration order; cancellation can still interrupt them. Ordinary observer
        exceptions are logged and isolated. Use intercept for hooks that may change
        the request, and see docs/guide/events.md for event lifetimes.
        """
        def decorator(handler):
            self._dispatcher.on(event_name, handler, blocking=blocking)
            return handler
        return decorator

    async def drain_events(self, timeout: float = 5.0) -> bool:
        """Wait for detached (`@app.on`) observers to finish.  Returns success.

        Only detached observers need waiting for; the events guide says which
        do not.  ``False`` means *timeout* expired with work still outstanding.
        Nothing is cancelled; call again with a longer budget.
        """
        return await self._dispatcher.drain(timeout)

    def intercept(self, event_name: str) -> Callable[[EventHandler], EventHandler]:
        """Await interceptors in registration order; return the handler unchanged.

        Exceptions propagate and abort subsequent interceptors for that event.
        """
        def decorator(handler):
            self._dispatcher.intercept(event_name, handler)
            return handler
        return decorator

    async def _handle_lifespan(self, receive, send):
        # Driven entirely by ``receive``/``send``: the lifespan scope itself is
        # unused, so ``__call__`` does not pass it.
        while True:
            event = await receive()
            if event['type'] == 'lifespan.startup':
                if _DEBUG:
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
                try:
                    await self._dispatcher.emit(Event('app_startup'))
                except Exception as exc:
                    # A raising hook must answer with lifespan.*.failed rather
                    # than unwind the task: an unacked startup leaves
                    # LifespanManager.__aenter__ blocked forever, and .failed
                    # is the signal external hosts act on.
                    self._logger.error('app_startup hook failed:\n%s',
                                       traceback.format_exc())
                    await send({'type': 'lifespan.startup.failed', 'message': str(exc)})
                    return
                await send({'type': 'lifespan.startup.complete'})
            elif event['type'] == 'lifespan.shutdown':
                if _DEBUG:
                    self._logger.debug('lifespan shutdown')
                try:
                    await self._dispatcher.emit(Event('app_shutdown'))
                    await self._dispatcher.aclose()
                except Exception as exc:
                    # Answer, don't unwind — see the startup arm above.
                    self._logger.error('app_shutdown hook failed:\n%s',
                                       traceback.format_exc())
                    await send({'type': 'lifespan.shutdown.failed',
                                'message': str(exc)})
                    return
                await send({'type': 'lifespan.shutdown.complete'})
                return

    async def _dispatch(self, conn, receive: ASGIReceiveCallable | None,
                        send: ASGISendCallable):
        """Emit request_received/before_handler/after_handler while dispatching.

        BlackBull.__call__ emits request_completed after the global middleware chain;
        keep this boundary so external ASGI hosts get the same lifecycle.
        """
        if _DEBUG:
            self._logger.debug((conn, receive, send))

        # WebSocket has its own lifecycle events, so it returns before the HTTP
        # request_received emit and _dispatch_http below.
        if conn.type == 'websocket':
            path = conn.path
            try:
                function = self._router[(path, HTTPMethod.GET, Scheme.websocket)]
            except (MethodNotApplicable, PathNotRegistered):
                self._logger.warning('No websocket handler registered for %s', path)
                return
            await function(conn, receive, send)
            return

        scheme = _SCHEME_BY_VALUE.get(conn.type)
        if scheme is None:
            self._logger.error(f'Invalid scheme ({conn.type}) is requested.')
            raise Exception('Invalid scheme is requested.')

        dispatcher = self._dispatcher
        if dispatcher.has_listeners('request_received'):
            client = conn.client or ('-',)
            await dispatcher.emit(Event('request_received', detail={
                'conn':        conn,
                'client_ip':    str(client[0]),
                'method':       conn.method,
                'path':         conn.path,
                'http_version': conn.http_version,
                'headers':      conn.headers,
            }))
        await self._dispatch_http(conn, receive, send, scheme)

    async def _dispatch_http(self, conn, receive: ASGIReceiveCallable | None,
                             send: ASGISendCallable, scheme):
        """Route and run one HTTP request (the non-WebSocket half of _dispatch)."""
        # gRPC rides the HTTP/2 path; see enable_grpc.
        if self._grpc_registry is not None and scheme == Scheme.http:
            content_type = conn.headers.get(b'content-type', b'')
            if content_type.strip().startswith(b'application/grpc'):
                from .grpc import serve_grpc  # noqa: PLC0415 — optional subpackage
                await serve_grpc(self._grpc_registry, conn, receive, send)
                return

        # ``raw_send`` is retained so a route with declared response headers can
        # re-wrap with the injector *below* the adapter (see the hook block).
        raw_send = send
        send = _wrap_send_native(send)

        # RFC 9110 §9.1: methods are case-sensitive; preserve unknown tokens as str.
        method = _METHOD_BY_VALUE.get(conn.method, conn.method)

        path = conn.path
        if _DEBUG:
            self._logger.debug((path, scheme))

        try:
            function = self._router[(path, method, scheme)]
        except MethodNotApplicable as e:
            if _DEBUG:
                self._logger.debug("%s: path=%r method=%r allowed=%r",
                             HTTPStatus.METHOD_NOT_ALLOWED.phrase, path, method, e.allowed_methods)
            conn.state.update({
                'error_status': HTTPStatus.METHOD_NOT_ALLOWED,
                'allowed_methods': e.allowed_methods,
            })
            handler = self._error_router[HTTPStatus.METHOD_NOT_ALLOWED]
            if handler is not None:
                await handler(conn, receive, send)
            return
        except PathNotRegistered:
            if _DEBUG:
                self._logger.debug("%s: path=%r", HTTPStatus.NOT_FOUND.phrase, path)
            conn.state['error_status'] = HTTPStatus.NOT_FOUND
            handler = self._error_router[HTTPStatus.NOT_FOUND]
            if handler is not None:
                await handler(conn, receive, send)
            return

        # Per-route hooks, applied uniformly: this block names no method and no
        # feature.  ``accept_query`` (RFC 10008) is the only producer, and its
        # QUERY-specific logic lives inside the guard, not here.
        resp_headers = getattr(function, '_bb_response_headers', None)
        if resp_headers is not None:
            # Wrapped around raw_send, so the header also lands on the
            # centrally-rendered error response (e.g. a guard's 415).
            send = _wrap_send_native(_inject_response_headers(raw_send, resp_headers))
        guard = getattr(function, '_bb_request_guard', None)
        if guard is not None:
            try:
                guard(conn)
            except HTTPException as e:
                # Rejected before the handler: no handler lifecycle events.
                self._logger.info('%s on %s %s: %s', int(e.status),
                                  conn.method, path, e.detail or e)
                conn.state.update({
                    'error_status': e.status,
                    'error_exception': e,
                })
                handler = self._error_router.resolve(type(e), e.status)
                if handler is not None:
                    await handler(conn, receive, send)
                return

        if _DEBUG:
            self._logger.debug((self, function))
        exc_caught: Exception | None = None
        try:
            if self._dispatcher.has_listeners('before_handler'):
                await self._dispatcher.emit(Event('before_handler', detail={
                    'conn':     conn,
                    'client_ip': conn.client[0] if conn.client else '',
                    'method':    conn.method,
                    'path':      conn.path,
                    'handler':   function.__name__,
                }))
            await function(conn, receive, send)
        except (ClientDisconnected, ConnectionResetError) as e:
            # Peer disconnect during body reads is an ordinary close, not a handler failure.
            exc_caught = e
            if _DEBUG:
                self._logger.debug('client disconnected before request body completed')
        except HTTPException as e:
            exc_caught = e
            if is_server_error(e.status):
                self._logger.error(traceback.format_exc())
            else:
                self._logger.info('%s on %s %s: %s', int(e.status),
                                  conn.method, conn.path,
                                  e.detail or e)
        except Exception as e:
            exc_caught = e
            self._logger.error(traceback.format_exc())
        finally:
            if self._dispatcher.has_listeners('after_handler'):
                await self._dispatcher.emit(Event('after_handler', detail={
                    'conn':     conn,
                    'client_ip': conn.client[0] if conn.client else '',
                    'method':    conn.method,
                    'path':      conn.path,
                    'handler':   function.__name__,
                    'exception': exc_caught,
                }))

        if exc_caught is not None and not isinstance(exc_caught, ClientDisconnected):
            err_status = (exc_caught.status if isinstance(exc_caught, HTTPException)
                          else HTTPStatus.INTERNAL_SERVER_ERROR)
            conn.state.update({
                'error_status': err_status,
                'error_exception': exc_caught,
            })
            handler = self._error_router.resolve(type(exc_caught), err_status)
            if handler is not None:
                await handler(conn, receive, send)

    def _build_chain(self):
        chain = self._dispatch
        for mw in reversed(self._global_middlewares):
            # No per-middleware conversion adapter.  One would only be needed
            # if a framework producer (``StaticFiles``' start/body/pathsend,
            # ``CORS``' preflight) emitted ASGI dicts that bypassed the
            # handler-boundary adapter; all of them are native, so a second
            # altitude has nothing to catch.
            chain = functools.partial(mw, call_next=chain)
        self._chain = chain

    async def __call__(self, conn: 'Connection | dict',
                       receive: ASGIReceiveCallable | None,
                       send: ASGISendCallable):
        """The ASGI 3.0 callable, and the app's only native/ASGI boundary.

        Everything below this method threads a
        [`Connection`][blackbull.connection.Connection], in both modes.  An ASGI
        scope dict arrives only from an external host (uvicorn,
        ``httpx.ASGITransport``) or under ``BB_FORCE_ASGI_SCOPE=1``, and is
        converted here, once; the same edge wraps the host's ``send`` with the
        native→ASGI conversion, while BlackBull's own server passes a
        native-capable send through untouched.  Lifespan is the one scope that
        is never a request and never becomes a ``Connection``.

        Args:
            conn: A ``Connection``, or an ASGI scope dict from a host.
            receive: Optional because a handler that never reads a body never
                touches it — the router guards on a missing receive and
                dispatch completes normally.  A conforming ASGI host always
                passes a callable; the tolerance is for direct drives.
            send: The host's send callable.
        """
        # ``target`` is what the actor's disconnect-detecting receive wrapper
        # shares with us (the Connection natively, the scope dict in the compat
        # lanes); ``disconnected(target)`` reads the flag off whichever it is.
        if self._asgi or not isinstance(conn, Connection):
            send = _to_asgi_boundary(send)
        target = conn
        if isinstance(conn, Connection):
            request = conn
        elif conn.get('type') == 'lifespan':
            await self._handle_lifespan(receive, send)
            return
        elif conn.get('type') == 'websocket':
            # The WS extras are derived (``conn.subprotocols`` reads the request
            # header) or actor-set (``conn._ws``), so none of them needs the
            # scope dict past this point.
            request = conn.get(CONNECTION_STASH_KEY)
            if request is None:
                request = Connection.from_scope(conn, receive)
        else:
            # Reuse the stashed native Connection on compatibility scopes.
            # Do not rebind _receive to the disconnect wrapper: it captures conn and
            # would create a per-request reference cycle.
            request = conn.get(CONNECTION_STASH_KEY)
            if request is None:
                request = Connection.from_scope(conn, receive)

        if self._chain is None:
            self._build_chain()

        # Emit terminal events after the whole middleware chain, including buffered responses.
        dispatcher = self._dispatcher
        want_request_completed = (request.type == 'http'
                                  and dispatcher.has_listeners('request_completed'))
        if not (want_request_completed
                or dispatcher.has_listeners('scope_completed')):
            await self._chain(request, receive, send)
            return
        exc: BaseException | None = None
        try:
            await self._chain(request, receive, send)
        except BaseException as e:
            exc = e
            raise
        finally:
            if want_request_completed and not disconnected(target):
                conn = request
                log = conn.state.get('access_log')
                client = conn.client or ('-',)
                await dispatcher.emit(Event('request_completed', detail={
                    'conn':          conn,
                    'client_ip':      str(client[0]),
                    'method':         conn.method,
                    'path':           conn.path,
                    'http_version':   conn.http_version,
                    'status':         log.status if log else '-',
                    'response_bytes': log.response_bytes if log else 0,
                    'duration_ms':    log.duration_ms() if log else 0.0,
                }))
            if dispatcher.has_listeners('scope_completed'):
                st, rtype = request.state, request.type
                client, rpath = request.client, request.path
                err = exc or st.get('error_exception')
                await dispatcher.emit(Event('scope_completed', {
                    'conn':     request,
                    'type':      rtype,
                    'client_ip': str((client or ['-'])[0]),
                    'path':      rpath,
                    'exception': err,
                }))

    def to_asgi(self):
        """Return the ASGI 3.0 callable to hand to an external host (uvicorn …).

        The app is native internally in both modes; ``BlackBull(asgi=True)``
        applies the native→ASGI boundary conversion in ``__call__`` whenever
        the app is driven (scope entry included), so the app instance itself
        is the callable — ``uvicorn.run(app.to_asgi())`` or ``uvicorn.run(app)``
        are equivalent.  Requires the ``asgi=True`` constructor flag: without
        it the app is wired for BlackBull's own native server.
        """
        if not self._asgi:
            raise RuntimeError(
                'to_asgi() requires BlackBull(asgi=True); an asgi=False app '
                'is wired for BlackBull\u2019s own native server')
        return self.__call__

    def route(self, methods: str | HTTPMethod | Iterable[str | HTTPMethod] = [HTTPMethod.GET],
              path: str | re.Pattern = '/', scheme: Scheme | Iterable[Scheme] = Scheme.http,
              functions: list = [], middlewares: list = [],
              name: str | None = None,
              accept_query: Iterable[str] | None = None):
        """Register a route handler, optionally wrapping it in middlewares.

        ``accept_query`` (RFC 10008) names the request **media types** the
        route accepts — it is the value of the ``Accept-Query`` response
        header, **not** a switch that enables the QUERY method.  (A method is
        accepted purely by listing it in ``methods``; QUERY is no different
        from GET there.)  It is meaningful for the QUERY method, which carries
        a request body.  When set, the route's responses carry an
        ``Accept-Query`` header (an RFC 9651 Structured Field list of those
        media types), and QUERY requests are Content-Type-enforced: a missing
        media type is answered ``400``, an unaccepted one ``415`` (with the
        ``Accept-Query`` header so the client can correct).  A handler may raise
        [`UnprocessableQuery`][blackbull.UnprocessableQuery] for ``422`` on a well-formed but
        unprocessable query.  Enforcement applies only to QUERY requests; other
        methods on the same route still receive the ``Accept-Query`` header.
        """
        return self._router.route(
            methods=methods,
            path=path,
            scheme=scheme,
            functions=functions,
            middlewares=middlewares,
            name=name,
            accept_query=accept_query,
            )

    def group(self, middlewares=[]) -> 'RouteGroup':
        """Return a RouteGroup that prepends *middlewares* to every route."""
        return RouteGroup(self, middlewares)

    def register_converter(self, type_: type, converter: Callable | None = None):
        """Convert a custom simplified-handler return type to a supported sendable.

        The converter must return Response, str, bytes, None, or a JSON-able
        mapping, list or dataclass. Pass it directly or omit it to get a decorator.
        Converters registered after a route still apply to that route.
        """
        if converter is None:
            def _decorator(fn: Callable) -> Callable:
                self._router.register_converter(type_, fn)
                return fn
            return _decorator
        self._router.register_converter(type_, converter)
        return converter

    def use(self, mw) -> None:
        """Register a global middleware applied to every non-lifespan request."""
        self._global_middlewares.append(mw)
        self._chain = None  # invalidate cached chain

    def _is_route_free(self, path: str, methods) -> bool:
        """True when *path* has no route registered for any of *methods*.

        Used by [`static`][] before it claims the bare mount prefix, since
        registering an already-registered path replaces it silently.
        """
        for method in methods:
            try:
                self._router[(path, method, Scheme.http)]
            except (PathNotRegistered, MethodNotApplicable):
                continue
            return False
        return True

    def _static_miss(self):
        async def _static_not_found(conn, receive, send):
            conn.state['error_status'] = HTTPStatus.NOT_FOUND
            handler = self._error_router[HTTPStatus.NOT_FOUND]
            if handler is not None:
                await handler(conn, receive, send)
        return _static_not_found

    def static(self, url_prefix: str, root_dir: str | Path, *,
               cache: bool = False, index: str | None = None,
               conditional: bool = True) -> None:
        """Mount static GET/HEAD routes under url_prefix.

        cache=True retains file bodies in memory. index names a directory index;
        None disables it. conditional=False disables ETag/Last-Modified and 304
        responses. See docs/guide/static-files.md for cache and traversal limits.
        """
        from blackbull.middleware.static import StaticFiles
        root = Path(root_dir).resolve()
        self._static_roots.append((url_prefix, root))
        mw = StaticFiles(url_prefix=url_prefix, root_dir=root, cache=cache,
                         index=index, conditional=conditional)
        prefix = url_prefix.rstrip('/')
        methods = [HTTPMethod.GET, HTTPMethod.HEAD]
        chain = [mw, self._static_miss()]
        self._router.route(methods=methods,
                           path=f'{prefix}/{{filepath:path}}',
                           functions=list(chain))
        # The ``path`` converter is ``r'.+'``, so neither ``/assets`` nor
        # ``/assets/`` matches the route above — yet both must resolve for
        # ``index=`` to serve the mount root (``blackbull serve`` mounts at
        # ``/`` and depends on it).  Each gets its own exact-match entry.
        for bare in ((prefix, f'{prefix}/') if prefix else ('/',)):
            if self._is_route_free(bare, methods):
                self._router.route(methods=methods, path=bare,
                                   functions=list(chain))

    def on_error(self, key):
        """Register a (conn, receive, send) handler for a status or exception class.

        An int key is converted to HTTPStatus. conn.state supplies error_status,
        error_exception when an exception triggered it, and allowed_methods for 405.
        See docs/guide/error-handling.md for exception-class precedence.
        """
        if isinstance(key, int) and not isinstance(key, HTTPStatus):
            key = HTTPStatus(key)
        return self._error_router(key)

    def url_path_for(self, name: str, /, **params) -> str:
        """Return the path for the named route with *params* substituted."""
        return self._router.url_path_for(name, **params)

    def get_routes(self) -> 'list[RouteInfo]':
        """Return a shallow snapshot in registration order, one RouteInfo per HTTP method.

        Changing the returned list cannot mutate the live router.
        """
        return self._router.get_routes()

    def enable_grpc(self, registry) -> None:
        """Serve all four gRPC call shapes from registry over HTTP/2.

        Handlers exchange raw message bytes without a protobuf dependency. Use
        TLS+ALPN or h2c; status is sent in trailers. HTTP routes remain available.
        """
        self._grpc_registry = registry

    def enable_openapi(self, *,
                       title: str = 'BlackBull API',
                       version: str = '0.1.0',
                       description: str | None = None,
                       spec_path: str = '/openapi.json',
                       docs_path: str | None = '/docs') -> None:
        """Publish OpenAPI JSON at spec_path and Swagger UI at docs_path.

        Call once. The spec is regenerated per request to include new routes.
        docs_path=None disables the UI; see docs/guide/openapi.md.
        """
        from .openapi import OpenAPIExtension  # noqa: PLC0415

        OpenAPIExtension(
            self,
            title=title,
            version=version,
            description=description,
            spec_path=spec_path,
            docs_path=docs_path,
        )

    def register_protocol_handler(
        self,
        name: str,
        handler: Callable[..., Awaitable[None]],
        *,
        detector: object | None = None,
        port: int | None = None,
        tls: bool = False,
        stateful: bool = True,
    ) -> RawBinding:
        """Register a handler for a non-ASGI (raw) protocol.

        The handler is an async callable ``(reader, writer, ctx) -> None`` that
        owns the connection for its whole lifetime.  When *port* is set, the
        server binds an additional listening socket on it; connections there
        skip HTTP detection and go straight to *handler*.

        Args:
            name: Protocol name (e.g. ``'echo'``, ``'mqtt'``); must be unique.
            handler: Async ``(reader, writer, ctx)`` coroutine.
            detector: First-byte sniffing on the shared HTTP port, so this
                protocol can be reached there as well as on its own port.
            port: Dedicated listening port for this protocol.
            tls: Serve this port through the server's TLS machinery (e.g.
                ``mqtts://``).  Requires the server to be configured with a
                certificate; startup fails fast otherwise.
            stateful: Whether an exchange depends on what an earlier one left
                behind — true by default.  A stateful protocol is served by one
                worker, so with ``workers > 1`` it is reached on its own port
                only; the shared port would answer from whichever worker
                accepted.  A stateful protocol with *no* dedicated port cannot
                be given one owner at all and is refused before the workers
                fork.  Pass ``False`` for a protocol that keeps nothing between
                exchanges.

        Returns:
            The registered binding.
        """
        if self._protocol_registry is None:
            from .server.protocol_registry import ProtocolRegistry  # noqa: PLC0415
            self._protocol_registry = ProtocolRegistry()
        return self._protocol_registry.register(
            name, handler, detector=detector, port=port, tls=tls,
            stateful=stateful)

    def raw_handler(self, name: str, *, port: int | None = None,
                    detector: object | None = None, tls: bool = False,
                    stateful: bool = True):
        """Decorator form of [`register_protocol_handler`][blackbull.app.BlackBull.register_protocol_handler].

        ::

            @app.raw_handler('echo', port=9000)
            async def echo(reader, writer, ctx):
                while data := await reader.read(1024):
                    await writer.write(data)
        """
        def decorator(handler):
            self.register_protocol_handler(name, handler,
                                           detector=detector, port=port,
                                           tls=tls, stateful=stateful)
            return handler
        return decorator

    def add_extension(self, ext):
        """Call ext.init_app(app) immediately and return ext.

        Subclassing Extension is optional. Optional async startup(app)/shutdown(app)
        methods are registered as lifespan hooks.
        """
        if not hasattr(ext, 'init_app'):
            raise TypeError(
                f"{type(ext).__name__} is not a valid extension: it must "
                f"expose init_app(app).")
        ext.init_app(self)
        startup = getattr(ext, 'startup', None)
        if startup is not None:
            self.on_startup(lambda: startup(self))
        shutdown = getattr(ext, 'shutdown', None)
        if shutdown is not None:
            self.on_shutdown(lambda: shutdown(self))
        return ext

    def run(self, certfile=None, keyfile=None, port: int | None = None,
            unix_path: str | None = None,
            inherited_fd: int | None = None,
            listeners: list | None = None,
            workers: int | None = None,
            max_connections: int | None = None,
            stream_queue_depth: int | None = None,
            ws_queue_depth: int | None = None,
            reload: bool | None = None,
            reload_paths: list | None = None) -> None:
        """Run synchronously; do not wrap in asyncio.run.

        Unset options resolve through BLACKBULL_* environment variables, .env,
        AppConfig and defaults. BB_* controls server tuning. Multiple workers or
        reload use a blocking supervisor. External ASGI hosts can call the app;
        embedded event-loop use requires ASGIServer. See the Configuration guide.
        """
        from .config import resolve_run_config, log_config_sources  # noqa: PLC0415

        resolved, sources = resolve_run_config(
            {
                'certfile': certfile, 'keyfile': keyfile, 'port': port,
                'unix_path': unix_path, 'inherited_fd': inherited_fd,
                'workers': workers, 'max_connections': max_connections,
                'stream_queue_depth': stream_queue_depth,
                'ws_queue_depth': ws_queue_depth,
                'reload': reload, 'reload_paths': reload_paths,
            },
            self._config,
        )
        log_config_sources(resolved, sources)
        if listeners:
            # No env var or config file resolves a listener list, so a resolved
            # port/path/fd would contradict it rather than default it.
            for said_another_way in ('port', 'unix_path', 'inherited_fd'):
                resolved.pop(said_another_way, None)
            resolved['listeners'] = listeners
        serve(self, **resolved)


def serve(app, *,
          certfile=None, keyfile=None, port=0,
          unix_path: str | None = None,
          inherited_fd: int | None = None,
          listeners: list | None = None,
          workers: int | None = None,
          max_connections: int | None = None,
          stream_queue_depth: int | None = None,
          ws_queue_depth: int | None = None,
          reload: bool = False,
          reload_paths: list | None = None) -> None:
    """Run a BlackBull instance or ASGI 3.0 callable synchronously.

    One worker without reload runs in this process; multiple workers or reload
    use a blocking supervisor. listeners replaces port/unix_path/inherited_fd;
    passing both is refused. Unset workers, max_connections and queue depths
    use BB_* settings. Deployment options do not resolve BLACKBULL_* here.
    """
    if listeners and (port or unix_path is not None or inherited_fd is not None):
        raise TypeError(
            'listeners= states the sockets by itself; pass port / unix_path / '
            'inherited_fd or listeners, not both.')
    from .env import get_settings as _get_settings  # noqa: PLC0415
    import os as _os  # noqa: PLC0415
    _cfg = _get_settings()
    workers = workers if workers is not None else _cfg.workers
    workers = workers or (_os.cpu_count() or 1)

    # Reload is the exception to "protocol on worker 0, HTTP on every worker";
    # see docs/deployment/workers.md.  The warning below states the rest.
    if (isinstance(app, BlackBull) and app._protocol_registry is not None
            and app._protocol_registry.has_port_bindings() and workers > 1
            and reload):
        logger.warning(
            'Auto-reload does not yet hand stateful protocol listeners across '
            'its exec; forcing workers=1 (was %d). Run without reload to scale '
            'HTTP alongside the broker.', workers)
        workers = 1
    max_connections = max_connections if max_connections is not None else _cfg.max_connections
    stream_queue_depth = (stream_queue_depth if stream_queue_depth is not None
                          else _cfg.stream_queue_depth)
    ws_queue_depth = ws_queue_depth if ws_queue_depth is not None else _cfg.ws_queue_depth

    # Validate before the workers fork, so a bad route fails once and loudly.
    if isinstance(app, BlackBull):
        app._router.validate()

    # Reload needs the master+worker structure: a long-lived supervisor has to
    # hold the listening sockets across worker recycles.
    if workers == 1 and not reload:
        _serve_single_worker(
            app,
            _cfg,
            certfile=certfile,
            keyfile=keyfile,
            port=port,
            unix_path=unix_path,
            inherited_fd=inherited_fd,
            listeners=listeners,
            max_connections=max_connections,
            stream_queue_depth=stream_queue_depth,
            ws_queue_depth=ws_queue_depth,
        )
        return

    from .server import ASGIServer  # noqa: PLC0415
    from .server.multiworker import MultiWorkerServer  # noqa: PLC0415

    # Workers inherit these sockets via fork.  After a reload re-exec,
    # open_socket adopts the inherited fds instead of binding.
    master_server = ASGIServer(app, certfile=certfile, keyfile=keyfile,
                               max_connections=max_connections,
                               stream_queue_depth=stream_queue_depth,
                               ws_queue_depth=ws_queue_depth,
                               listeners=listeners)

    # Before open_socket and before the fork — see on_warmup.
    from .protocol.rsock import close_sockets  # noqa: PLC0415
    from .server.warmup import run_warmup  # noqa: PLC0415
    supervisor_ssl_context = master_server.ssl_context
    run_warmup(app, supervisor_ssl_context)

    bound_listeners = None
    try:
        master_server.open_socket(
            port, unix_path=unix_path, inherited_fd=inherited_fd)
        addr = (f'unix:{master_server.unix_path}'
                if master_server.unix_path else
                f'port {master_server.port}')
        logger.info(
            'Starting %d worker(s) on %s%s', workers, addr,
            ' [auto-reload]' if reload else '',
        )

        # The local scope owns the detached listeners until the supervisor has
        # entered run(); re-closing after its own rollback is harmless.
        bound_listeners = master_server._take_bound_listeners()

        MultiWorkerServer(
            app,
            bound_listeners,
            supervisor_ssl_context,
            workers=workers,
            max_connections=max_connections,
            stream_queue_depth=stream_queue_depth,
            ws_queue_depth=ws_queue_depth,
            reload=reload,
            reload_paths=reload_paths,
        ).run()
    except BaseException:
        try:
            if bound_listeners is None:
                cleanup_error = master_server._close_socket()
            else:
                cleanup_error = close_sockets(
                    sock
                    for _listener, sockets in bound_listeners
                    for sock in sockets)
        except BaseException as exc:
            cleanup_error = exc
        if cleanup_error is not None:
            try:
                logger.error(
                    'Failed to close listeners during startup rollback: %s',
                    cleanup_error)
            except BaseException:
                # Logging caused, or must not replace, the startup failure.
                pass
        raise


def _serve_single_worker(
    app,
    cfg,
    *,
    certfile,
    keyfile,
    port,
    unix_path,
    inherited_fd,
    listeners,
    max_connections,
    stream_queue_depth,
    ws_queue_depth,
) -> None:
    import logging as _logging  # noqa: PLC0415
    from .logger import setup_async_logging, teardown_async_logging  # noqa: PLC0415
    from .env import apply_event_loop_policy  # noqa: PLC0415
    from .server import ASGIServer  # noqa: PLC0415

    apply_event_loop_policy(cfg)
    if cfg.async_logging:
        setup_async_logging(
            log_format=cfg.log_format,
            syslog_addr=cfg.log_syslog_addr,
            batch_size=cfg.log_batch_size,
            batch_timeout_ms=cfg.log_batch_timeout_ms,
            log_file=cfg.log_file,
        )
    if not cfg.access_log:
        _logging.getLogger('blackbull.access').setLevel(_logging.WARNING)
    try:
        asyncio.run(
            _run_single(
                app,
                certfile=certfile,
                keyfile=keyfile,
                port=port,
                unix_path=unix_path,
                inherited_fd=inherited_fd,
                listeners=listeners,
                max_connections=max_connections,
                stream_queue_depth=stream_queue_depth,
                ws_queue_depth=ws_queue_depth,
                drain_timeout=cfg.worker_drain_timeout,
            )
        )
    finally:
        teardown_async_logging()


async def _run_single(app, *, certfile, keyfile, port, unix_path, inherited_fd,
                      listeners, max_connections, stream_queue_depth,
                      ws_queue_depth, drain_timeout):
    """Single-worker server loop — invoked from ``serve``."""
    from .server import ASGIServer  # noqa: PLC0415
    from .server.server import _SigtermCapture  # noqa: PLC0415
    server = ASGIServer(app, certfile=certfile, keyfile=keyfile,
                        max_connections=max_connections,
                        stream_queue_depth=stream_queue_depth,
                        ws_queue_depth=ws_queue_depth,
                        listeners=listeners)

    # Before binding, on the serving loop — no temporary loop needed.
    from .server.warmup import warmup_inline  # noqa: PLC0415
    await warmup_inline(app, server.ssl_context)

    server.open_socket(port, unix_path=unix_path, inherited_fd=inherited_fd)
    with _SigtermCapture(server, drain_timeout) as sigterm:
        if not sigterm.installed:
            logger.info('SIGTERM not capturable here; it stays an immediate '
                        'termination')
        await server.run(port=port)
