"""MQTT 5.0 broker — the routing/session state owner (actor model).

:class:`BrokerActor` is one per app/worker, supervisor/lifespan-owned. It owns
*all* routing state (subscriptions, sessions, retained messages, the live
connection registry) and processes its inbox serially, so there are no locks
and no shared mutable state. Per-connection actors (see
:mod:`blackbull.mqtt.connection`) ``send`` it the Level A messages defined here
and receive :class:`Send` / :class:`Close` back.

Because the broker outlives every connection actor, a Will (LWT) routes to live
subscribers during a peer's teardown with no special-casing.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from ..actor import Actor, Message as ActorMessage
from .messages import (
    MQTTConnect, MQTTConnack,
    MQTTPublish, MQTTPuback, MQTTPubrec, MQTTPubrel, MQTTPubcomp,
    MQTTSubscribe, MQTTSuback,
    MQTTUnsubscribe, MQTTUnsuback,
    ProtocolLevel, ReasonCode,
    topic_matches_filter,
)

logger = logging.getLogger(__name__)


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


def _new_broker_session() -> dict[str, Any]:
    """Per-client session state owned by the broker (§3.1.2.11)."""
    return {
        # list[tuple[str, int, dict]] — (filter, qos, options).  §3.1.2.11
        # requires subscription *options* (No Local / Retain As Published /
        # Retain Handling) to be session state, so each entry carries the full
        # options dict and survives a Clean Start = 0 reconnect.
        'subscriptions': [],
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
        if connect.proto_level != ProtocolLevel.V5_0:
            await conn.send(Send(packet=MQTTConnack(
                session_present=False,
                reason_code=ReasonCode.UNSUPPORTED_PROTOCOL_VERSION)))
            await conn.send(Close(reason_code=ReasonCode.UNSUPPORTED_PROTOCOL_VERSION))
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
            session_present=session_present, reason_code=ReasonCode.SUCCESS)))

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
            opts = dict(options[index]) if index < len(options) else {}
            opts['qos'] = qos
            # §3.8.4 — a SUBSCRIBE for an existing Topic Filter replaces its
            # prior subscription (and its options) rather than adding a second.
            subs = [s for s in session['subscriptions'] if s[0] != topic_filter]
            subs.append((topic_filter, qos, opts))
            session['subscriptions'] = subs
            reason_codes.append(qos)  # granted QoS == requested QoS
            if int(opts.get('retain_handling', 0)) != 2:
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
        session['subscriptions'] = [
            s for s in session['subscriptions'] if s[0] not in topics
        ]
        await conn.send(Send(packet=MQTTUnsuback(
            packet_id=unsubscribe.packet_id,
            reason_codes=[ReasonCode.SUCCESS] * len(unsubscribe.topics))))

    async def _on_publish(self, conn, publish) -> None:
        if publish.retain:
            self._store_retained(publish)
        if publish.qos == 1:
            await conn.send(Send(packet=MQTTPuback(
                packet_id=publish.packet_id, reason_code=ReasonCode.SUCCESS)))
        elif publish.qos == 2:
            await conn.send(Send(packet=MQTTPubrec(
                packet_id=publish.packet_id, reason_code=ReasonCode.SUCCESS)))
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
            for topic_filter, qos, _opts in session['subscriptions']:
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
            packet_id=packet_id, reason_code=ReasonCode.SUCCESS)))
        session = self._session_for(conn)
        if session is not None:
            session['pending_qos2_in'].pop(packet_id, None)

    async def _on_pubrec(self, conn, packet_id) -> None:
        await conn.send(Send(packet=MQTTPubrel(
            packet_id=packet_id, reason_code=ReasonCode.SUCCESS)))
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
