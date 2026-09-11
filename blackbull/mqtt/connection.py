"""MQTT 5.0 per-connection actor and its raw-protocol entry point.

:class:`MQTT5Actor` is one per connection. Its **inbox carries only
outbound packets** (:class:`~blackbull.mqtt.broker.Send` from the broker, plus
local transport refusals). ``Close`` sets terminal state
without needing a queue slot. Its ``run()`` — draining that inbox — is the
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
    ClientPuback, ClientPubrec, ClientPubrel, ClientPubcomp, ClientPing, ClientAuth,
    ClientProtocolError, Detach, Send, Close,
)
from .messages import (
    MQTTConnect, MQTTPublish, MQTTPuback, MQTTPubrec, MQTTPubrel, MQTTPubcomp,
    MQTTSubscribe, MQTTUnsubscribe, MQTTPingreq,
    MQTTDisconnect, MQTTAuth, MQTTMessage,
    IncompletePacket, MQTTDecodeError, ReasonCode,
    decode_packet, decode_variable_byte_integer, encode_packet,
)
from ..server.cap_log import log_cap_hit
from .mailbox import Mailbox, MailboxClosed, MailboxTooLarge
from .tap import Message, TapActor, compile_taps, run_taps

logger = logging.getLogger(__name__)

_READ_CHUNK = 4096
_IDLE_SLEEP = 0.005


def _output_size(msg: ActorMessage) -> int:
    if isinstance(msg, Send):
        if msg._encoded is None:
            msg._encoded = encode_packet(msg.packet)
        return len(msg._encoded)
    return 1


class PacketTooLarge(Exception):
    """A packet declared more bytes than ``BB_MQTT_MAX_PACKET_SIZE`` allows.

    Carries the declared size rather than a buffered one: the whole point
    of the check is that the payload is refused on the strength of the
    fixed header, so there is nothing buffered to report.
    """

    def __init__(self, declared: int, maximum: int):
        super().__init__(
            f'packet declares {declared} bytes, over the maximum {maximum}')
        self.declared = declared
        self.maximum = maximum


class PacketFramer:
    """Incremental MQTT packet de-framer.

    Fed raw bytes with :meth:`feed`, it yields each fully decoded packet on
    iteration and retains any trailing partial packet for the next feed.  The
    framing/resync state machine lives here rather than inline in the read loop:

    * an **incomplete** packet simply ends the current iteration — the partial
      bytes stay buffered for the next :meth:`feed` (TCP will deliver the rest),
    * a **hard decode error** (reserved flag bits, unknown type — the junk a
      desynchronised stream produces) resyncs to the next plausible header,
    * a packet whose **declared** size exceeds *max_packet_size* raises
      :class:`PacketTooLarge` from the fixed header, before its body is
      waited for.  MQTT 5 lets a peer declare 268,435,455 bytes and then
      dribble them; a framer that judged the packet only once it was
      complete would already have paid for the attack.

    The ``bytes(...)`` snapshot at the decode boundary is the price of the
    codec taking ``bytes``: going zero-copy would widen that contract to the
    buffer protocol.
    """

    def __init__(self, max_packet_size: int = 0) -> None:
        self._buffer = bytearray()
        self._max_packet_size = max_packet_size
        # True while the buffer is known to start at a packet boundary: a
        # fresh connection does, and so does every position just after a
        # successful decode.  A resync leaves it False until the next packet
        # decodes, because from there the start is a guess — and the size
        # gate's answer is too final to spend on a guess.
        self._synced = True

    @property
    def buffered(self) -> bytearray:
        """Bytes held for the next feed — the thing a size bound must keep small."""
        return self._buffer

    def feed(self, data: bytes) -> None:
        self._buffer += data

    def _check_declared_size(self, buffer: bytearray) -> None:
        """Refuse an over-size packet from its fixed header alone.

        Silent when the header is not yet readable or is malformed: this
        is a size gate, not a validator.  Whatever is wrong with those
        bytes is the decoder's verdict to give, one step later.

        **Only applied when the framer is synchronised.**  The gate's
        answer is fatal — ``DISCONNECT 0x95`` and close — so it must only
        judge bytes that really are a packet header.  After a resync the
        buffer starts at a *guess*, and junk read as a Remaining Length
        decodes to something enormous about as often as not; answering
        that would turn a desync the framer can recover from into a
        connection the peer cannot re-establish its way out of.  A fresh
        connection begins at a boundary, and every successful decode ends
        at one; only those positions are trusted.

        Also silent for a reserved control-packet type of 0 (§2.1.2),
        which cannot begin a packet at any position.
        """
        if not self._max_packet_size or not self._synced:
            return
        if not buffer[0] >> 4:
            return
        try:
            remaining_length, rl_consumed = decode_variable_byte_integer(
                bytes(buffer[1:5]))
        except (IncompletePacket, MQTTDecodeError, ValueError):
            return
        declared = 1 + rl_consumed + remaining_length
        if declared > self._max_packet_size:
            log_cap_hit('mqtt_max_packet_size',
                        requested=declared,
                        limit=self._max_packet_size,
                        protocol='mqtt')
            raise PacketTooLarge(declared, self._max_packet_size)

    def __iter__(self) -> Iterator[MQTTMessage]:
        buffer = self._buffer
        while buffer:
            self._check_declared_size(buffer)
            try:
                message = decode_packet(bytes(buffer))
            except IncompletePacket:
                return  # need more bytes; keep the partial packet buffered
            except (MQTTDecodeError, ValueError):
                self._synced = False
                self._resync(buffer)
                continue
            del buffer[:message[1]]  # message[1] == bytes consumed
            self._synced = True      # this position is a real boundary again
            yield message

    @staticmethod
    def _resync(buffer: bytearray) -> None:
        """Drop to the next byte that could plausibly start a packet.

        Dropping one byte and re-decoding from the start is quadratic in
        the length of a junk run, and a junk run is something a peer
        chooses.  A control-packet type of 0 is reserved (§2.1.2), so any
        byte with a zero high nibble cannot begin a packet and can be
        skipped without a decode attempt.  Everything past that first
        plausible byte is still the decoder's problem — this only
        declines to *ask* about bytes that cannot possibly be an answer.
        """
        for offset in range(1, len(buffer)):
            if buffer[offset] >> 4:
                del buffer[:offset]
                return
        buffer.clear()


class MQTT5Actor(Actor):
    """One per MQTT 5.0 connection; the sole writer to its socket.

    Tap dispatch is selected at construction: pass a running :class:`TapActor`
    as *tap* for decoupled (actor-mode) dispatch, or *app_handlers* for inline
    dispatch on this connection.
    """

    def __init__(self, writer: AbstractWriter, broker: BrokerActor,
                 ctx: ProtocolContext, *, app_handlers=None,
                 tap: TapActor | None = None,
                 max_packet_size: int | None = None,
                 inbox_maxsize: int | None = None,
                 inbox_max_bytes: int | None = None) -> None:
        super().__init__()
        self._writer = writer
        from ..env import get_settings  # noqa: PLC0415
        settings = get_settings()
        if max_packet_size is None:
            max_packet_size = settings.mqtt_max_packet_size
        self._mailbox = Mailbox(
            settings.mqtt_connection_inbox_maxsize if inbox_maxsize is None else inbox_maxsize,
            settings.mqtt_connection_inbox_max_bytes if inbox_max_bytes is None else inbox_max_bytes,
            _output_size)
        self._write_timeout = settings.write_timeout
        self._writer_task: asyncio.Task | None = None
        self._close_requested: asyncio.Future[None] | None = None
        self._aborted = False
        self._max_packet_size = max_packet_size
        self._broker = broker
        self._ctx = ctx
        # Tap dispatch: a TapActor (decoupled) takes precedence; otherwise the
        # compiled inline handlers run sequentially on this connection.
        self._tap = tap
        self._inline_taps = compile_taps(app_handlers) if tap is None else []
        self._done = False
        self.graceful = False
        # §3.1.2.10 — negotiated Keep Alive (seconds; 0 disables the check),
        # learned from the client's CONNECT.  ``_last_rx`` is the monotonic time
        # of the last received packet, used to enforce the 1.5× idle deadline.
        self._keep_alive = 0
        self._last_rx = 0.0

    # -- inbox drain (the only writer) --------------------------------------

    @property
    def _inbox(self) -> Mailbox:
        return self._mailbox

    def _stop(self, *, abort: bool = False) -> None:
        self._done = True
        self._aborted |= abort
        self._inbox.close(discard=abort)
        if self._close_requested is not None and not self._close_requested.done():
            self._close_requested.set_result(None)
        if abort and self._writer_task is not None:
            if self._writer_task is not asyncio.current_task():
                self._writer_task.cancel()

    async def send(self, msg: ActorMessage) -> None:
        if isinstance(msg, Close):
            # Terminal state is coalesced, never queued behind a full inbox.
            self._stop()
            return
        if self._inbox.closed:
            return
        try:
            self._inbox.put_nowait(msg)
        except asyncio.QueueFull:
            # A burst in one broker dispatch (e.g. retained replay) must give
            # a healthy writer an opportunity to consume, but must not await
            # a slow socket: its reader may itself be awaiting this broker.
            await asyncio.sleep(0)
            if self._inbox.closed:
                return
            try:
                self._inbox.put_nowait(msg)
            except asyncio.QueueFull:
                self._overloaded(msg)
        except MailboxTooLarge:
            self._overloaded(msg)

    def _overloaded(self, msg: ActorMessage) -> None:
        size = _output_size(msg)
        byte_cap = self._inbox.queued_bytes + size > self._inbox.max_bytes
        log_cap_hit(
            'mqtt_connection_inbox_max_bytes' if byte_cap else 'mqtt_connection_inbox_maxsize',
            requested=self._inbox.queued_bytes + size if byte_cap else self._inbox.qsize() + 1,
            limit=self._inbox.max_bytes if byte_cap else self._inbox.maxsize,
            protocol='mqtt')
        # A stalled sole writer cannot reliably transmit a refusal. Terminate
        # the transport through serve_connection; do not start a second writer.
        self.graceful = False
        self._stop(abort=True)

    async def run(self) -> None:
        self._writer_task = asyncio.current_task()
        try:
            while True:
                msg = await self._inbox.get()
                try:
                    await self._handle(msg)
                finally:
                    self._inbox.task_done()
        except MailboxClosed:
            logger.debug('MQTT connection mailbox closed; stopping writer loop.')
        finally:
            self._writer_task = None
            self._stop(abort=True)

    async def _handle(self, msg: ActorMessage) -> None:
        if isinstance(msg, Send):
            try:
                _output_size(msg)
                async with asyncio.timeout(self._write_timeout or None):
                    await self._writer.write(msg._encoded)
            except Exception:
                logger.debug('MQTT write failed', exc_info=True)
                self.graceful = False
                self._stop(abort=True)
        elif isinstance(msg, Close):
            self._stop()

    # -- reader task --------------------------------------------------------

    async def read_loop(self, reader: AbstractReader) -> None:
        """Decode the wire and forward control packets to the broker.

        Reads block on a real socket; a fake reader may return ``b''`` while
        merely idle, in which case we poll.  Either way the loop ends on EOF
        (``at_eof()``), a DISCONNECT (sets ``_done``), or task cancellation.
        """
        framer = PacketFramer(max_packet_size=self._max_packet_size)
        loop = asyncio.get_running_loop()
        self._last_rx = loop.time()
        while not self._done:
            try:
                for message in framer:
                    await self._forward(message)
                    if self._done:
                        return
            except (PacketTooLarge, MailboxTooLarge) as exc:
                logger.debug('MQTT %s; closing', exc)
                reason = (ReasonCode.PACKET_TOO_LARGE if isinstance(exc, PacketTooLarge)
                          else ReasonCode.QUOTA_EXCEEDED)
                await self._refuse(reason)
                return
            data = await self._read_with_keepalive(reader)
            if data:
                self._last_rx = loop.time()
                framer.feed(data)
            elif reader.at_eof():
                break  # peer closed the connection (EOF)
            elif self._keep_alive and (loop.time() - self._last_rx) > self._keep_alive * 1.5:
                # §3.1.2.10 — no packet within 1.5× the negotiated Keep Alive.
                # Treat it as an abnormal disconnect: fire the Will and close so
                # a dead peer (crashed client, half-open NAT) stops holding its
                # connection, session and Will indefinitely.
                logger.debug('MQTT keep-alive timeout (%.1fs idle); closing',
                             loop.time() - self._last_rx)
                self.graceful = False
                await self._broker.send(Detach(graceful=False, sender=self))
                self._done = True
            else:
                await asyncio.sleep(_IDLE_SLEEP)

    async def _refuse(self, reason_code: int) -> None:
        """Tell the peer why the connection is ending, then end it.

        A refusal the peer cannot read is indistinguishable from a crash,
        and a client that cannot tell the two apart will reconnect and do
        the same thing again.
        """
        await self.send(Send(packet=MQTTDisconnect(reason_code=reason_code)))
        await self.send(Close(reason_code=reason_code))
        self.graceful = False
        await self._broker.send(Detach(graceful=False, sender=self))

    async def _read_with_keepalive(self, reader: AbstractReader) -> bytes:
        """Read a chunk, but wake at the keep-alive deadline so a silent peer on
        a blocking socket cannot hold the connection open forever.  With Keep
        Alive disabled (0) this is a plain blocking read; otherwise a read that
        stalls past 1.5× the interval returns ``b''`` and the loop enforces the
        idle deadline (§3.1.2.10)."""
        if not self._keep_alive:
            return await reader.read(_READ_CHUNK)
        # NB: ``asyncio.timeout``, not ``asyncio.wait_for``.  On Python 3.11 the
        # latter can *swallow* an external ``CancelledError`` when the wrapped
        # read completes in the same loop iteration the cancel arrives — the
        # read-loop then never observes the cancellation and spins forever (the
        # connection task wedges in the ``cancelling`` state).  ``asyncio.timeout``
        # distinguishes its own deadline from an outer cancel and re-raises the
        # latter, so ``serve_connection`` can be torn down deterministically.
        try:
            async with asyncio.timeout(self._keep_alive * 1.5):
                return await reader.read(_READ_CHUNK)
        except asyncio.TimeoutError:
            return b''

    async def _forward(self, message: MQTTMessage) -> None:
        if self._done:
            return
        broker = self._broker
        if isinstance(message, MQTTConnect):
            self._keep_alive = message.keep_alive
            await broker.send(Attach(connect=message, sender=self))
        elif isinstance(message, MQTTPublish):
            # The reader may pipeline before CONNACK. Only the broker knows
            # whether this packet precedes or follows rejection/retirement.
            admitted = (asyncio.get_running_loop().create_future()
                        if self._tap is not None or self._inline_taps else None)
            await broker.send(ClientPublish(publish=message, sender=self,
                                            admitted=admitted))
            if admitted is not None and await admitted:
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
            await broker.send(ClientPing(sender=self))
        elif isinstance(message, MQTTAuth):
            await broker.send(ClientAuth(sender=self))
        elif isinstance(message, MQTTDisconnect):
            # "Disconnect with Will Message" keeps the Will; anything else is graceful.
            self.graceful = message.reason_code != ReasonCode.DISCONNECT_WITH_WILL
            # §3.14.2.2.2 — DISCONNECT may carry a Session Expiry Interval;
            # the broker decides whether it may be honoured.
            await broker.send(Detach(
                graceful=self.graceful, sender=self,
                session_expiry_interval=(message.properties or {}).get(
                    'session_expiry_interval')))
            self._done = True
        else:
            # A valid codec result can still be a server-only packet. Its
            # retirement must precede any subsequent pipelined command.
            await broker.send(ClientProtocolError(sender=self))

    async def _dispatch_taps(self, publish: MQTTPublish) -> None:
        """Route an inbound PUBLISH to the application taps.

        In actor mode the :class:`Message` is *offered* to the shared
        :class:`TapActor` and we return at once (a slow tap never back-pressures
        this connection or the broker).  In inline mode the matching callbacks
        run here, sequentially, with isolated exceptions.
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
    conn = MQTT5Actor(writer, broker, ctx, app_handlers=app_handlers, tap=tap)
    conn._close_requested = asyncio.get_running_loop().create_future()
    writer_task = asyncio.create_task(conn.run())
    reader_task = asyncio.create_task(conn.read_loop(reader))
    clean_read = False
    try:
        done, _ = await asyncio.wait(
            (reader_task, writer_task, conn._close_requested),
            return_when=asyncio.FIRST_COMPLETED)
        if reader_task in done:
            reader_task.result()
            clean_read = True
        elif conn._close_requested in done:
            clean_read = not conn._aborted
    finally:
        conn._close_requested.cancel()
        reader_task.cancel()
        try:
            await asyncio.gather(reader_task, return_exceptions=True)
            conn._done = True
            # Keep this connection's cleanup owned by its serving task while
            # it waits for FIFO admission. Broker shutdown wakes this wait;
            # no detached cleanup task or unbounded side queue is needed.
            with contextlib.suppress(MailboxClosed):
                detached = asyncio.Event()
                await broker.send(Detach(graceful=conn.graceful, sender=conn,
                                         processed=detached))
                if clean_read and not writer_task.done():
                    # A per-connection FIFO barrier: traffic on unrelated
                    # connections must not prolong this connection's flush.
                    with contextlib.suppress(TimeoutError):
                        async with asyncio.timeout(conn._write_timeout or None):
                            await detached.wait()
                            conn._stop()
                            # Detach only acknowledges broker processing;
                            # the sole writer must finish queued packets
                            # before the cleanup below can cancel it.
                            await writer_task
        finally:
            writer_task.cancel()
            await asyncio.gather(reader_task, writer_task, return_exceptions=True)
