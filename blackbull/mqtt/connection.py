"""MQTT 5.0 per-connection actor and its raw-protocol entry point.

:class:`MQTTConnectionActor` is one per connection. Its **inbox carries only
outbound packets** (:class:`~blackbull.mqtt.broker.Send` /
:class:`~blackbull.mqtt.broker.Close` from the broker, plus the two stateless
replies it generates itself), and its ``run()`` — draining that inbox — is the
*sole writer* to the socket, so there are no cross-task write races.  A sibling
reader loop decodes the wire (via :class:`PacketFramer`) and ``send``s control
messages to the broker.  :func:`serve_connection` is the
:data:`~blackbull.server.protocol_registry.RawProtocolHandler` body that wires
the two together.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Iterator

from ..actor import Actor, Message as ActorMessage
from ..server.protocol_registry import ProtocolContext
from ..server.recipient import AbstractReader
from ..server.sender import AbstractWriter
from .broker import (
    BrokerActor, Attach, ClientSubscribe, ClientUnsubscribe, ClientPublish,
    ClientPuback, ClientPubrec, ClientPubrel, ClientPubcomp, Detach, Send, Close,
)
from .messages import (
    MQTTConnect, MQTTPublish, MQTTPuback, MQTTPubrec, MQTTPubrel, MQTTPubcomp,
    MQTTSubscribe, MQTTUnsubscribe, MQTTPingreq, MQTTPingresp,
    MQTTDisconnect, MQTTAuth, MQTTMessage,
    IncompletePacket, MQTTDecodeError,
    decode_packet, encode_packet,
)
from .tap import Message, TapActor, compile_taps, run_taps

logger = logging.getLogger(__name__)

_RC_SUCCESS = 0x00
_READ_CHUNK = 4096
_IDLE_SLEEP = 0.005


class PacketFramer:
    """Incremental MQTT packet de-framer.

    Fed raw bytes with :meth:`feed`, it yields each fully decoded packet on
    iteration and retains any trailing partial packet for the next feed.  The
    framing/resync state machine lives here rather than inline in the read loop:

    * an **incomplete** packet simply ends the current iteration — the partial
      bytes stay buffered for the next :meth:`feed` (TCP will deliver the rest),
    * a **hard decode error** (reserved flag bits, unknown type — the junk a
      desynchronised stream produces) drops one byte and resyncs.

    This replaces Sprint 53's ``stalled_len`` bookkeeping.  The ``bytes(...)``
    snapshot at the decode boundary stays because the codec's input contract is
    deliberately ``bytes``; a zero-copy framer would mean widening that contract
    to the buffer protocol (deferred).
    """

    def __init__(self) -> None:
        self._buffer = bytearray()

    def feed(self, data: bytes) -> None:
        self._buffer += data

    def __iter__(self) -> Iterator[MQTTMessage]:
        buffer = self._buffer
        while buffer:
            try:
                message = decode_packet(bytes(buffer))
            except IncompletePacket:
                return  # need more bytes; keep the partial packet buffered
            except (MQTTDecodeError, ValueError):
                del buffer[0]  # junk byte — resync by dropping it
                continue
            del buffer[:message[1]]  # message[1] == bytes consumed
            yield message


class MQTTConnectionActor(Actor):
    """One per MQTT connection; the sole writer to its socket.

    Tap dispatch is selected at construction: pass a running :class:`TapActor`
    as *tap* for decoupled (actor-mode) dispatch, or *app_handlers* for inline
    dispatch on this connection (the Sprint 53 contract).
    """

    def __init__(self, writer: AbstractWriter, broker: BrokerActor,
                 ctx: ProtocolContext, *, app_handlers=None,
                 tap: TapActor | None = None) -> None:
        super().__init__()
        self._writer = writer
        self._broker = broker
        self._ctx = ctx
        # Tap dispatch: a TapActor (decoupled) takes precedence; otherwise the
        # compiled inline handlers run sequentially on this connection.
        self._tap = tap
        self._inline_taps = compile_taps(app_handlers) if tap is None else []
        self._done = False
        self.graceful = False

    # -- inbox drain (the only writer) --------------------------------------

    async def _handle(self, msg: ActorMessage) -> None:
        if isinstance(msg, Send):
            try:
                await self._writer.write(encode_packet(msg.packet))
            except Exception:  # pragma: no cover - peer vanished mid-write
                logger.debug('MQTT write failed', exc_info=True)
                self._done = True
        elif isinstance(msg, Close):
            self._done = True

    # -- reader task --------------------------------------------------------

    async def read_loop(self, reader: AbstractReader) -> None:
        """Decode the wire and forward control packets to the broker.

        Reads block on a real socket; a fake reader may return ``b''`` while
        merely idle, in which case we poll.  Either way the loop ends on EOF
        (``at_eof()``), a DISCONNECT (sets ``_done``), or task cancellation.
        """
        framer = PacketFramer()
        while not self._done:
            for message in framer:
                await self._forward(message)
                if self._done:
                    return
            data = await reader.read(_READ_CHUNK)
            if data:
                framer.feed(data)
            elif reader.at_eof():
                break  # peer closed the connection (EOF)
            else:
                await asyncio.sleep(_IDLE_SLEEP)

    async def _forward(self, message: MQTTMessage) -> None:
        broker = self._broker
        if isinstance(message, MQTTConnect):
            await broker.send(Attach(connect=message, sender=self))
        elif isinstance(message, MQTTPublish):
            await broker.send(ClientPublish(publish=message, sender=self))
            await self._dispatch_taps(message)
        elif isinstance(message, MQTTSubscribe):
            await broker.send(ClientSubscribe(subscribe=message, sender=self))
        elif isinstance(message, MQTTUnsubscribe):
            await broker.send(ClientUnsubscribe(unsubscribe=message, sender=self))
        elif isinstance(message, MQTTPuback):
            await broker.send(ClientPuback(packet_id=message.packet_id, sender=self))
        elif isinstance(message, MQTTPubrec):
            await broker.send(ClientPubrec(packet_id=message.packet_id, sender=self))
        elif isinstance(message, MQTTPubrel):
            await broker.send(ClientPubrel(packet_id=message.packet_id, sender=self))
        elif isinstance(message, MQTTPubcomp):
            await broker.send(ClientPubcomp(packet_id=message.packet_id, sender=self))
        elif isinstance(message, MQTTPingreq):
            # Stateless: reply through our own inbox so run() stays the only writer.
            await self.send(Send(packet=MQTTPingresp()))
        elif isinstance(message, MQTTAuth):
            await self.send(Send(packet=MQTTAuth(reason_code=_RC_SUCCESS)))
        elif isinstance(message, MQTTDisconnect):
            # 0x04 = "Disconnect with Will Message"; anything else is graceful.
            self.graceful = message.reason_code != 0x04
            await broker.send(Detach(graceful=self.graceful, sender=self))
            self._done = True
        else:  # pragma: no cover - decode_packet yields only known types
            logger.debug('MQTT connection ignoring %s', type(message).__name__)

    async def _dispatch_taps(self, publish: MQTTPublish) -> None:
        """Route an inbound PUBLISH to the application taps.

        In actor mode the :class:`Message` is *offered* to the shared
        :class:`TapActor` and we return at once (a slow tap never back-pressures
        this connection or the broker).  In inline mode the matching callbacks
        run here, sequentially, with isolated exceptions (the Sprint 53 contract).
        """
        if self._tap is None and not self._inline_taps:
            return
        message = Message(topic=publish.topic, payload=publish.payload,
                          qos=publish.qos, retain=publish.retain,
                          properties=dict(publish.properties))
        if self._tap is not None:
            self._tap.offer(message)
        else:
            await run_taps(self._inline_taps, message)


async def serve_connection(reader: AbstractReader, writer: AbstractWriter,
                           ctx: ProtocolContext, broker: BrokerActor,
                           *, app_handlers=None, tap: TapActor | None = None) -> None:
    """Raw-protocol handler body for one MQTT connection.

    Spawns the connection actor's inbox-drain (`run`) alongside the reader loop,
    and guarantees the broker sees a ``Detach`` when the connection ends — so a
    Will fires on an abnormal (cancelled) close.  Pass *tap* for decoupled tap
    dispatch or *app_handlers* for inline dispatch (see :mod:`blackbull.mqtt.tap`).
    """
    conn = MQTTConnectionActor(writer, broker, ctx,
                               app_handlers=app_handlers, tap=tap)
    writer_task = asyncio.create_task(conn.run())
    graceful = False
    try:
        await conn.read_loop(reader)
        graceful = True
    finally:
        # Synchronous enqueue: safe even while this task is being cancelled.
        # Idempotent — a graceful DISCONNECT already sent its own Detach, after
        # which the broker no longer knows this connection.
        broker._inbox.put_nowait(Detach(graceful=conn.graceful, sender=conn))
        # On a clean close, flush every reply the broker still owes this
        # connection before stopping its writer (an abrupt cancel skips this —
        # the peer is already gone).
        if graceful:
            await _flush_pending(broker, conn)
        writer_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await writer_task


async def _flush_pending(broker, conn, *, max_turns: int = 1000) -> None:
    """Yield until the broker has drained its inbox (FIFO ⇒ every reply this
    connection is owed is enqueued) and the connection's writer has drained its
    own inbox.  Requires two consecutive idle turns so a reply enqueued while the
    broker was mid-handle is not missed."""
    idle = 0
    for _ in range(max_turns):
        await asyncio.sleep(0)
        if broker._inbox.empty() and conn._inbox.empty():
            idle += 1
            if idle >= 2:
                return
        else:
            idle = 0
