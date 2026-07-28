"""The high-level WebSocket handler object (Sprint 82).

A :class:`WebSocket` wraps the raw ``(conn, receive, send)`` triplet so a
handler works in **data and methods** instead of event dicts::

    @app.route(path='/chat', scheme=Scheme.websocket)
    async def chat(ws: WebSocket):
        await ws.accept()
        async for message in ws:
            await ws.send_text(message)

The equivalent raw handler has to know that the first ``receive()`` yields
``websocket.connect``, that ``accept`` is a *send*, that a text message hides
under ``event['text']`` while a binary one hides under ``event['bytes']``,
and that the loop ends on a ``websocket.disconnect`` whose code lives in yet
another key.  None of that is protocol knowledge — it is transport encoding,
which is exactly what a framework should absorb.

**The raw triplet form is not deprecated.**  It stays supported for at least
a year (see ``docs/guide/websockets.md``); this object is additive, and both
forms run over the same actor, codec, and sender.  Nothing about the wire
changes — a handler that uses this object produces byte-identical frames to
one that sends the dicts by hand.

Naming follows the Sprint 80 rule: this is a *native* surface, so it is
unprefixed and lives outside ``asgi.py``.  The ``ASGIEvent`` dicts it builds
are the boundary representation, and stay there.
"""
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from .asgi import (ASGIEvent, WebSocketAcceptEvent, WebSocketCloseEvent,
                   WebSocketSendEvent)
from .connection import Connection
from .headers import Headers

logger = logging.getLogger(__name__)

__all__ = ['WebSocket', 'WebSocketDisconnect']

#: RFC 6455 §7.4.1 normal closure — the default for :meth:`WebSocket.close`.
#: Spelled out rather than imported from ``blackbull.server.constants``: this
#: is a user-facing handler object, and the public package should not have to
#: reach into the server stack for two integers.
_NORMAL_CLOSURE = 1000

#: Close code reported when the peer went away without sending one.  RFC 6455
#: §7.1.5 — 1005 is "no status received"; it is never sent on the wire, it is
#: what the *application* observes in that case.
_NO_STATUS = 1005


class WebSocketDisconnect(Exception):
    """The peer closed the connection.

    Raised by :meth:`WebSocket.receive` and its typed variants.  Iterating
    with ``async for`` handles this for you — the loop simply ends — so catch
    it only when you call ``receive()`` directly and need the close code.

    ``code`` is the RFC 6455 close code the peer sent, or 1005 when it sent
    none.  ``reason`` is its optional UTF-8 explanation.
    """

    def __init__(self, code: int = _NO_STATUS, reason: str | None = None):
        self.code = code
        self.reason = reason
        detail = f'{code}' if not reason else f'{code} ({reason})'
        super().__init__(f'WebSocket closed by peer: {detail}')


class WebSocket:
    """One WebSocket connection, as an object.

    Constructed by the framework and handed to handlers that ask for it by
    annotation (``ws: WebSocket``) or by name (``ws`` / ``websocket``).  A
    handler that takes the raw ``(conn, receive, send)`` triplet keeps
    receiving exactly that, and this object is never built for it.

    The handshake is explicit: the connection is not live until
    :meth:`accept` returns, and sending before that is an error.  To reject a
    connection, call :meth:`close` instead of :meth:`accept`.
    """

    __slots__ = ('_conn', '_receive', '_send', '_connect_seen', '_accepted',
                 '_closed', '_disconnected', '_close_code', '_close_reason')

    def __init__(self,
                 conn: Connection,
                 receive: Callable[[], Awaitable[Any]],
                 send: Callable[..., Awaitable[None]]) -> None:
        self._conn = conn
        self._receive = receive
        self._send = send
        self._connect_seen = False
        self._accepted = False
        self._closed = False
        self._disconnected = False
        self._close_code: int | None = None
        self._close_reason: str | None = None

    def __repr__(self) -> str:
        if self._disconnected:
            state = f'disconnected code={self._close_code}'
        elif self._closed:
            state = 'closed'
        elif self._accepted:
            state = 'open'
        else:
            state = 'connecting'
        return f'<WebSocket {self._conn.path!r} {state}>'

    # ---- connection facts (delegated to the native Connection) -----------

    @property
    def connection(self) -> Connection:
        """The underlying :class:`~blackbull.connection.Connection`.

        Everything the handshake carried — headers, cookies, TLS state, the
        client address — is reachable through it.  The shortcuts below cover
        the fields WebSocket handlers actually reach for.
        """
        return self._conn

    @property
    def path(self) -> str:
        return self._conn.path

    @property
    def headers(self) -> Headers:
        return self._conn.headers

    @property
    def path_params(self) -> dict[str, Any]:
        return self._conn.path_params

    @property
    def query_string(self) -> bytes:
        return self._conn.query_string

    @property
    def client(self) -> tuple[str, int | None] | None:
        """``(host, port)`` of the peer; the port is ``None`` on a UDS."""
        return self._conn.client

    @property
    def subprotocols(self) -> list[str]:
        """Subprotocols the client offered, in its order of preference."""
        return self._conn.subprotocols

    # ---- handshake state -------------------------------------------------

    @property
    def accepted(self) -> bool:
        """True once :meth:`accept` has completed the handshake."""
        return self._accepted

    @property
    def client_disconnected(self) -> bool:
        """True once the peer's close has been observed.

        Only ever set by *reading* — the disconnect arrives on the receive
        channel, so a handler that has stopped receiving will not see this
        flip.  Use it to break out of a send-only loop that also receives.
        """
        return self._disconnected

    @property
    def close_code(self) -> int | None:
        """The close code, once either side has closed; ``None`` while open."""
        return self._close_code

    @property
    def close_reason(self) -> str | None:
        return self._close_reason

    # ---- handshake -------------------------------------------------------

    async def _consume_connect(self) -> None:
        """Pop the opening ``websocket.connect`` event, once.

        The raw form makes every handler do this by hand before it may
        accept.  It carries no information beyond "a client is offering a
        handshake", so the object absorbs it.
        """
        if self._connect_seen:
            return
        self._connect_seen = True
        event = await self._receive()
        etype = event.get('type')
        if etype == ASGIEvent.WS_DISCONNECT:
            # The peer gave up mid-handshake.  Record it so accept()/close()
            # can decide what to do rather than writing to a dead transport.
            self._note_disconnect(event)
        elif etype != ASGIEvent.WS_CONNECT:
            # Something already took the handshake off the channel — almost
            # always the built-in ``websocket`` middleware, which accepts on
            # the handler's behalf.  Swallowing this event would silently eat
            # the client's first message, so refuse loudly instead.
            raise RuntimeError(
                f'WebSocket handshake was already consumed: expected '
                f'{ASGIEvent.WS_CONNECT!r} but the channel yielded '
                f'{etype!r}.  The high-level WebSocket object drives the '
                f'handshake itself, so it cannot be combined with middleware '
                f'that accepts for you (blackbull.middleware.websocket) — '
                f'drop the middleware, or use the raw (conn, receive, send) '
                f'handler form with it.')

    async def accept(self,
                     subprotocol: str | None = None,
                     *,
                     headers: list[tuple[bytes, bytes]] | None = None) -> None:
        """Complete the handshake.

        *subprotocol* names the one being accepted from :attr:`subprotocols`;
        leaving it ``None`` keeps the server's automatic negotiation, exactly
        as sending ``{'type': 'websocket.accept', 'subprotocol': None}`` does
        on the raw path.  *headers* are extra response headers for the 101.

        Raises :class:`WebSocketDisconnect` if the peer abandoned the
        handshake before it could be completed.
        """
        if self._accepted:
            raise RuntimeError('WebSocket.accept() called more than once')
        if self._closed:
            raise RuntimeError('WebSocket.accept() called after close()')
        await self._consume_connect()
        if self._disconnected:
            raise WebSocketDisconnect(self._close_code or _NO_STATUS,
                                      self._close_reason)
        event: WebSocketAcceptEvent = {
            'type': ASGIEvent.WS_ACCEPT, 'subprotocol': subprotocol}
        if headers is not None:
            event['headers'] = headers
        await self._send(event)
        self._accepted = True

    async def close(self,
                    code: int = _NORMAL_CLOSURE,
                    reason: str | None = None) -> None:
        """Close the connection, or reject the handshake.

        Called before :meth:`accept`, this rejects the connection — the
        client's ``connect()`` fails rather than opening and immediately
        closing.  Idempotent, and a no-op once the peer has already gone, so
        a ``finally: await ws.close()`` is always safe.
        """
        if self._closed or self._disconnected:
            self._closed = True
            return
        # A rejection still has to answer the handshake offer, so the connect
        # event must be consumed first — but a peer that vanished during it
        # needs no close frame.
        if not self._accepted:
            await self._consume_connect()
            if self._disconnected:
                self._closed = True
                return
        self._closed = True
        event: WebSocketCloseEvent = {'type': ASGIEvent.WS_CLOSE, 'code': code}
        if reason is not None:
            event['reason'] = reason
        await self._send(event)
        self._close_code = code
        self._close_reason = reason

    # ---- sending ---------------------------------------------------------

    def _check_sendable(self) -> None:
        if not self._accepted:
            raise RuntimeError(
                'WebSocket is not accepted yet — await ws.accept() before '
                'sending (or await ws.close() to reject the connection).')
        if self._disconnected:
            raise WebSocketDisconnect(self._close_code or _NO_STATUS,
                                      self._close_reason)
        if self._closed:
            raise RuntimeError('WebSocket.send_*() called after close()')

    async def send_text(self, data: str) -> None:
        """Send one complete text message."""
        self._check_sendable()
        event: WebSocketSendEvent = {'type': ASGIEvent.WS_SEND, 'text': data}
        await self._send(event)

    async def send_bytes(self, data: bytes) -> None:
        """Send one complete binary message."""
        self._check_sendable()
        event: WebSocketSendEvent = {'type': ASGIEvent.WS_SEND, 'bytes': data}
        await self._send(event)

    async def send_json(self, data: Any, *, binary: bool = False) -> None:
        """JSON-serialise *data* and send it as one message.

        Text by default, which is what browsers and most clients expect; pass
        ``binary=True`` to send the UTF-8 encoding as a binary frame instead.
        """
        payload = json.dumps(data)
        if binary:
            await self.send_bytes(payload.encode())
        else:
            await self.send_text(payload)

    async def send(self, data: str | bytes) -> None:
        """Send *data* as text or binary, chosen by its Python type."""
        if isinstance(data, str):
            await self.send_text(data)
        elif isinstance(data, (bytes, bytearray, memoryview)):
            await self.send_bytes(bytes(data))
        else:
            raise TypeError(
                f'WebSocket.send() takes str or bytes, not {type(data).__name__} '
                f'— use send_json() for structured data.')

    # ---- receiving -------------------------------------------------------

    def _note_disconnect(self, event: dict) -> None:
        self._disconnected = True
        self._close_code = event.get('code', _NO_STATUS)
        self._close_reason = event.get('reason')

    async def receive(self) -> str | bytes:
        """Await one complete message — ``str`` for text, ``bytes`` for binary.

        Fragmented messages have already been reassembled, so what comes back
        is always a whole application message.

        Raises :class:`WebSocketDisconnect` when the peer closes.  Prefer
        ``async for`` unless you need the close code.
        """
        if self._disconnected:
            raise WebSocketDisconnect(self._close_code or _NO_STATUS,
                                      self._close_reason)
        if not self._accepted:
            raise RuntimeError(
                'WebSocket is not accepted yet — await ws.accept() before '
                'receiving.')
        while True:
            event = await self._receive()
            etype = event.get('type')
            if etype == ASGIEvent.WS_RECEIVE:
                text = event.get('text')
                if text is not None:
                    return text
                data = event.get('bytes')
                if data is not None:
                    return data
                # Neither key set: a zero-length message.  The recipient
                # always sets both keys with one of them None, so this is a
                # degenerate frame rather than a protocol violation — report
                # it as the empty text message it is.
                return ''
            if etype == ASGIEvent.WS_DISCONNECT:
                self._note_disconnect(event)
                raise WebSocketDisconnect(self._close_code or _NO_STATUS,
                                          self._close_reason)
            # websocket.connect can only appear first and _consume_connect
            # has already taken it; anything else is not ours to interpret.
            logger.debug('WebSocket.receive: ignoring unexpected event %r', etype)

    async def receive_text(self) -> str:
        """Await one message, requiring it to be text."""
        message = await self.receive()
        if not isinstance(message, str):
            raise TypeError(
                f'expected a text message, got {len(message)} bytes of binary '
                f'— use receive() to accept either.')
        return message

    async def receive_bytes(self) -> bytes:
        """Await one message, requiring it to be binary."""
        message = await self.receive()
        if not isinstance(message, bytes):
            raise TypeError(
                'expected a binary message, got text '
                '— use receive() to accept either.')
        return message

    async def receive_json(self) -> Any:
        """Await one message and parse it as JSON (text or binary)."""
        message = await self.receive()
        if isinstance(message, bytes):
            message = message.decode()
        return json.loads(message)

    def __aiter__(self) -> AsyncIterator[str | bytes]:
        """Iterate messages until the peer disconnects.

        The disconnect ends the loop rather than raising, so the common shape
        is just ``async for message in ws:``.
        """
        return self._iter_messages()

    async def _iter_messages(self) -> AsyncIterator[str | bytes]:
        while True:
            try:
                message = await self.receive()
            except WebSocketDisconnect:
                return
            yield message
