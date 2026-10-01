"""The send quota belongs to a Network Connection (§4.9) and replay spends it.

An omitted `Receive Maximum` means 65535 on *this* connection, and the window
is shared by live delivery and the §4.4 retransmissions of a resumed session.
"""
from __future__ import annotations

import pytest

from blackbull.actor import Actor
from blackbull.mqtt.broker import (
    Attach, BrokerActor, ClientPing, ClientProtocolError, ClientPuback,
    ClientPubcomp, ClientPubrel, ClientPublish, ClientPubrec, ClientSubscribe,
    Detach, Send,
)
from blackbull.mqtt.messages import (
    MQTTConnack, MQTTConnect, MQTTPublish, MQTTPubrel, MQTTSubscribe, ReasonCode,
)

pytestmark = pytest.mark.asyncio


class RecordingConn(Actor):
    """A fake connection actor that records what the broker sends it."""

    def __init__(self) -> None:
        super().__init__()
        self.outbox = []

    async def send(self, msg) -> None:  # override: record instead of enqueue
        self.outbox.append(msg)

    def packets(self) -> list:
        return [m.packet for m in self.outbox if isinstance(m, Send)]

    def publishes(self) -> list:
        return [p for p in self.packets() if isinstance(p, MQTTPublish)]


async def _attach(broker, conn, *, client_id='c1', clean_start=True,
                  receive_maximum=None, expiry=3600):
    props = {'session_expiry_interval': expiry}
    if receive_maximum is not None:
        props['receive_maximum'] = receive_maximum
    await broker._handle(Attach(
        connect=MQTTConnect(client_id=client_id, clean_start=clean_start,
                            keep_alive=60, properties=props),
        sender=conn))


async def _detach(broker, conn):
    await broker._handle(Detach(graceful=True, sender=conn))


async def _subscribe(broker, conn, topic='t', qos=1):
    await broker._handle(ClientSubscribe(
        subscribe=MQTTSubscribe(packet_id=1, subscriptions=[(topic, qos)]),
        sender=conn))


async def _publish(broker, source, *, payload=b'x', qos=1, packet_id=1,
                   topic='t'):
    await broker._handle(ClientPublish(
        publish=MQTTPublish(topic=topic, payload=payload, qos=qos,
                            packet_id=packet_id),
        sender=source))


async def _three_unacked(broker, *, receive_maximum=3):
    sub, pub = RecordingConn(), RecordingConn()
    await _attach(broker, sub, receive_maximum=receive_maximum)
    await _subscribe(broker, sub, qos=1)
    await _attach(broker, pub, client_id='pub')
    for i in range(3):
        await _publish(broker, pub, payload=bytes([i]), packet_id=i + 1)
    pids = [p.packet_id for p in sub.publishes()]
    assert len(pids) == 3
    return sub, pub, pids


class TestQuotaIsPerConnection:
    async def test_a_window_of_three_carries_three_messages(self):
        broker = BrokerActor()
        sub, _pub, _pids = await _three_unacked(broker)
        assert [p.payload for p in sub.publishes()] == [b'\x00', b'\x01', b'\x02']
        assert not broker._sessions['c1']['outbound_queue']

    async def test_reconnect_lowering_the_limit_replays_within_it(self):
        broker = BrokerActor()
        sub, _pub, pids = await _three_unacked(broker)
        await _detach(broker, sub)

        conn = RecordingConn()
        await _attach(broker, conn, clean_start=False, receive_maximum=1)

        replayed = conn.publishes()
        assert [p.packet_id for p in replayed] == [pids[0]], (
            f'replayed {len(replayed)} PUBLISH packets into a window of 1')
        assert all(p.dup for p in replayed)

    async def test_reconnect_raising_the_limit_replays_more(self):
        broker = BrokerActor()
        sub, _pub, pids = await _three_unacked(broker)
        await _detach(broker, sub)
        low = RecordingConn()
        await _attach(broker, low, clean_start=False, receive_maximum=1)
        assert [p.packet_id for p in low.publishes()] == [pids[0]]
        await _detach(broker, low)

        conn = RecordingConn()
        await _attach(broker, conn, clean_start=False, receive_maximum=3)

        assert [p.packet_id for p in conn.publishes()] == pids

    async def test_reconnect_omitting_the_limit_applies_the_default(self):
        broker = BrokerActor()
        sub, pub, pids = await _three_unacked(broker)
        await _detach(broker, sub)
        low = RecordingConn()
        await _attach(broker, low, clean_start=False, receive_maximum=1)
        assert [p.packet_id for p in low.publishes()] == [pids[0]]
        await _detach(broker, low)

        conn = RecordingConn()
        await _attach(broker, conn, clean_start=False)

        assert [p.packet_id for p in conn.publishes()] == pids, (
            'a window of 65535 still replayed as if the stored limit of 1 '
            'were in force')

    async def test_a_declared_zero_is_a_protocol_error(self):
        broker = BrokerActor()
        conn = RecordingConn()
        await _attach(broker, conn, receive_maximum=0)

        acks = [p for p in conn.packets() if isinstance(p, MQTTConnack)]
        assert acks and acks[0].reason_code == ReasonCode.PROTOCOL_ERROR, (
            'a declared Receive Maximum of 0 was answered as success')
        assert not broker._sessions

    async def test_the_default_window_stays_open_for_live_delivery(self):
        broker = BrokerActor()
        sub, pub, _pids = await _three_unacked(broker)
        await _detach(broker, sub)
        low = RecordingConn()
        await _attach(broker, low, clean_start=False, receive_maximum=1)
        await _detach(broker, low)
        conn = RecordingConn()
        await _attach(broker, conn, clean_start=False)
        conn.outbox.clear()

        for i in range(3):
            await _publish(broker, pub, payload=bytes([i]), packet_id=10 + i)

        assert [p.payload for p in conn.publishes()] == \
            [b'\x00', b'\x01', b'\x02'], (
            'an omitted Receive Maximum left the previous connection\'s '
            'window in force')


class TestReplayAdvancesOnAck:
    async def test_an_ack_advances_the_next_replay(self):
        broker = BrokerActor()
        sub, _pub, pids = await _three_unacked(broker)
        await _detach(broker, sub)
        conn = RecordingConn()
        await _attach(broker, conn, clean_start=False, receive_maximum=1)

        for index, pid in enumerate(pids):
            if index:
                await broker._handle(ClientPuback(packet_id=pids[index - 1],
                                                  sender=conn))
            assert [p.packet_id for p in conn.publishes()] == pids[:index + 1]
        await broker._handle(ClientPuback(packet_id=pids[-1], sender=conn))
        assert [p.packet_id for p in conn.publishes()] == pids
        assert all(p.dup for p in conn.publishes())

    async def test_replays_outrank_the_held_queue(self):
        broker = BrokerActor()
        sub, pub, pids = await _three_unacked(broker)
        await _detach(broker, sub)
        conn = RecordingConn()
        await _attach(broker, conn, clean_start=False, receive_maximum=1)
        await _publish(broker, pub, payload=b'new', packet_id=9)

        assert [p.payload for p in conn.publishes()] == [b'\x00']
        for pid in pids:
            await broker._handle(ClientPuback(packet_id=pid, sender=conn))
        assert [p.payload for p in conn.publishes()] == \
            [b'\x00', b'\x01', b'\x02', b'new']
        assert [p.dup for p in conn.publishes()] == [True, True, True, False]

    async def test_held_messages_follow_the_replays_when_the_window_reopens(self):
        broker = BrokerActor()
        sub, pub = RecordingConn(), RecordingConn()
        await _attach(broker, sub, receive_maximum=1)
        await _subscribe(broker, sub, qos=1)
        await _attach(broker, pub, client_id='pub')
        for i in range(3):
            await _publish(broker, pub, payload=bytes([i]), packet_id=i + 1)
        await _detach(broker, sub)

        conn = RecordingConn()
        await _attach(broker, conn, clean_start=False)

        assert [p.payload for p in conn.publishes()] == \
            [b'\x00', b'\x01', b'\x02']
        assert [p.dup for p in conn.publishes()] == [True, False, False]

    async def test_an_ack_of_an_unsent_message_does_not_widen_the_window(self):
        broker = BrokerActor()
        sub, pub, pids = await _three_unacked(broker, receive_maximum=4)
        await _publish(broker, pub, payload=b'four', packet_id=4)
        pids = [p.packet_id for p in sub.publishes()]
        await _detach(broker, sub)
        conn = RecordingConn()
        await _attach(broker, conn, clean_start=False, receive_maximum=1)
        assert [p.packet_id for p in conn.publishes()] == [pids[0]]

        await broker._handle(ClientPuback(packet_id=pids[2], sender=conn))
        await broker._handle(ClientPuback(packet_id=pids[3], sender=conn))

        assert [p.packet_id for p in conn.publishes()] == [pids[0]], (
            'acknowledgements of messages this connection never sent '
            'widened its window')
        await broker._handle(ClientPuback(packet_id=pids[0], sender=conn))
        assert [p.packet_id for p in conn.publishes()] == pids[:2]


class TestReplayOrder:
    async def test_replays_keep_the_order_the_promises_were_made_in(self):
        """MQTT-4.4.0-2 — in the order the originals were sent, QoS 1 and 2."""
        broker = BrokerActor()
        sub, pub = RecordingConn(), RecordingConn()
        await _attach(broker, sub, receive_maximum=5)
        await _subscribe(broker, sub, qos=2)
        await _attach(broker, pub, client_id='pub')
        await _publish(broker, pub, payload=b'qos2', qos=2, packet_id=1)
        await _publish(broker, pub, payload=b'qos1', qos=1, packet_id=2)
        assert [p.payload for p in sub.publishes()] == [b'qos2', b'qos1']
        await _detach(broker, sub)

        conn = RecordingConn()
        await _attach(broker, conn, clean_start=False)

        assert [(p.payload, p.dup) for p in conn.publishes()] == \
            [(b'qos2', True), (b'qos1', True)]

    async def test_a_full_window_holds_the_newer_message_whatever_its_qos(self):
        broker = BrokerActor()
        sub, pub = RecordingConn(), RecordingConn()
        await _attach(broker, sub, receive_maximum=5)
        await _subscribe(broker, sub, qos=2)
        await _attach(broker, pub, client_id='pub')
        await _publish(broker, pub, payload=b'qos2', qos=2, packet_id=1)
        await _publish(broker, pub, payload=b'qos1', qos=1, packet_id=2)
        await _detach(broker, sub)

        conn = RecordingConn()
        await _attach(broker, conn, clean_start=False, receive_maximum=1)
        assert [p.payload for p in conn.publishes()] == [b'qos2']

        first = conn.publishes()[0]
        await broker._handle(ClientPubrec(packet_id=first.packet_id, sender=conn))
        await broker._handle(ClientPubcomp(packet_id=first.packet_id, sender=conn))
        assert [p.payload for p in conn.publishes()] == [b'qos2', b'qos1']


class TestReplayAcrossMixedQoS:
    async def test_mixed_replays_share_the_window_and_pubrel_never_waits(self):
        broker = BrokerActor()
        sub, pub = RecordingConn(), RecordingConn()
        await _attach(broker, sub, receive_maximum=3)
        await _subscribe(broker, sub, qos=2)
        await _attach(broker, pub, client_id='pub')
        await _publish(broker, pub, payload=b'q1', qos=1, packet_id=1)
        await _publish(broker, pub, payload=b'q2a', qos=2, packet_id=2)
        await _publish(broker, pub, payload=b'q2b', qos=2, packet_id=3)
        by_payload = {p.payload: p.packet_id for p in sub.publishes()}
        await broker._handle(ClientPubrec(
            packet_id=by_payload[b'q2b'], sender=sub))
        await _detach(broker, sub)

        conn = RecordingConn()
        await _attach(broker, conn, clean_start=False, receive_maximum=1)

        # PUBREL is a control packet: it goes at once, and the one window slot
        # is spent on the oldest un-sent PUBLISH instead.
        assert [(p.packet_id, p.dup) for p in conn.publishes()] == \
            [(by_payload[b'q1'], True)]
        assert [p.packet_id for p in conn.packets()
                if isinstance(p, MQTTPubrel)] == [by_payload[b'q2b']]

        await broker._handle(ClientPuback(packet_id=by_payload[b'q1'],
                                          sender=conn))
        assert [(p.packet_id, p.dup) for p in conn.publishes()] == \
            [(by_payload[b'q1'], True), (by_payload[b'q2a'], True)]

        # A QoS 2 answer is control too — the window is still full here.
        await broker._handle(ClientPubrec(packet_id=by_payload[b'q2a'],
                                          sender=conn))
        assert [p.packet_id for p in conn.packets()
                if isinstance(p, MQTTPubrel)] == \
            [by_payload[b'q2b'], by_payload[b'q2a']]

    async def test_a_rejected_publish_stops_charging_the_window(self):
        broker = BrokerActor()
        sub, pub = RecordingConn(), RecordingConn()
        await _attach(broker, sub, receive_maximum=1)
        await _subscribe(broker, sub, qos=2)
        await _attach(broker, pub, client_id='pub')
        await _publish(broker, pub, payload=b'a', qos=2, packet_id=1)
        first = sub.publishes()[0]

        await broker._handle(ClientPubrec(
            packet_id=first.packet_id, reason_code=ReasonCode.QUOTA_EXCEEDED,
            sender=sub))
        await _publish(broker, pub, payload=b'b', qos=2, packet_id=2)

        assert [p.payload for p in sub.publishes()] == [b'a', b'b'], (
            'a PUBLISH the client rejected still charges the window')
        assert not any(isinstance(p, MQTTPubrel) for p in sub.packets()), (
            'the exchange ends at the rejected PUBREC (§2.2.1); no PUBREL')
        assert first.packet_id not in \
            broker._sessions['c1']['pending_qos2_out'], (
            'a rejected PUBLISH must release its packet identifier')

    async def test_a_rejected_publish_is_not_replayed(self):
        """MQTT-4.4.0-2 — the rejected PUBLISH is acknowledged, so a resumed
        session owes neither the PUBLISH nor a PUBREL for it."""
        broker = BrokerActor()
        sub, pub = RecordingConn(), RecordingConn()
        await _attach(broker, sub, receive_maximum=1)
        await _subscribe(broker, sub, qos=2)
        await _attach(broker, pub, client_id='pub')
        await _publish(broker, pub, payload=b'a', qos=2, packet_id=1)
        await broker._handle(ClientPubrec(
            packet_id=sub.publishes()[0].packet_id,
            reason_code=ReasonCode.QUOTA_EXCEEDED, sender=sub))
        await _detach(broker, sub)

        conn = RecordingConn()
        await _attach(broker, conn, clean_start=False)
        assert not [p for p in conn.packets()
                    if isinstance(p, (MQTTPublish, MQTTPubrel))], (
            'a rejected PUBLISH was re-driven on reconnect')

    async def test_the_window_opens_when_the_qos2_flow_completes(self):
        broker = BrokerActor()
        sub, pub = RecordingConn(), RecordingConn()
        await _attach(broker, sub, receive_maximum=1)
        await _subscribe(broker, sub, qos=2)
        await _attach(broker, pub, client_id='pub')
        await _publish(broker, pub, payload=b'a', qos=2, packet_id=1)
        first = sub.publishes()[0]
        await broker._handle(ClientPubrec(packet_id=first.packet_id, sender=sub))
        await _publish(broker, pub, payload=b'b', qos=2, packet_id=2)
        assert len(sub.publishes()) == 1, (
            'a message awaiting PUBCOMP still holds the window')

        await broker._handle(ClientPubcomp(packet_id=first.packet_id, sender=sub))
        assert [p.payload for p in sub.publishes()] == [b'a', b'b']


class TestEveryDeliveryPathSharesTheWindow:
    async def test_retained_delivery_waits_for_a_free_slot(self):
        broker = BrokerActor()
        broker._store_retained(MQTTPublish(
            topic='t', payload=b'r', qos=1, packet_id=1, retain=True))
        sub, pub = RecordingConn(), RecordingConn()
        await _attach(broker, sub, receive_maximum=1)
        await _subscribe(broker, sub, topic='other', qos=1)
        await _attach(broker, pub, client_id='pub')
        await _publish(broker, pub, topic='other', payload=b'live', qos=1,
                       packet_id=1)
        assert [p.payload for p in sub.publishes()] == [b'live']

        await _subscribe(broker, sub, topic='t', qos=1)

        assert [p.payload for p in sub.publishes()] == [b'live'], (
            'a retained message jumped a full window')
        await broker._handle(ClientPuback(
            packet_id=sub.publishes()[0].packet_id, sender=sub))
        assert [p.payload for p in sub.publishes()] == [b'live', b'r']

    async def test_a_shared_delivery_is_held_for_a_full_window_not_lost(self):
        broker = BrokerActor()
        first, second, pub = (RecordingConn(), RecordingConn(), RecordingConn())
        await _attach(broker, first, client_id='a', receive_maximum=1)
        await _subscribe(broker, first, topic='fill', qos=1)
        await _subscribe(broker, first, topic='$share/g/t', qos=1)
        await _attach(broker, second, client_id='b')
        await _subscribe(broker, second, topic='$share/g/t', qos=1)
        await _attach(broker, pub, client_id='pub')
        await _publish(broker, pub, topic='fill', payload=b'fill', qos=1,
                       packet_id=1)

        await _publish(broker, pub, topic='t', payload=b'shared', qos=1,
                       packet_id=2)

        assert [p.payload for p in second.publishes()] == [], (
            'the group got a second copy for a member that was only full')
        assert [p.payload for p in first.publishes()] == [b'fill']
        session = broker._sessions['a']
        assert [h.publish.payload for h in session['outbound_queue']] == \
            [b'shared']
        await broker._handle(ClientPuback(
            packet_id=first.publishes()[0].packet_id, sender=first))
        assert [p.payload for p in first.publishes()] == [b'fill', b'shared']


class TestControlNeverWaits:
    async def test_acknowledgements_and_pubrel_flow_at_quota_zero(self):
        broker = BrokerActor()
        sub, pub = RecordingConn(), RecordingConn()
        await _attach(broker, sub, receive_maximum=1)
        await _subscribe(broker, sub, qos=2)
        await _attach(broker, pub, client_id='pub')
        await _publish(broker, pub, payload=b'out', qos=2, packet_id=1)
        outbound = sub.publishes()[0]
        assert len(sub.publishes()) == 1  # window full

        await _publish(broker, sub, payload=b'in1', qos=1, packet_id=7,
                       topic='in')
        await _publish(broker, sub, payload=b'in2', qos=2, packet_id=8,
                       topic='in')
        kinds = [type(p).__name__ for p in sub.packets()]
        assert 'MQTTPuback' in kinds and 'MQTTPubrec' in kinds, (
            f'inbound acknowledgements stopped at quota zero: {kinds}')

        await broker._handle(ClientPubrel(packet_id=8, sender=sub))
        await broker._handle(ClientPubrec(packet_id=outbound.packet_id,
                                          sender=sub))
        await broker._handle(ClientPing(sender=sub))
        kinds = [type(p).__name__ for p in sub.packets()]
        assert {'MQTTPubcomp', 'MQTTPubrel', 'MQTTPingresp'} <= set(kinds), (
            f'PUBREL/PUBCOMP/PINGRESP stopped at quota zero: {kinds}')

        await broker._handle(ClientProtocolError(sender=sub))
        kinds = [type(p).__name__ for p in sub.packets()]
        assert 'MQTTDisconnect' in kinds, (
            f'teardown stopped at quota zero: {kinds}')


class TestStateSurvivesTheConnection:
    async def test_a_disconnect_midway_keeps_packet_identifiers_and_dup(self):
        broker = BrokerActor()
        sub, _pub, pids = await _three_unacked(broker)
        await _detach(broker, sub)
        low = RecordingConn()
        await _attach(broker, low, clean_start=False, receive_maximum=1)
        assert [p.packet_id for p in low.publishes()] == [pids[0]]
        await _detach(broker, low)  # connection dies mid-replay, no acks

        conn = RecordingConn()
        await _attach(broker, conn, clean_start=False)

        assert [p.packet_id for p in conn.publishes()] == pids
        assert all(p.dup for p in conn.publishes())

    async def test_an_ack_before_the_disconnect_is_not_replayed(self):
        broker = BrokerActor()
        sub, pub = RecordingConn(), RecordingConn()
        await _attach(broker, sub, receive_maximum=3)
        await _subscribe(broker, sub, qos=2)
        await _attach(broker, pub, client_id='pub')
        await _publish(broker, pub, payload=b'qos2', qos=2, packet_id=1)
        await _publish(broker, pub, payload=b'ack', qos=1, packet_id=2)
        await _publish(broker, pub, payload=b'qos1', qos=1, packet_id=3)
        sent = {p.payload: p.packet_id for p in sub.publishes()}
        await broker._handle(ClientPuback(packet_id=sent[b'ack'], sender=sub))
        await _detach(broker, sub)

        conn = RecordingConn()
        await _attach(broker, conn, clean_start=False)

        # Bucket order would replay the QoS 1 first; the promises were made
        # the other way round, and the acknowledged one not at all.
        assert [(p.payload, p.dup) for p in conn.publishes()] == \
            [(b'qos2', True), (b'qos1', True)]
