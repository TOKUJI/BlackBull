"""MQTT 5.0 broker on the Non-ASGI bridge — actor model (Sprint 53).

Two actor roles, no shared mutable state:

* :class:`BrokerActor` — one per app/worker, supervisor/lifespan-owned. Owns
  *all* routing state (subscriptions, sessions, retained messages, the live
  connection registry). Processes its inbox serially, so there are no locks.
* :class:`MQTTConnectionActor` — one per connection. Its inbox carries only
  *outbound* packets, and its ``run()`` is the sole writer to the socket; a
  sibling reader loop (:meth:`MQTTConnectionActor.read_loop`) decodes the wire
  and ``send``s control messages to the broker. :func:`serve_connection` is the
  :data:`~blackbull.server.protocol_registry.RawProtocolHandler` body that wires
  the two together.

Together they implement the broker subset exercised by the conformance matrix:
CONNECT/CONNACK, SUBSCRIBE/SUBACK, UNSUBSCRIBE/UNSUBACK, PUBLISH QoS 0/1/2 with
their acknowledgement flows, PINGREQ/PINGRESP, retained messages, Will (LWT)
delivery on abnormal disconnect, and Clean Start / Session Present semantics.
Because the broker outlives every connection actor, a Will routes to live
subscribers during a peer's teardown with no special-casing.

Application taps registered via :meth:`MQTTExtension.on_message` run inline in
the connection actor (sequentially) and receive a :class:`Message`.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from typing import Any

from ..actor import Actor, Message as ActorMessage
from ..extension import Extension
from ..server.protocol_registry import ProtocolContext, ProtocolDetector
from ..server.recipient import AbstractReader
from ..server.sender import AbstractWriter
from .messages import (
    MQTTConnect, MQTTConnack,
    MQTTPublish, MQTTPuback, MQTTPubrec, MQTTPubrel, MQTTPubcomp,
    MQTTSubscribe, MQTTSuback,
    MQTTUnsubscribe, MQTTUnsuback,
    MQTTPingreq, MQTTPingresp,
    MQTTDisconnect, MQTTAuth,
    MQTTMessage,
    IncompletePacket, MQTTDecodeError,
    decode_packet, encode_packet,
    topic_matches_filter,
)

logger = logging.getLogger(__name__)

# MQTT 5.0 reason codes used by the broker.
_RC_SUCCESS = 0x00
_RC_UNSUPPORTED_PROTOCOL_VERSION = 0x84

_READ_CHUNK = 4096
_IDLE_SLEEP = 0.005


# ---------------------------------------------------------------------------
# Protocol detection (shared-port sniffing)
# ---------------------------------------------------------------------------

class MQTTProtocolDetector(ProtocolDetector):
    """Recognise an MQTT connection from its first byte.

    Every MQTT session opens with a CONNECT packet whose fixed-header first
    byte is ``0x10`` (type 1, flags 0).  That is unambiguous against the HTTP
    request line and the HTTP/2 preface, both of which start with ASCII text.
    """

    def detect(self, first_bytes: bytes, alpn: str | None) -> bool:
        return bool(first_bytes) and first_bytes[0] == 0x10

    @property
    def protocol_name(self) -> str:
        return 'mqtt'


# ---------------------------------------------------------------------------
# Extension — the user-facing wiring
# ---------------------------------------------------------------------------

class MQTTExtension(Extension):
    """Wires the MQTT 5 broker into a BlackBull app as a non-ASGI protocol.

    Register it once and tap the broker's routing with :meth:`on_message`::

        from blackbull.mqtt import MQTTExtension, Message

        mqtt = app.add_extension(MQTTExtension(port=1883))

        @mqtt.on_message(topic='sensors/+/temperature')
        async def on_temp(msg: Message):
            print(msg.topic, msg.payload)

    The broker itself (CONNECT/SUBSCRIBE/PUBLISH/QoS flows, retained messages,
    Will delivery) runs whether or not any handler is registered; handlers are
    an application-level tap on top of normal broker routing.

    This instance owns a single :class:`BrokerActor` (started on app startup,
    stopped on shutdown) holding all routing/session/retained state — one broker
    per worker, no module globals.  The class is self-contained and importable
    from a future ``blackbull-mqtt`` package without touching the core.
    """

    extension_key = 'mqtt'

    def __init__(self, *, port: int = 1883) -> None:
        self.port = port
        self._handlers: list[tuple[str, Any]] = []
        self._broker = BrokerActor()
        self._broker_task = None

    def on_message(self, topic: str = '#'):
        """Decorator: register an async ``(message) -> None`` tap for every
        PUBLISH whose topic matches *topic* (an MQTT topic filter, so ``+`` and
        ``#`` wildcards apply).  The callback receives a :class:`Message`."""
        def decorator(callback):
            self._handlers.append((topic, callback))
            return callback
        return decorator

    def init_app(self, app: Any) -> None:
        handlers = self._handlers  # shared list: later on_message() calls apply
        broker = self._broker

        async def _serve(reader, writer, ctx):
            await serve_connection(reader, writer, ctx, broker,
                                   app_handlers=handlers)

        app.register_protocol_handler(
            'mqtt', _serve, detector=MQTTProtocolDetector(), port=self.port)
        self._register(app)

    async def startup(self, app: Any) -> None:
        """Start the broker actor's inbox loop (once per worker)."""
        if self._broker_task is None:
            self._broker_task = asyncio.create_task(self._broker.run())

    async def shutdown(self, app: Any) -> None:
        """Stop the broker actor on lifespan shutdown."""
        if self._broker_task is not None:
            self._broker_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._broker_task
            self._broker_task = None


# ===========================================================================
# Actor-model broker (Sprint 53)
# ===========================================================================
#
# The classes below replace the procedural broker above (MQTTActor + module
# globals + _TopicRouter).  They coexist with it until the per-connection actor
# and the test-seam swap land (Phases 2 & 4 of bench/sprint-logs/sprint-53.md);
# the procedural path is then deleted.
#
# Design: a single, supervisor/lifespan-owned ``BrokerActor`` owns *all* routing
# state (subscriptions, sessions, retained, the live-connection registry).
# Because it processes its inbox serially there is no shared mutable state and
# no locks.  Per-connection actors ``send`` it the Level A messages below and
# receive ``Send`` / ``Close`` back.


# -- Level A messages: connection actor -> broker ---------------------------

@dataclass
class Attach(ActorMessage):
    """A client's CONNECT arrived; register it and reply with CONNACK."""
    connect: MQTTConnect | None = field(default=None, compare=False, repr=False)


@dataclass
class ClientSubscribe(ActorMessage):
    subscribe: MQTTSubscribe | None = field(default=None, compare=False, repr=False)


@dataclass
class ClientUnsubscribe(ActorMessage):
    unsubscribe: MQTTUnsubscribe | None = field(default=None, compare=False, repr=False)


@dataclass
class ClientPublish(ActorMessage):
    publish: MQTTPublish | None = field(default=None, compare=False, repr=False)


@dataclass
class ClientPuback(ActorMessage):
    packet_id: int | None = field(default=None, compare=False, repr=False)


@dataclass
class ClientPubrec(ActorMessage):
    packet_id: int | None = field(default=None, compare=False, repr=False)


@dataclass
class ClientPubrel(ActorMessage):
    packet_id: int | None = field(default=None, compare=False, repr=False)


@dataclass
class ClientPubcomp(ActorMessage):
    packet_id: int | None = field(default=None, compare=False, repr=False)


@dataclass
class Detach(ActorMessage):
    """A client's connection is ending (``graceful=False`` fires its Will)."""
    graceful: bool = field(default=True, compare=False, repr=False)


# -- Level A messages: broker -> connection actor ---------------------------

@dataclass
class Send(ActorMessage):
    """Tell the connection actor to encode + write *packet* to its socket."""
    packet: Any = field(default=None, compare=False, repr=False)


@dataclass
class Close(ActorMessage):
    """Tell the connection actor to close (e.g. CONNECT rejected)."""
    reason_code: int | None = field(default=None, compare=False, repr=False)


@dataclass(frozen=True)
class Message:
    """A published message handed to an ``on_message`` tap.

    The user-facing read-model — a plain, immutable view of one PUBLISH,
    deliberately distinct from the wire codec ``MQTTPublish`` (which carries the
    ``__iter__``/``__getitem__`` tuple-magic) and from the actor inbox
    ``Message`` base.  Named ``Message`` for ecosystem consistency (aiomqtt,
    paho): users write ``from blackbull.mqtt import Message``.
    """
    topic: str
    payload: bytes
    qos: int = 0
    retain: bool = False
    properties: dict = field(default_factory=dict)


def _new_broker_session() -> dict[str, Any]:
    """Per-client session state owned by the broker (§3.1.2.11)."""
    return {
        'subscriptions': set(),     # set[tuple[str, int]] (filter, qos)
        'pending_qos1_out': {},     # packet_id -> MQTTPublish awaiting PUBACK
        'pending_qos2_in': {},      # packet_id -> state str (PUBREC sent ...)
        'pending_qos2_out': {},     # packet_id -> state str (PUBLISH/PUBREL ...)
        '_expiry': 0,
        '_next_pid': 0,
    }


class BrokerActor(Actor):
    """Single owner of all MQTT routing state (one per app/worker).

    Connection actors ``send`` it client events; it ``send``s ``Send``/``Close``
    back to them.  Serial inbox processing means no locks and no shared mutable
    state — the design goal of the Sprint 53 refactor.
    """

    def __init__(self) -> None:
        super().__init__()
        self._clients = {}          # client_id -> live connection Actor
        self._client_by_conn = {}   # id(conn) -> client_id
        self._sessions = {}         # client_id -> session dict
        self._retained = {}         # topic -> MQTTPublish
        self._wills = {}            # client_id -> MQTTPublish (Will template)
        self._auto_seq = 0          # for server-assigned client ids

    # -- dispatch -----------------------------------------------------------

    async def _handle(self, msg: ActorMessage) -> None:
        if isinstance(msg, Attach):
            await self._on_attach(msg.sender, msg.connect)
        elif isinstance(msg, ClientPublish):
            await self._on_publish(msg.sender, msg.publish)
        elif isinstance(msg, ClientSubscribe):
            await self._on_subscribe(msg.sender, msg.subscribe)
        elif isinstance(msg, ClientUnsubscribe):
            await self._on_unsubscribe(msg.sender, msg.unsubscribe)
        elif isinstance(msg, ClientPubrel):
            await self._on_pubrel(msg.sender, msg.packet_id)
        elif isinstance(msg, ClientPubrec):
            await self._on_pubrec(msg.sender, msg.packet_id)
        elif isinstance(msg, ClientPubcomp):
            self._clear_pending(msg.sender, 'pending_qos2_out', msg.packet_id)
        elif isinstance(msg, ClientPuback):
            self._clear_pending(msg.sender, 'pending_qos1_out', msg.packet_id)
        elif isinstance(msg, Detach):
            await self._on_detach(msg.sender, msg.graceful)
        else:  # pragma: no cover - connection actor sends only the above
            logger.debug('BrokerActor ignoring %s', type(msg).__name__)

    # -- helpers ------------------------------------------------------------

    def _session_for(self, conn):
        client_id = self._client_by_conn.get(id(conn))
        return self._sessions.get(client_id) if client_id is not None else None

    @staticmethod
    def _alloc_pid(session) -> int:
        session['_next_pid'] = (session.get('_next_pid', 0) % 65535) + 1
        return session['_next_pid']

    def _store_retained(self, publish) -> None:
        # §3.3.2.3 — a zero-length retained payload deletes the retained message.
        if publish.payload == b'':
            self._retained.pop(publish.topic, None)
        else:
            self._retained[publish.topic] = publish

    def _clear_pending(self, conn, bucket, packet_id) -> None:
        session = self._session_for(conn)
        if session is not None:
            session[bucket].pop(packet_id, None)

    # -- handlers -----------------------------------------------------------

    async def _on_attach(self, conn, connect) -> None:
        if connect.proto_level != 5:
            await conn.send(Send(packet=MQTTConnack(
                session_present=False,
                reason_code=_RC_UNSUPPORTED_PROTOCOL_VERSION)))
            await conn.send(Close(reason_code=_RC_UNSUPPORTED_PROTOCOL_VERSION))
            return

        client_id = connect.client_id
        if not client_id:
            self._auto_seq += 1
            client_id = f'auto-{self._auto_seq}'

        self._clients[client_id] = conn
        self._client_by_conn[id(conn)] = client_id

        if connect.will_topic is not None:
            self._wills[client_id] = MQTTPublish(
                topic=connect.will_topic,
                payload=connect.will_payload or b'',
                qos=connect.will_qos,
                # Placeholder id only to satisfy the QoS>0 invariant; the real id
                # is allocated per subscriber at delivery time.
                packet_id=1 if connect.will_qos > 0 else None,
                retain=connect.will_retain,
                properties=dict(connect.will_properties),
            )
        else:
            self._wills.pop(client_id, None)

        expiry = connect.properties.get('session_expiry_interval', 0)
        session_present = False
        if connect.clean_start:
            session = _new_broker_session()
            session['_expiry'] = expiry
            self._sessions[client_id] = session
        else:
            existing = self._sessions.get(client_id)
            if existing is not None:
                session_present = True
                session = existing
                # Normalise a possibly partial session (e.g. one seeded directly
                # by a test) so the rest of the broker can rely on all keys.
                for key, default in _new_broker_session().items():
                    session.setdefault(key, default)
                session['_expiry'] = max(session.get('_expiry', 0), expiry)
            else:
                session = _new_broker_session()
                session['_expiry'] = expiry
                self._sessions[client_id] = session

        await conn.send(Send(packet=MQTTConnack(
            session_present=session_present, reason_code=_RC_SUCCESS)))

        # Deliver QoS 1 messages queued while the client was offline.
        if session_present:
            for pending in list(session['pending_qos1_out'].values()):
                await conn.send(Send(packet=pending))

    async def _on_subscribe(self, conn, subscribe) -> None:
        session = self._session_for(conn)
        if session is None:
            return
        reason_codes = []
        options = subscribe.subscription_options or []
        for index, (topic_filter, qos) in enumerate(subscribe.subscriptions):
            session['subscriptions'].add((topic_filter, qos))
            reason_codes.append(qos)  # granted QoS == requested QoS
            retain_handling = (int(options[index].get('retain_handling', 0))
                               if index < len(options) else 0)
            if retain_handling != 2:
                await self._deliver_retained(conn, session, topic_filter, qos)
        await conn.send(Send(packet=MQTTSuback(
            packet_id=subscribe.packet_id, reason_codes=reason_codes)))

    async def _deliver_retained(self, conn, session, topic_filter, qos) -> None:
        for topic, retained in list(self._retained.items()):
            if topic_matches_filter(topic, topic_filter):
                await self._deliver(conn, session, retained, qos, retain=True)

    async def _on_unsubscribe(self, conn, unsubscribe) -> None:
        session = self._session_for(conn)
        if session is None:
            return
        topics = set(unsubscribe.topics)
        session['subscriptions'] = {
            (f, q) for (f, q) in session['subscriptions'] if f not in topics
        }
        await conn.send(Send(packet=MQTTUnsuback(
            packet_id=unsubscribe.packet_id,
            reason_codes=[_RC_SUCCESS] * len(unsubscribe.topics))))

    async def _on_publish(self, conn, publish) -> None:
        if publish.retain:
            self._store_retained(publish)
        if publish.qos == 1:
            await conn.send(Send(packet=MQTTPuback(
                packet_id=publish.packet_id, reason_code=_RC_SUCCESS)))
        elif publish.qos == 2:
            await conn.send(Send(packet=MQTTPubrec(
                packet_id=publish.packet_id, reason_code=_RC_SUCCESS)))
            session = self._session_for(conn)
            if session is not None:
                session['pending_qos2_in'][publish.packet_id] = 'PUBREC_SENT'
        await self._route(publish)

    async def _route(self, publish) -> None:
        """Deliver *publish* to every live client with a matching subscription."""
        for client_id, conn in list(self._clients.items()):
            session = self._sessions.get(client_id)
            if session is None:
                continue
            granted = None
            for topic_filter, qos in session['subscriptions']:
                if topic_matches_filter(publish.topic, topic_filter):
                    granted = qos if granted is None else max(granted, qos)
            if granted is None:
                continue
            await self._deliver(conn, session, publish, granted)

    async def _deliver(self, conn, session, publish, granted_qos, *, retain=False) -> None:
        qos = min(publish.qos, granted_qos)
        packet_id = self._alloc_pid(session) if qos > 0 else None
        out = MQTTPublish(
            topic=publish.topic, payload=publish.payload, qos=qos,
            packet_id=packet_id, retain=retain, properties=dict(publish.properties))
        if qos == 1:
            session['pending_qos1_out'][packet_id] = out
        elif qos == 2:
            session['pending_qos2_out'][packet_id] = 'PUBLISH_SENT'
        await conn.send(Send(packet=out))

    async def _on_pubrel(self, conn, packet_id) -> None:
        await conn.send(Send(packet=MQTTPubcomp(
            packet_id=packet_id, reason_code=_RC_SUCCESS)))
        session = self._session_for(conn)
        if session is not None:
            session['pending_qos2_in'].pop(packet_id, None)

    async def _on_pubrec(self, conn, packet_id) -> None:
        await conn.send(Send(packet=MQTTPubrel(
            packet_id=packet_id, reason_code=_RC_SUCCESS)))
        session = self._session_for(conn)
        if session is not None:
            session['pending_qos2_out'][packet_id] = 'PUBREL_SENT'

    async def _on_detach(self, conn, graceful) -> None:
        client_id = self._client_by_conn.pop(id(conn), None)
        if client_id is None:
            return
        # Drop the live connection first so a routed Will is not echoed to it.
        if self._clients.get(client_id) is conn:
            self._clients.pop(client_id, None)
        # Will (LWT) on abnormal disconnect.  The broker outlives the connection
        # actors, so subscribers are still live here — no teardown race, and no
        # need for the old "keep globals forever" crutch.
        will = self._wills.pop(client_id, None)
        if will is not None and not graceful:
            if will.retain:
                self._store_retained(will)
            await self._route(will)
        session = self._sessions.get(client_id)
        if session is not None and session.get('_expiry', 0) <= 0:
            self._sessions.pop(client_id, None)


# ---------------------------------------------------------------------------
# Per-connection actor (Sprint 53)
# ---------------------------------------------------------------------------

class MQTTConnectionActor(Actor):
    """One per MQTT connection.

    Its **inbox carries only outbound packets** (``Send``/``Close`` from the
    broker, plus the two stateless replies this actor generates itself), and
    ``run()`` — draining that inbox — is the *sole writer* to the socket, so
    there are no cross-task write races.  A sibling reader task (:meth:`read_loop`)
    decodes the wire and ``send``s control messages to the broker.
    """

    def __init__(self, writer: AbstractWriter, broker: BrokerActor,
                 ctx: ProtocolContext, app_handlers=None) -> None:
        super().__init__()
        self._writer = writer
        self._broker = broker
        self._ctx = ctx
        # (topic_filter, async callback) pairs registered via
        # ``MQTTExtension.on_message``; run inline (sequentially) when a matching
        # PUBLISH arrives on this connection — see the Sprint 53 tap-dispatch
        # decision (a decoupled TapActor is Sprint 54).
        self._app_handlers = app_handlers or []
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

        An empty ``read()`` means *idle* (not EOF) — the connection stays open
        until a DISCONNECT (sets ``_done``) or the handler task is cancelled.
        """
        buffer = bytearray()
        stalled_len = -1
        while not self._done:
            while buffer and not self._done:
                try:
                    message = decode_packet(bytes(buffer))
                except IncompletePacket:
                    if len(buffer) == stalled_len:
                        del buffer[0]
                        stalled_len = -1
                        continue
                    stalled_len = len(buffer)
                    break
                except (MQTTDecodeError, ValueError):
                    del buffer[0]
                    stalled_len = -1
                    continue
                consumed = message[1]
                del buffer[:consumed]
                stalled_len = -1
                await self._forward(message)

            if self._done:
                break
            data = await reader.read(_READ_CHUNK)
            if data:
                buffer += data
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
        """Invoke each matching ``on_message`` tap inline (sequential).

        Runs on *this* connection only, so a slow tap back-pressures one client,
        never the broker (the Sprint 53 contract).  Exceptions are isolated.
        """
        if not self._app_handlers:
            return
        msg = Message(topic=publish.topic, payload=publish.payload,
                      qos=publish.qos, retain=publish.retain,
                      properties=dict(publish.properties))
        for topic_filter, callback in self._app_handlers:
            if topic_matches_filter(publish.topic, topic_filter):
                try:
                    await callback(msg)
                except Exception:  # pragma: no cover - user handler isolation
                    logger.exception('MQTT on_message handler for %r raised',
                                     topic_filter)


async def serve_connection(reader: AbstractReader, writer: AbstractWriter,
                           ctx: ProtocolContext, broker: BrokerActor,
                           app_handlers=None) -> None:
    """Raw-protocol handler body for one MQTT connection.

    Spawns the connection actor's inbox-drain (`run`) alongside the reader loop,
    and guarantees the broker sees a ``Detach`` when the connection ends — so a
    Will fires on an abnormal (cancelled) close.
    """
    conn = MQTTConnectionActor(writer, broker, ctx, app_handlers=app_handlers)
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
