"""
§3.1.2-24 — the peer's Maximum Packet Size binds every packet we send.

The CONNACK first: full, then trimmed of the broker's advertised limits
([MQTT-3.2.2-16] — an assigned identifier is not optional), then a `0x95`
refusal when that fits, else a bare Close — decided before anything is
registered.  Every refusal reply honours the limit the same way.

Then the data path (§3.1.2-25): an Application Message whose packet the
limit excludes is discarded whole at the delivery decision — no Packet
Identifier booked, no pending entry, nothing for §4.4 to resend — and the
flow is then treated complete, including a queued PUBLISH or a PUBREL
re-drive that no longer fits the current connection.  Probe-derived
bounds keep the tests honest about actual encoded sizes.
"""

from __future__ import annotations

import pytest

from blackbull.actor import Actor
from blackbull.mqtt.broker import (
    BrokerActor, Attach, Detach, Send, Close, ClientPuback,
)

from blackbull.mqtt.messages import (
    MQTTConnect,
    MQTTConnack,
    MQTTDisconnect,
    MQTTPublish,
    MQTTPubrel,
    MQTTSubscribe,
    ReasonCode,
    encode_packet,
)

pytestmark = pytest.mark.asyncio


class RecordingConn(Actor):
    def __init__(self) -> None:
        super().__init__()
        self.outbox = []

    async def send(self, msg) -> None:
        self.outbox.append(msg)

    def packets(self):
        return [m.packet for m in self.outbox if isinstance(m, Send)]


def _connect(client_id='', clean_start=True, **kw):
    return MQTTConnect(client_id=client_id, clean_start=clean_start,
                       keep_alive=kw.pop('keep_alive', 60), **kw)


async def _attach(broker, conn, **kw):
    await broker._handle(Attach(connect=_connect(**kw), sender=conn))


async def _detach(broker, conn):
    await broker._handle(Detach(graceful=True, sender=conn))


def _assigned(connack):
    return (connack.properties or {}).get('assigned_client_identifier')


def _size(packet) -> int:
    return len(encode_packet(packet))


def _refusal_size(reason=ReasonCode.PACKET_TOO_LARGE) -> int:
    return _size(MQTTConnack(session_present=False, reason_code=reason))


def _minimal_size() -> int:
    return _size(MQTTConnack(session_present=False,
                             reason_code=ReasonCode.SUCCESS))


async def _sizes(client_id='') -> tuple[int, int]:
    """(full, trimmed) success-CONNACK sizes for one CONNECT shape."""
    broker, probe = BrokerActor(), RecordingConn()
    await _attach(broker, probe, client_id=client_id)
    full = _size(probe.packets()[0])
    trimmed = _size(MQTTConnack(
        session_present=False, reason_code=ReasonCode.SUCCESS,
        properties={'assigned_client_identifier': _assigned(probe.packets()[0])}
        if client_id == '' else {}))
    return full, trimmed


def _assert_within(conn, limit):
    for msg in conn.outbox:
        if isinstance(msg, Send):
            assert len(msg.wire_bytes()) <= limit


def _assert_unregistered(broker):
    assert broker._sessions == {}
    assert broker._clients == {}
    assert broker._client_by_conn == {}
    assert broker._wills == {}


class TestEmptyIdentifierConnack:
    async def test_the_full_connack_is_sent_when_it_fits(self):
        full, trimmed = await _sizes()
        assert trimmed < full
        broker, conn = BrokerActor(), RecordingConn()
        await _attach(broker, conn,
                      properties={'maximum_packet_size': full})
        ack = conn.packets()[0]
        assert ack.reason_code == ReasonCode.SUCCESS
        assigned = _assigned(ack)
        assert assigned
        assert {'maximum_packet_size', 'receive_maximum'} <= set(ack.properties)
        assert set(broker._sessions) == {assigned}
        _assert_within(conn, full)

    async def test_the_trimmed_connack_is_sent_when_only_it_fits(self):
        full, trimmed = await _sizes()
        broker, conn = BrokerActor(), RecordingConn()
        await _attach(broker, conn,
                      properties={'maximum_packet_size': full - 1})
        ack = conn.packets()[0]
        assert ack.reason_code == ReasonCode.SUCCESS
        assigned = _assigned(ack)
        assert assigned
        assert set(ack.properties) == {'assigned_client_identifier'}
        assert set(broker._sessions) == {assigned}
        _assert_within(conn, full - 1)

    async def test_an_unfittable_assigned_connack_is_refused_without_a_session(self):
        _, trimmed = await _sizes()
        broker, conn = BrokerActor(), RecordingConn()
        await _attach(broker, conn, will_topic='w',
                      properties={'maximum_packet_size': trimmed - 1})
        # §3.14 [MQTT-3.14.0-1] — DISCONNECT only ever follows a CONNACK.
        ack = conn.packets()[0]
        assert isinstance(ack, MQTTConnack)
        assert ack.reason_code == ReasonCode.PACKET_TOO_LARGE
        assert not any(isinstance(p, MQTTDisconnect) for p in conn.packets())
        assert any(isinstance(m, Close) for m in conn.outbox)
        _assert_unregistered(broker)
        _assert_within(conn, trimmed - 1)
        await _detach(broker, conn)
        _assert_unregistered(broker)

    async def test_below_the_refusal_size_nothing_is_sent(self):
        limit = _refusal_size() - 1
        broker, conn = BrokerActor(), RecordingConn()
        await _attach(broker, conn, will_topic='w',
                      properties={'maximum_packet_size': limit})
        assert conn.packets() == []
        assert any(isinstance(m, Close) for m in conn.outbox)
        _assert_unregistered(broker)


class TestNamedIdentifierConnack:
    async def test_a_named_connack_just_below_full_fits_trimmed(self):
        full, trimmed = await _sizes(client_id='probe')
        assert trimmed < full
        broker, conn = BrokerActor(), RecordingConn()
        await _attach(broker, conn, client_id='c1',
                      properties={'maximum_packet_size': full - 1})
        ack = conn.packets()[0]
        assert ack.reason_code == ReasonCode.SUCCESS
        assert _assigned(ack) is None
        assert not (ack.properties or {})
        assert set(broker._sessions) == {'c1'}
        _assert_within(conn, full - 1)

    async def test_a_named_resume_keeps_session_present_trimmed(self):
        broker, first = BrokerActor(), RecordingConn()
        await _attach(broker, first, client_id='c1', clean_start=False,
                      properties={'session_expiry_interval': 3600})
        await _detach(broker, first)
        full = _size(first.packets()[0])
        conn = RecordingConn()
        await _attach(broker, conn, client_id='c1', clean_start=False,
                      properties={'maximum_packet_size': full - 1})
        ack = conn.packets()[0]
        assert ack.reason_code == ReasonCode.SUCCESS
        assert ack.session_present is True
        assert _assigned(ack) is None
        assert not (ack.properties or {})
        assert set(broker._sessions) == {'c1'}
        _assert_within(conn, full - 1)

    async def test_a_named_connect_is_accepted_whenever_any_reply_fits(self):
        # The trimmed named CONNACK is the minimal CONNACK — the same size
        # as the `0x95` refusal — so a named CONNECT is refused only by a
        # bare Close.
        assert _minimal_size() == _refusal_size()
        broker, conn = BrokerActor(), RecordingConn()
        await _attach(broker, conn, client_id='c1',
                      properties={'maximum_packet_size': _refusal_size()})
        ack = conn.packets()[0]
        assert ack.reason_code == ReasonCode.SUCCESS
        assert set(broker._sessions) == {'c1'}

    async def test_a_named_connack_below_everything_gets_only_a_close(self):
        limit = _minimal_size() - 1
        broker, conn = BrokerActor(), RecordingConn()
        await _attach(broker, conn, client_id='c1', will_topic='w',
                      properties={'maximum_packet_size': limit})
        assert conn.packets() == []
        assert any(isinstance(m, Close) for m in conn.outbox)
        _assert_unregistered(broker)


class TestRefusalsHonourTheLimit:
    async def test_the_quota_refusal_is_sent_only_when_it_fits(self):
        broker = BrokerActor()
        broker._max_sessions = 1
        await _attach(broker, RecordingConn(), client_id='c1')
        conn = RecordingConn()
        await _attach(broker, conn, client_id='c2',
                      properties={'maximum_packet_size': _refusal_size()})
        assert conn.packets()[0].reason_code == ReasonCode.QUOTA_EXCEEDED
        assert set(broker._sessions) == {'c1'}

        broker = BrokerActor()
        broker._max_sessions = 1
        await _attach(broker, RecordingConn(), client_id='c1')
        conn = RecordingConn()
        await _attach(broker, conn, client_id='c2',
                      properties={'maximum_packet_size': _refusal_size() - 1})
        assert conn.packets() == []
        assert any(isinstance(m, Close) for m in conn.outbox)
        assert set(broker._sessions) == {'c1'}

    async def test_the_version_refusal_is_sent_only_when_it_fits(self):
        broker, conn = BrokerActor(), RecordingConn()
        await _attach(broker, conn, client_id='c1', proto_level=4,
                      properties={'maximum_packet_size': _refusal_size()})
        assert (conn.packets()[0].reason_code
                == ReasonCode.UNSUPPORTED_PROTOCOL_VERSION)
        assert any(isinstance(m, Close) for m in conn.outbox)
        _assert_unregistered(broker)

        broker, conn = BrokerActor(), RecordingConn()
        await _attach(broker, conn, client_id='c1', proto_level=4,
                      properties={'maximum_packet_size': _refusal_size() - 1})
        assert conn.packets() == []
        assert any(isinstance(m, Close) for m in conn.outbox)
        _assert_unregistered(broker)

    async def test_the_packet_limit_is_answered_before_the_quota(self):
        _, trimmed = await _sizes()
        broker = BrokerActor()
        broker._max_sessions = 1
        await _attach(broker, RecordingConn(), client_id='c1')
        conn = RecordingConn()
        await _attach(broker, conn,
                      properties={'maximum_packet_size': trimmed - 1})
        assert conn.packets()[0].reason_code == ReasonCode.PACKET_TOO_LARGE
        assert set(broker._sessions) == {'c1'}

    async def test_a_tiny_limit_gets_a_close_and_nothing_else(self):
        broker, conn = BrokerActor(), RecordingConn()
        await _attach(broker, conn, will_topic='w',
                      properties={'maximum_packet_size': 2})
        assert conn.packets() == []
        assert any(isinstance(m, Close) for m in conn.outbox)
        _assert_unregistered(broker)

    async def test_a_zero_maximum_packet_size_cannot_bypass_the_check(self):
        broker, conn = BrokerActor(), RecordingConn()
        await _attach(broker, conn, will_topic='w',
                      properties={'maximum_packet_size': 0})
        assert conn.packets() == []
        assert any(isinstance(m, Close) for m in conn.outbox)
        _assert_unregistered(broker)


async def _subscriber(broker, client_id='probe-connect', *, limit=None,
                      subs=(('a', 1),), receive_maximum=None,
                      clean_start=True, session_expiry=0, **kw):
    """One established client, optionally subscribed: (conn, session)."""
    conn = RecordingConn()
    props = {'session_expiry_interval': session_expiry}
    if limit is not None:
        props['maximum_packet_size'] = limit
    if receive_maximum is not None:
        props['receive_maximum'] = receive_maximum
    props = {k: v for k, v in props.items() if v}
    await _attach(broker, conn, client_id=client_id, clean_start=clean_start,
                  properties=props, **kw)
    session = broker._session_for(conn)
    if subs:
        await broker._on_subscribe(conn, MQTTSubscribe(
            packet_id=1, subscriptions=list(subs),
            subscription_options=[{} for _ in subs]))
    return conn, session


def _pubs(conn):
    return [m.packet for m in conn.outbox
            if isinstance(m, Send) and isinstance(m.packet, MQTTPublish)]


def _pubrels(conn):
    return [m.packet for m in conn.outbox
            if isinstance(m, Send) and isinstance(m.packet, MQTTPubrel)]


def _named_floor() -> int:
    """The smallest Maximum Packet Size a named client can declare and
    still be accepted — its trimmed CONNACK (properties are zero-length)."""
    return _size(MQTTConnack(session_present=False,
                             reason_code=ReasonCode.SUCCESS, properties={}))


def _msg(payload=b'', qos=0, topic='a'):
    return MQTTPublish(topic=topic, payload=payload, qos=qos,
                       packet_id=1 if qos > 0 else None, properties={})


class TestApplicationMessageDiscard:
    """§3.1.2-25 — a message the peer's limit excludes never happens.

    Dropped whole at the delivery decision: not on the wire, no Packet
    Identifier booked, no pending entry, nothing §4.4 would resend.  The
    decision is the *current* connection's declaration (§3.1.2.11.4 — the
    Maximum Packet Size the Client is willing to accept is a CONNECT
    property; §4.1's Session State list does not carry it), so queued
    messages are re-judged against whoever receives the re-drive.
    """

    async def test_an_oversized_qos0_message_never_reaches_the_wire(self):
        broker = BrokerActor()
        conn, session = await _subscriber(broker, limit=_size(_msg(qos=0)))
        await broker._route(_msg(payload=b'x', qos=0))
        assert _pubs(conn) == []
        assert not session['outbound_queue']

    async def test_an_oversized_qos1_message_is_dropped_without_a_packet_identifier(self):
        small = _msg(payload=b'ok', qos=1)
        broker = BrokerActor()
        conn, session = await _subscriber(broker, limit=_size(small))
        await broker._route(_msg(payload=b'x' * 32, qos=1))
        assert _pubs(conn) == []
        assert session['pending_qos1_out'] == {}
        assert session['pending_qos2_out'] == {}
        await broker._route(_msg(payload=b'ok', qos=1))
        assert [p.packet_id for p in _pubs(conn)] == [1]

    async def test_an_oversized_qos2_message_is_dropped_without_a_packet_identifier(self):
        small = _msg(payload=b'ok', qos=1)
        broker = BrokerActor()
        conn, session = await _subscriber(broker, limit=_size(small))
        await broker._route(_msg(payload=b'x' * 32, qos=2))
        assert _pubs(conn) == []
        assert session['pending_qos1_out'] == {}
        assert session['pending_qos2_out'] == {}
        await broker._route(_msg(payload=b'ok', qos=2))
        assert [p.packet_id for p in _pubs(conn)] == [1]

    async def test_a_dropped_message_is_not_held_when_the_window_is_full(self):
        small = _msg(payload=b'ok', qos=1)
        broker = BrokerActor()
        conn, session = await _subscriber(
            broker, limit=_size(_msg(payload=b'first', qos=1)),
            receive_maximum=1)
        await broker._route(_msg(payload=b'first', qos=1))
        await broker._route(_msg(payload=b'ok', qos=1))
        await broker._route(_msg(payload=b'x' * 32, qos=1))
        assert [p.payload for p in _pubs(conn)] == [b'first']
        assert [h.publish.payload for h in session['outbound_queue']] == [b'ok']
        await broker._handle(ClientPuback(packet_id=1, sender=conn))
        assert [p.payload for p in _pubs(conn)] == [b'first', b'ok']
        assert not session['outbound_queue']

    async def test_queued_redelivery_drops_an_oversized_qos1_publish(self):
        small = _msg(payload=b'ok', qos=1)
        broker = BrokerActor()
        conn, session = await _subscriber(broker, subs=(('a', 1),),
                                          session_expiry=30)
        await broker._route(_msg(payload=b'ok', qos=1, topic='a'))
        await broker._route(_msg(payload=b'x' * 32, qos=1, topic='a'))
        assert len(session['pending_qos1_out']) == 2
        await _detach(broker, conn)
        conn2 = RecordingConn()
        await _attach(broker, conn2, client_id='probe-connect',
                      clean_start=False,
                      properties={'maximum_packet_size': _size(small)})
        pubs = _pubs(conn2)
        assert [(p.payload, p.dup) for p in pubs] == [(b'ok', True)]
        assert [p.payload for p in session['pending_qos1_out'].values()] == [b'ok']

    async def test_queued_redelivery_drops_an_oversized_qos2_publish(self):
        small = _msg(payload=b'ok', qos=1)
        broker = BrokerActor()
        conn, session = await _subscriber(broker, subs=(('a', 2),),
                                          session_expiry=30)
        await broker._route(_msg(payload=b'ok', qos=2, topic='a'))
        await broker._route(_msg(payload=b'x' * 32, qos=2, topic='a'))
        assert len(session['pending_qos2_out']) == 2
        await _detach(broker, conn)
        conn2 = RecordingConn()
        await _attach(broker, conn2, client_id='probe-connect',
                      clean_start=False,
                      properties={'maximum_packet_size': _size(small)})
        assert [(p.payload, p.dup) for p in _pubs(conn2)] == [(b'ok', True)]
        assert set(session['pending_qos2_out']) == {1}
        assert session['pending_qos2_out'][1]['packet'].payload == b'ok'

    async def test_a_pubrel_redrive_the_peer_cannot_receive_completes_the_flow(self):
        # The minimum limit a named client can declare and still get a CONNACK
        # is the trimmed size (5); the PUBREL re-drive is 6 bytes, so §4.4's
        # resend can be ruled out at the wire while the flow is past PUBREC.
        broker = BrokerActor()
        conn, session = await _subscriber(broker, subs=(('a', 2),),
                                          session_expiry=30)
        await broker._route(_msg(payload=b'ok', qos=2, topic='a'))
        pid = next(iter(session['pending_qos2_out']))
        await broker._on_pubrec(conn, pid)
        await _detach(broker, conn)
        conn2 = RecordingConn()
        await _attach(broker, conn2, client_id='probe-connect',
                      clean_start=False,
                      properties={'maximum_packet_size': _named_floor()})
        assert _pubrels(conn2) == []
        assert session['pending_qos2_out'] == {}

    async def test_retained_replay_drops_an_oversized_message(self):
        broker = BrokerActor()
        broker._store_retained(_msg(payload=b'x' * 32, qos=0))
        conn, session = await _subscriber(
            broker, limit=_size(_msg(payload=b'ok', qos=0)), subs=())
        await broker._on_subscribe(conn, MQTTSubscribe(
            packet_id=1, subscriptions=[('a', 0)], subscription_options=[{}]))
        assert _pubs(conn) == []

    async def test_will_delivery_drops_an_oversized_message(self):
        broker = BrokerActor()
        conn, _ = await _subscriber(
            broker, limit=_size(_msg(payload=b'ok', qos=0)))
        owner = RecordingConn()
        await _attach(broker, owner, client_id='owner',
                      will_topic='a', will_payload=b'x' * 32, will_qos=0)
        await broker._handle(Detach(graceful=False, sender=owner))
        assert _pubs(conn) == []

    async def test_a_shared_subscription_skips_a_member_that_cannot_receive(self):
        """§3.1.2.11.4 — "In the case of a Shared Subscription where the
        message is too large to send to one or more of the Clients but other
        Clients can receive it, the Server can choose either discard the
        message without sending the message to any of the Clients, or to send
        the message to one of the Clients that can receive it."  BlackBull
        sends it to one that can receive it."""
        broker = BrokerActor()
        small, _ = await _subscriber(
            broker, 'probe-connect', limit=_size(_msg(payload=b'ok', qos=0)),
            subs=(('$share/g/a', 1),))
        roomy, _ = await _subscriber(
            broker, 'other', subs=(('$share/g/a', 1),))
        await broker._route(_msg(payload=b'x' * 32, qos=1))
        assert _pubs(small) == []
        assert [p.payload for p in _pubs(roomy)] == [b'x' * 32]

    async def test_a_shared_subscription_drops_the_message_when_no_member_can_receive(self):
        broker = BrokerActor()
        limit = _size(_msg(payload=b'ok', qos=0))
        first, _ = await _subscriber(
            broker, 'probe-connect', limit=limit, subs=(('$share/g/a', 1),))
        second, _ = await _subscriber(
            broker, 'other', limit=limit, subs=(('$share/g/a', 1),))
        await broker._route(_msg(payload=b'x' * 32, qos=1))
        assert _pubs(first) == []
        assert _pubs(second) == []

    async def test_a_message_that_exactly_fits_is_delivered_unchanged(self):
        small = _msg(payload=b'ok', qos=1)
        broker = BrokerActor()
        conn, session = await _subscriber(broker, limit=_size(small))
        await broker._route(_msg(payload=b'ok', qos=1))
        pubs = _pubs(conn)
        assert pubs == [MQTTPublish(topic='a', payload=b'ok', qos=1,
                                    packet_id=1, retain=False, properties={})]
        assert len(encode_packet(pubs[0])) == _size(small)

    async def test_a_message_one_byte_over_the_limit_is_dropped(self):
        small = _msg(payload=b'ok', qos=0)
        broker = BrokerActor()
        conn, session = await _subscriber(broker, limit=_size(small) - 1)
        await broker._route(_msg(payload=b'ok', qos=0))
        assert _pubs(conn) == []

    async def test_a_client_without_the_property_receives_byte_identical_messages(self):
        broker = BrokerActor()
        conn, session = await _subscriber(broker, subs=(('a', 1), ('b', 0)))
        await broker._route(_msg(payload=b'ok', qos=1))
        await broker._route(_msg(payload=b'ok', qos=0, topic='b'))
        assert [encode_packet(p) for p in _pubs(conn)] == [
            encode_packet(MQTTPublish(topic='a', payload=b'ok', qos=1,
                                      packet_id=1, retain=False,
                                      properties={})),
            encode_packet(MQTTPublish(topic='b', payload=b'ok', qos=0,
                                      retain=False, properties={})),
        ]

    async def test_the_minimum_reachable_limit_drops_every_message(self):
        broker = BrokerActor()
        conn, session = await _subscriber(
            broker, limit=_named_floor(), subs=(('a', 0), ('a', 1),
                                                ('b', 2)))
        await broker._route(_msg(payload=b'ok', qos=0))
        await broker._route(_msg(payload=b'ok', qos=1))
        await broker._route(_msg(payload=b'ok', qos=2))
        assert _pubs(conn) == []
        assert session['pending_qos1_out'] == {}
        assert session['pending_qos2_out'] == {}
        assert not session['outbound_queue']

    async def test_a_reconnect_without_the_property_lifts_the_limit(self):
        broker = BrokerActor()
        conn, session = await _subscriber(
            broker, limit=_named_floor(), subs=(('a', 1),), session_expiry=30)
        await broker._route(_msg(payload=b'ok', qos=1))
        assert _pubs(conn) == []
        await _detach(broker, conn)
        conn2 = RecordingConn()
        await _attach(broker, conn2, client_id='probe-connect',
                      clean_start=False)
        await broker._route(_msg(payload=b'ok', qos=1))
        assert [p.payload for p in _pubs(conn2)] == [b'ok']
