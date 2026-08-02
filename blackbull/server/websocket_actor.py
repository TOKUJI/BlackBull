"""WebSocket Actor — owns one upgraded connection's frame loop."""
import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from ..actor import Actor, Message
from ..connection import Connection
from ..event_aggregator import EventAggregator
from ..asgi import (ASGIEvent, WebSocketAcceptEvent, WebSocketCloseEvent,
                    WebSocketSendEvent)
from .conn_id import new_connection_id
from .constants import WSCloseCode
from .permessage_deflate import (
    DeflateParams, InboundDecompressor, OutboundCompressor,
)
from .recipient import AbstractReader, RecipientFactory, _WS_READ_INLINE
from .sender import AbstractWriter, SenderFactory

logger = logging.getLogger(__name__)


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
        peername: tuple[str, int] | None = None,
        sockname: tuple[str, int] | None = None,
        ssl: bool = False,
        ws_queue_depth: int = _WS_READ_INLINE,
    ) -> None:
        super().__init__()
        self._reader = reader
        self._writer = writer
        self._conn = conn
        self._app = app
        self._aggregator = aggregator
        self._peername = peername
        self._sockname = sockname
        self._ssl = ssl
        # permessage-deflate (RFC 7692) — when the handshake negotiated it,
        # the Connection's WS bag carries a :class:`DeflateParams`.  Instantiate
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
            ws_queue_depth=ws_queue_depth,
            decompressor=decompressor,
            on_message=self._emit_websocket_message,
            read_ahead_needed=self._aggregator.has_websocket_message_listeners,
        )
        self._ws_send = SenderFactory.websocket(writer, compressor=compressor)
        self._disconnect_code: int = WSCloseCode.ABNORMAL

    async def _emit_websocket_message(self, event: dict) -> None:
        """Read-time emit adapter: ``websocket_message`` fires when the
        recipient reads a message, before the handler consumes it.

        Re-checks the listener set per message (cached predicate) so a
        listener registered after this connection was built still receives
        events, while a no-listener throughput workload pays one boolean
        check instead of the whole ``Event``/``emit`` chain.
        """
        if not self._aggregator.has_websocket_message_listeners():
            return
        await self._aggregator.on_websocket_message(self._conn, event)

    async def run(self) -> None:
        try:
            await self._app(self._conn, self._receive, self._send)
        except asyncio.CancelledError:
            # Cancellation is not an error: re-raise so the task actually
            # cancels rather than completing normally.  (Mirrors HTTP1Actor;
            # the finally below still runs the disconnect/close cleanup.)
            raise
        except Exception as exc:
            await self._aggregator.on_error(self._conn, exc)
        finally:
            await self._aggregator.on_websocket_disconnected(
                self._conn, code=self._disconnect_code)
            self._ws_receive.disarm_watchdog()
            await self._writer.close()

    async def _receive(self) -> dict[str, Any]:
        event = await self._ws_receive()
        if event.get('type') == ASGIEvent.WS_DISCONNECT:
            self._disconnect_code = event.get('code', WSCloseCode.ABNORMAL)
        return event

    async def _send(self,
                    event: WebSocketSendEvent | WebSocketCloseEvent | WebSocketAcceptEvent,
                    _status=None, _headers=None) -> None:
        if isinstance(event, dict) and event.get('type') == ASGIEvent.WS_ACCEPT:
            ws_bag = self._conn._ws or {}
            send_101 = ws_bag.pop('send_101', None)
            if send_101:
                subprotocol = (event.get('subprotocol')
                               or ws_bag.pop('auto_subprotocol', None))
                await send_101(subprotocol)
            if not self._conn.connection_id:
                # Normally set by the HTTP actor's upgrade path from the
                # accept-time id; mint one only for direct test drives.
                self._conn.connection_id = new_connection_id()
            await self._aggregator.on_websocket_connected(
                self._conn, event.get('subprotocol'))
        await self._ws_send(event)
        # Control-frame watchdog (design A'): service PING/CLOSE frames that
        # arrived while the handler worked, without blocking the send path
        # (buffered-bytes only, and only when a control frame actually leads
        # the buffer — with data frames buffered, the app/reader owns them).
        # On a connection where a reader task owns the wire this is a no-op
        # — the reader services control frames.
        if self._ws_receive.has_control_frames_buffered():
            await self._ws_receive.service_available_control_frames()
        self._ws_receive.touch()

    async def _handle(self, msg: Message) -> None:
        raise NotImplementedError
