"""Raw protocol Actor — Sprint 50 (Non-ASGI bridge).

:class:`RawProtocolActor` is the Layer-2 Actor for non-HTTP protocols.  It wraps
a :data:`RawProtocolHandler` with connection-lifetime timing, error isolation,
and the ``connection_closed`` lifecycle event — the generic 80%-case host for a
protocol that speaks the wire directly (raw TCP here; MQTT in Sprint 51).

The handler owns the connection for its whole lifetime: it decides when to
close.  This is deliberately unlike HTTP's request/response-per-connection
model — raw protocols are typically long-lived and stateful.
"""
import logging
import time

from ..actor import Actor, Message
from .protocol_registry import ProtocolContext, RawProtocolHandler
from .recipient import AbstractReader
from .sender import AbstractWriter

logger = logging.getLogger(__name__)


class RawProtocolActor(Actor):
    """One per accepted non-HTTP connection.

    Error contract (design §7.4): if the handler raises, the exception is
    emitted via ``aggregator.on_error`` and swallowed here — ``ConnectionActor``
    has its own ``on_error`` path, so re-raising would double-emit.  Either way
    the connection is closed by ``ConnectionActor``'s ``finally`` and
    ``connection_closed`` fires from this Actor's ``finally``.
    """

    def __init__(
        self,
        reader: AbstractReader,
        writer: AbstractWriter,
        handler: RawProtocolHandler,
        ctx: ProtocolContext,
    ) -> None:
        super().__init__()
        self._reader = reader
        self._writer = writer
        self._handler = handler
        self._ctx = ctx

    async def run(self) -> None:
        start = time.monotonic()
        try:
            await self._handler(self._reader, self._writer, self._ctx)
        except Exception as exc:
            logger.debug('raw protocol %r handler raised: %r',
                         self._ctx.protocol, exc)
            if self._ctx.aggregator is not None:
                await self._ctx.aggregator.on_error({}, exc)
        finally:
            elapsed_ms = (time.monotonic() - start) * 1000
            if self._ctx.aggregator is not None:
                await self._ctx.aggregator.on_connection_closed(
                    self._ctx.peername, self._ctx.protocol, elapsed_ms)

    async def _handle(self, msg: Message) -> None:  # never reached — run() overridden
        raise NotImplementedError
