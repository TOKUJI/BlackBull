"""Owns WebSocket handshake decisions and the frame loop."""
from collections.abc import Awaitable, Callable

from ..actor import Actor, Message
from ..native import NativeResponse, NativeWSMessage, report_dict, warns_app_dicts
from ..connection import Connection
from ..event_aggregator import EventAggregator
from ..asgi import (ASGIEvent, WebSocketAcceptEvent, WebSocketCloseEvent,
                    WebSocketSendEvent)
from .conn_id import new_connection_id
from .constants import WSCloseCode
from ..headers import _MinimalResponseHeaders, _as_response_fields
from ..request import ClientDisconnected
from .permessage_deflate import (
    DeflateParams, InboundDecompressor, OutboundCompressor,
)
from .recipient import AbstractReader, RecipientFactory, _WS_READ_INLINE
from .sender import AbstractWriter, SenderFactory


_DISCONNECT_HOOK_TIMEOUT = 5.0


# Application accept headers must not replace handshake-owned fields.
_HANDSHAKE_OWNED = frozenset({
    b'upgrade', b'connection', b'sec-websocket-accept',
    b'sec-websocket-extensions', b'sec-websocket-protocol',
})


def _app_accept_headers(raw) -> _MinimalResponseHeaders | None:
    """Check the accept event's extra headers; order and multiplicity stay.

    A name the handshake owns, or a field that would break the response
    (BLA-342's grammar), raises rather than being dropped.
    """
    if not raw:
        return None
    out = _as_response_fields((name, value) for name, value in raw)
    for name, _ in out:
        if name in _HANDSHAKE_OWNED:
            raise ValueError(
                f'{name!r} is owned by the WebSocket handshake and cannot '
                f'be set from websocket.accept headers')
    return out


class WebSocketActor(Actor):
    """Owns one WebSocket connection after the HTTP upgrade handshake.

    FragmentAssembler runs inside WebSocketRecipient; the ASGI app always
    receives complete messages.

    Supervisor strategy: isolate — exceptions from the app or protocol
    call on_error; on_websocket_disconnected always fires in finally.

    websocket_connected fires after the app sends websocket.accept (matching
    the ASGI semantic: the connection is "established" from the app's view).
    websocket_disconnected fires in finally with the last disconnect code seen.
    """

    def __init__(
        self,
        reader: AbstractReader,
        writer: AbstractWriter,
        conn: Connection,
        app: Callable[..., Awaitable[None]],
        aggregator: EventAggregator,
        *,
        ws_queue_depth: int = _WS_READ_INLINE,
        http_send: Callable[[NativeResponse], Awaitable[None]] | None = None,
        abort_denial: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        super().__init__()
        self._writer = writer
        self._conn = conn
        self._app = app
        self._warn_dicts = warns_app_dicts(app)
        self._aggregator = aggregator
        self._http_send = http_send
        self._abort_denial = abort_denial
        self._accepted = False
        self.denial_status: int | None = None
        self._denied = False
        # permessage-deflate (RFC 7692) — when the handshake negotiated it,
        # the Connection's WS bag carries a [`DeflateParams`][].  Instantiate
        # the streaming inflater + deflater so the recipient/sender don't need
        # to know about negotiation logic.
        ws_bag = conn._ws or {}
        deflate: DeflateParams | None = ws_bag.get('deflate')
        decompressor = (
            InboundDecompressor(
                wbits=deflate.client_max_window_bits,
                reset_per_message=deflate.client_no_context_takeover,
            ) if deflate else None
        )
        compressor = (
            OutboundCompressor(
                wbits=deflate.server_max_window_bits,
                reset_per_message=deflate.server_no_context_takeover,
            ) if deflate else None
        )
        self._ws_receive = RecipientFactory.websocket(
            reader, writer,
            # Only the cap-hit log reads this on the actor path (every other
            # use is gated on a dispatcher, which this path does not pass) —
            # without it a WS limit reports with no path, which is the one
            # field an operator needs to act on it.
            conn=conn,
            ws_queue_depth=ws_queue_depth,
            decompressor=decompressor,
            on_message=self._emit_websocket_message,
            read_ahead_needed=self._aggregator.has_websocket_message_listeners,
        )
        self._ws_send = SenderFactory.websocket(writer, compressor=compressor)

    @property
    def _disconnect_code(self) -> int:
        return self._ws_receive.terminal_code or WSCloseCode.ABNORMAL

    async def _emit_websocket_message(self, message: str | bytes) -> None:
        """Emit at recipient read time, before the handler consumes the message.

        Re-check listeners for each message so registration after connection setup
        still takes effect.
        """
        if not self._aggregator.has_websocket_message_listeners():
            return
        await self._aggregator.on_websocket_message(self._conn, message)

    async def run(self) -> None:
        try:
            try:
                await self._app(self._conn, self._ws_receive, self._send)
                if self.denial_status is not None and not self._denied:
                    raise RuntimeError('WebSocket application left the HTTP denial incomplete')
            except ClientDisconnected:
                return
            except Exception as exc:
                await self._aggregator.on_error(self._conn, exc)
            if not self._accepted:
                await self._refuse_unanswered()
        finally:
            # Cancel the reader before closing: a full queue can leave it
            # blocked in queue.put(), which EOF cannot wake.
            try:
                await self._aggregator.on_websocket_disconnected(
                    self._conn, code=self._disconnect_code,
                    timeout=_DISCONNECT_HOOK_TIMEOUT)
            finally:
                try:
                    await self._ws_receive.shutdown()
                finally:
                    if not self._denied:
                        if self.denial_status is not None and self._abort_denial:
                            await self._abort_denial()
                        else:
                            await self._writer.close()

    async def _refuse_unanswered(self) -> None:
        if self._http_send is None or self._accepted or self.denial_status is not None:
            return
        try:
            await self._send_denial(NativeResponse.complete(403, [], b''))
        except ClientDisconnected:
            pass

    async def _send_denial(self, event: NativeResponse) -> None:
        if self._http_send is None or self._accepted or self._denied:
            raise RuntimeError('HTTP denial is only available before WebSocket acceptance')
        if event._extension is not None or event.expects_trailers or event._trailers is not None:
            raise ValueError('WebSocket denial supports response headers and body only')
        if event._header is not None:
            if self.denial_status is not None or not 300 <= event.status <= 599:
                raise ValueError('expected one HTTP denial status between 300 and 599')
        elif self.denial_status is None or event.body is None:
            raise RuntimeError('expected HTTP denial headers before the body')
        await self._http_send(event)
        if event._header is not None:
            self.denial_status = event.status
        if event.body is not None and not event.more_body:
            await self._writer.close()
            self._denied = True

    async def _send(self,
                    event: (WebSocketSendEvent | WebSocketCloseEvent
                            | WebSocketAcceptEvent | NativeWSMessage | NativeResponse),
                    _status=None, _headers=None) -> None:
        if isinstance(event, NativeWSMessage):
            kind = event.kind
            is_accept = kind == NativeWSMessage.ACCEPT
        elif isinstance(event, dict):
            if self._warn_dicts:
                report_dict(event)
            kind = event.get('type')
            is_accept = kind == ASGIEvent.WS_ACCEPT
        else:
            await self._send_denial(event)
            return
        if is_accept:
            if self._accepted:
                raise RuntimeError('WebSocket already accepted')
            if self.denial_status is not None:
                raise RuntimeError('WebSocket message after HTTP denial')
            if isinstance(event, NativeWSMessage):
                offered, extra = event.subprotocol, event.headers
            else:
                offered, extra = event.get('subprotocol'), event.get('headers')
            if offered is not None:
                protocol = offered.decode('utf-8') if isinstance(offered, bytes) else offered
                if protocol not in self._conn.subprotocols:
                    raise ValueError('selected a WebSocket subprotocol the client did not offer')
            # Checked before the response goes out: a refused header must
            # fail the accept, not arrive silently shortened.
            app_headers = _app_accept_headers(extra)
            ws_bag = self._conn._ws or {}
            send_101 = ws_bag.pop('send_101', None)
            if send_101:
                subprotocol = offered or ws_bag.pop('auto_subprotocol', None)
                await send_101(subprotocol, app_headers)
            self._accepted = True
            if not self._conn.connection_id:
                # Normally set by the HTTP actor's upgrade path from the
                # accept-time id; mint one only for direct test drives.
                self._conn.connection_id = new_connection_id()
            await self._aggregator.on_websocket_connected(self._conn, offered)
        elif not self._accepted and self._http_send is not None:
            if self.denial_status is not None:
                raise RuntimeError('WebSocket message after HTTP denial')
            if kind in (NativeWSMessage.CLOSE, ASGIEvent.WS_CLOSE):
                await self._refuse_unanswered()
                return
            raise RuntimeError('WebSocket send before acceptance')
        else:
            await self._ws_send(event)
        self._ws_receive.send_touch()

    async def _handle(self, msg: Message) -> None:
        raise NotImplementedError
