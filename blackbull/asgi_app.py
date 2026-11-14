"""Host a foreign ASGI 3.0 application on BlackBull's server.

[`ASGIApp`][] is the one place the server's native ``Connection`` and send
become an ASGI scope and ASGI event dicts.
"""
import asyncio
import logging
from http import HTTPStatus

from .connection import Connection, disconnected, mark_disconnected
from .event import EventDispatcher, EventHooks
from .event_aggregator import emit_scope_end, handler_event, request_received_event
from .native import NativeWSMessage, _native_from_asgi
from .request import ClientDisconnected
from .response import Response

logger = logging.getLogger(__name__)


class SendAfterDisconnect(OSError):
    """``send()`` after the client went away; ending there is an ordinary close."""


def _blackbull_in(app):
    """The BlackBull app *app* is, or wraps through ``__wrapped__``; else None."""
    from .app import BlackBull  # noqa: PLC0415 — app imports the server
    while app is not None:
        if isinstance(app, BlackBull):
            return app
        app = getattr(app, '__wrapped__', None)
    return None


# _REFUSED: the transport refused a message, so it alone judges what may follow.
_START, _BODY, _TRAILERS, _DONE, _REFUSED = range(5)


class _Exchange:
    """One HTTP request's ASGI channels; ``send`` supervises a foreign app."""

    __slots__ = ('conn', 'scope', '_receive', '_send', '_state', '_trailers', '_gone',
                 '_body_done', '_settled')

    def __init__(self, conn, receive, send) -> None:
        self.conn = conn
        self.scope = conn.to_asgi_scope(force_asgi=True)
        self._receive = receive
        self._send = send
        self._state = _START
        self._trailers = False
        self._gone = False
        self._body_done = False
        self._settled: asyncio.Event | None = None

    async def receive(self):
        """After the body, ``http.disconnect`` only once the client is gone or
        the response is complete: HTTP/1.1 answers such a read at once and
        sees the peer leave only through a failed send."""
        if self._body_done and self.conn.http_version != '2':
            if self._settled is None:
                self._settled = asyncio.Event()
                if self._gone or self._state == _DONE:
                    self._settled.set()
            await self._settled.wait()
            return {'type': 'http.disconnect'}
        event = await self.relay_receive()
        if event.get('type') == 'http.request' and not event.get('more_body', False):
            self._body_done = True
        return event

    async def relay_receive(self):
        event = await self._receive()
        if event.get('type') == 'http.disconnect' and not self._gone:
            self._gone = True
            # A BlackBull app reads the flag off the scope it was given.
            mark_disconnected(self.scope)
        return event

    async def relay(self, event):
        """Convert only: a BlackBull app answers for itself, as when served natively."""
        await self._send(_native_from_asgi(event))

    async def send(self, event):
        if self._gone:
            raise SendAfterDisconnect('the client disconnected')
        state = self._next_state(event)
        try:
            await self._send(_native_from_asgi(event))
        except ClientDisconnected:
            self._gone = True
            self._settle()
            raise SendAfterDisconnect('the client disconnected') from None
        except Exception:
            self._state = _REFUSED
            raise
        self._state = state
        if state == _DONE:
            self._settle()

    def _settle(self):
        if self._settled is not None:
            self._settled.set()

    def _next_state(self, event):
        kind = event.get('type')
        state = self._state
        if state == _REFUSED:
            return state
        if state == _START and kind == 'http.response.start':
            self._trailers = bool(event.get('trailers', False))
            return _BODY
        if state == _BODY and kind == 'http.response.body':
            if event.get('more_body', False):
                return _BODY
            return _TRAILERS if self._trailers else _DONE
        if state == _BODY and kind == 'http.response.pathsend':
            return _TRAILERS if self._trailers else _DONE
        if state == _BODY and kind == 'http.response.push':
            return _BODY
        # Declared trailers may also end the body directly (gRPC trailers-only).
        if (state in (_BODY, _TRAILERS) and self._trailers
                and kind == 'http.response.trailers'):
            return _TRAILERS if event.get('more_trailers', False) else _DONE
        raise RuntimeError(f'unexpected ASGI message {kind!r} at this point '
                           'of the response')

    async def run(self, call):
        """Await *call*; answer 500 if it fails or returns before the response starts.

        Returns the exception answered for.  One raised after the response
        started, or a return before it completed, propagates: the transport
        then ends the exchange.
        """
        try:
            await call()
        except SendAfterDisconnect:
            return None
        except Exception as exc:
            if self._gone:
                return None
            logger.error('Exception in ASGI application', exc_info=exc)
            if self._state == _START:
                await self._internal_error()
                return exc
            if self._state == _DONE:
                return exc
            raise
        finally:
            # Even unwaited: a receive() outliving the app must not hang.
            if self._settled is None:
                self._settled = asyncio.Event()
            self._settled.set()
        if self._gone or self._state in (_DONE, _REFUSED):
            return None
        if self._state == _START:
            logger.error('ASGI application returned without starting a response')
            await self._internal_error()
            return None
        raise RuntimeError('ASGI application returned without completing the response')

    async def _internal_error(self):
        self._state = _DONE
        await self._send(Response(b'Internal Server Error',
                                  status=HTTPStatus.INTERNAL_SERVER_ERROR,
                                  content_type='text/plain; charset=utf-8').to_native())


class ASGIApp(EventHooks):
    """Serve the ASGI 3.0 application *app* as a native BlackBull callable.

    Construct it directly to register listeners with ``on``/``intercept``.
    Around a BlackBull app (also via ``__wrapped__``) it only converts; that
    app fires its own events and errors as when served natively.

    Until BLA-675/BLA-676 the lifespan scope passes unchanged and a foreign
    app's WebSocket requests are refused.
    """

    @property
    def available_ws_protocols(self) -> list[bytes]:
        """The wrapped BlackBull app's WebSocket subprotocols, else none."""
        return self._inner.available_ws_protocols if self._inner is not None else []

    def __init__(self, app) -> None:
        self.app = app
        inner = _blackbull_in(app)
        self._inner = inner
        self._dispatcher = inner._dispatcher if inner is not None else EventDispatcher()
        # Its WebSocket sends reach the actor directly, as native code.
        self._warns_native_dicts = inner is not None and inner._warns_native_dicts

    async def __call__(self, conn, receive, send):
        if not isinstance(conn, Connection):
            await self.app(conn, receive, send)
            return
        if conn.type == 'websocket':
            if self._inner is not None:
                await self.app(conn, receive, send)
            else:
                await send(NativeWSMessage.close())
                await emit_scope_end(self._dispatcher, conn, None, completed=False)
            return
        exchange = _Exchange(conn, receive, send)
        if self._inner is not None:
            await self.app(exchange.scope, exchange.relay_receive, exchange.relay)
            return
        await self._serve(conn, exchange)

    async def _serve(self, conn, exchange):
        dispatcher = self._dispatcher
        app = self.app
        called = False

        async def call():
            nonlocal called
            if dispatcher.has_listeners('request_received'):
                await dispatcher.emit(request_received_event(conn))
            if dispatcher.has_listeners('before_handler'):
                await dispatcher.emit(handler_event('before_handler', conn, app))
            called = True
            await app(exchange.scope, exchange.receive, exchange.send)

        exc = None
        try:
            exc = await exchange.run(call)
        except BaseException as e:
            exc = e
            raise
        finally:
            if called and dispatcher.has_listeners('after_handler'):
                await dispatcher.emit(handler_event('after_handler', conn, app, exc))
            # As natively: only a disconnect seen by receive withholds request_completed.
            await emit_scope_end(dispatcher, conn, exc,
                                 completed=not disconnected(exchange.scope))
