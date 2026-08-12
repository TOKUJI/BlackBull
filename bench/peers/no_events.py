"""Lifecycle-event knock-out for the Sprint 100 Phase C2 measurement.

Armed by ``BB_NO_EVENTS=1`` on the BlackBull bench app.  Replaces
``BlackBull._dispatch`` / ``_dispatch_http`` / ``__call__`` with
event-stripped copies so the FOUR request-lifecycle guards (``request_received``
in ``_dispatch``, ``before_handler`` + ``after_handler`` in ``_dispatch_http``,
``request_completed``/``scope_completed`` in ``__call__``) are removed from the
hot path.  A bare A/B (normal BB bare vs no-events BB bare, same session)
quantifies the events' per-request cost.

The copies are exact for every non-event statement — the only deletions are the
``has_listeners`` guards and the guarded ``emit`` blocks.  The local E2E smoke
verifies both variants serve identical responses and that the no-events variant
performs zero ``has_listeners`` calls.

Measurement-only bench code; never import outside the bench.
"""
import traceback
from http import HTTPStatus, HTTPMethod

from blackbull.app import (
    BlackBull,
    _inject_response_headers,
    _to_asgi_boundary,
    _wrap_send_native,
)
from blackbull.connection import CONNECTION_STASH_KEY, Connection
from blackbull.request import ClientDisconnected
from blackbull.router import HTTPException, MethodNotApplicable, PathNotRegistered
from blackbull.utils import Scheme, is_server_error


def _install_no_events(app) -> None:  # noqa: ARG001
    """Monkeypatch the app's class so no request-lifecycle event guard runs.

    ``_dispatch_http`` is long; to keep the copy exact the deletions are only
    the two guarded blocks (before_handler before the handler call,
    after_handler in the ``finally``).  ``_dispatch`` drops the
    request_received block; ``__call__`` drops the terminal-event fast-path
    checks and always takes the plain ``_chain`` path.
    """

    async def _no_events_dispatch(self, conn, receive, send):
        self._logger.debug((conn, receive, send))
        if conn.type == 'websocket':
            path = conn.path
            try:
                function = self._router[(path, HTTPMethod.GET, Scheme.websocket)]
            except (MethodNotApplicable, PathNotRegistered):
                self._logger.warning('No websocket handler registered for %s', path)
                return
            await function(conn, receive, send)
            return
        try:
            scheme = Scheme(conn.type)
        except ValueError:
            self._logger.error(f'Invalid scheme ({conn.type}) is requested.')
            raise Exception('Invalid scheme is requested.')
        await self._dispatch_http(conn, receive, send, scheme)

    async def _no_events_dispatch_http(self, conn, receive, send, scheme):
        if self._grpc_registry is not None and scheme == Scheme.http:
            content_type = conn.headers.get(b'content-type', b'')
            if content_type.strip().startswith(b'application/grpc'):
                from blackbull.grpc import serve_grpc  # noqa: PLC0415
                await serve_grpc(self._grpc_registry, conn, receive, send)
                return
        raw_send = send
        send = _wrap_send_native(send)
        try:
            method = HTTPMethod(conn.method)
        except ValueError:
            method = conn.method
        path = conn.path
        self._logger.debug((path, scheme))
        try:
            function = self._router[(path, method, scheme)]
        except MethodNotApplicable as e:
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
            self._logger.debug("%s: path=%r", HTTPStatus.NOT_FOUND.phrase, path)
            conn.state['error_status'] = HTTPStatus.NOT_FOUND
            handler = self._error_router[HTTPStatus.NOT_FOUND]
            if handler is not None:
                await handler(conn, receive, send)
            return
        resp_headers = getattr(function, '_bb_response_headers', None)
        if resp_headers is not None:
            send = _wrap_send_native(_inject_response_headers(raw_send, resp_headers))
        guard = getattr(function, '_bb_request_guard', None)
        if guard is not None:
            try:
                guard(conn)
            except HTTPException as e:
                self._logger.info('%s on %s %s: %s', int(e.status),
                                  conn.method, path, e.detail or e)
                conn.state.update({
                    'error_status': e.status,
                    'error_exception': e,
                })
                handler = self._error_router[e.status]
                if handler is not None:
                    await handler(conn, receive, send)
                return
        self._logger.debug((self, function))
        exc_caught: Exception | None = None
        try:
            await function(conn, receive, send)
        except ClientDisconnected as e:
            exc_caught = e
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
        if exc_caught is not None and not isinstance(exc_caught, ClientDisconnected):
            err_status = (exc_caught.status if isinstance(exc_caught, HTTPException)
                          else HTTPStatus.INTERNAL_SERVER_ERROR)
            conn.state.update({
                'error_status': err_status,
                'error_exception': exc_caught,
            })
            handler = self._error_router[exc_caught]
            if handler is not None:
                await handler(conn, receive, send)

    async def _no_events_call(self, conn, receive, send):
        if self._asgi or not isinstance(conn, Connection):
            send = _to_asgi_boundary(send)
        if isinstance(conn, Connection):
            request = conn
        elif conn.get('type') == 'lifespan':
            await self._handle_lifespan(receive, send)
            return
        elif conn.get('type') == 'websocket':
            request = conn.get(CONNECTION_STASH_KEY)
            if request is None:
                request = Connection.from_scope(conn, receive)
        else:
            request = conn.get(CONNECTION_STASH_KEY)
            if request is None:
                request = Connection.from_scope(conn, receive)
        if self._chain is None:
            self._build_chain()
        await self._chain(request, receive, send)

    BlackBull._dispatch = _no_events_dispatch
    BlackBull._dispatch_http = _no_events_dispatch_http
    BlackBull.__call__ = _no_events_call
