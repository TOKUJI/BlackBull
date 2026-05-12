"""WebSocket Actor — Phase 6 Step 5."""
import logging
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import uuid4

from ..actor import Actor, Message
from ..event_aggregator import EventAggregator
from .constants import ASGIEvent, WSCloseCode
from .recipient import AbstractReader, RecipientFactory
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
        scope: dict[str, Any],
        app: Callable[..., Awaitable[None]],
        aggregator: EventAggregator,
        *,
        peername: tuple[str, int] | None = None,
        sockname: tuple[str, int] | None = None,
        ssl: bool = False,
    ) -> None:
        super().__init__()
        self._reader = reader
        self._writer = writer
        self._scope = scope
        self._app = app
        self._aggregator = aggregator
        self._peername = peername
        self._sockname = sockname
        self._ssl = ssl
        self._ws_receive = RecipientFactory.websocket(reader, writer)
        self._ws_send = SenderFactory.websocket(writer)
        self._disconnect_code: int = WSCloseCode.ABNORMAL

    async def run(self) -> None:
        try:
            await self._app(self._scope, self._receive, self._send)
        except BaseException as exc:
            await self._aggregator.on_error(self._scope, exc)
        finally:
            await self._aggregator.on_websocket_disconnected(
                self._scope, code=self._disconnect_code)
            await self._writer.close()

    async def _receive(self) -> dict[str, Any]:
        event = await self._ws_receive()
        if event.get('type') == ASGIEvent.WS_RECEIVE:
            await self._aggregator.on_websocket_message(self._scope, event)
        elif event.get('type') == ASGIEvent.WS_DISCONNECT:
            self._disconnect_code = event.get('code', WSCloseCode.ABNORMAL)
        return event

    async def _send(self, event: dict[str, Any], _status=None, _headers=None) -> None:
        if isinstance(event, dict) and event.get('type') == ASGIEvent.WS_ACCEPT:
            send_101 = self._scope.pop('_ws_send_101', None)
            if send_101:
                subprotocol = (event.get('subprotocol')
                               or self._scope.pop('_ws_auto_subprotocol', None))
                await send_101(subprotocol)
            if not self._scope.get('_connection_id'):
                self._scope['_connection_id'] = str(uuid4())
            await self._aggregator.on_websocket_connected(
                self._scope, event.get('subprotocol'))
        await self._ws_send(event)

    async def _handle(self, msg: Message) -> None:
        raise NotImplementedError
