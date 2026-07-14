"""Behaviour-level tests for the Sprint 70 MQTT-broker hardening cluster (1.19).

The pre-existing MQTT suite asserted mostly *codec round-trips* — which is
exactly why the broker-behaviour gaps in this cluster (keep-alive enforcement,
QoS-2 dedup, session takeover, No Local, …) stayed hidden.  Every test here
drives real broker/connection behaviour, not encode/decode symmetry.
"""
import asyncio
import contextlib

import pytest

from blackbull.actor import Actor
from blackbull.mqtt.broker import (
    BrokerActor,
    Attach, ClientSubscribe, ClientPublish, ClientPubrec, Detach, Send, Close,
    _new_broker_session,
)
from blackbull.mqtt.connection import MQTT5Actor, PacketFramer
from blackbull.mqtt.messages import (
    MQTTConnect, MQTTPublish, MQTTPuback, MQTTPubrec, MQTTPubrel,
    MQTTSubscribe, MQTTSuback, MQTTDisconnect, MQTTDecodeError,
    ReasonCode, decode_packet,
)
from blackbull.server.protocol_registry import ProtocolContext
from blackbull.server.recipient import AbstractReader
from blackbull.server.sender import AbstractWriter

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


def _connect(client_id='c1', clean_start=True, **kw):
    return MQTTConnect(client_id=client_id, clean_start=clean_start,
                       keep_alive=kw.pop('keep_alive', 60), **kw)


async def _attach(broker, conn, **kw):
    await broker._handle(Attach(connect=_connect(**kw), sender=conn))


async def _subscribe(broker, conn, packet_id, subs, options=None):
    await broker._handle(ClientSubscribe(
        subscribe=MQTTSubscribe(packet_id=packet_id, subscriptions=subs,
                                subscription_options=options),
        sender=conn))


# ===========================================================================
# 1.19g — malformed CONNECT decodes to MQTTDecodeError, not IndexError
# ===========================================================================

class TestConnectDecodeBounds:
    async def test_connect_truncated_after_protocol_name_is_malformed(self):
        """A CONNECT whose Remaining Length covers only the protocol name must
        raise MQTTDecodeError (which the framer resyncs on), not an IndexError
        that unwinds the read loop."""
        # 0x10 = CONNECT, RL=6, body = 2-byte-len-prefixed "MQTT" only.
        packet = b'\x10\x06\x00\x04MQTT'
        with pytest.raises(MQTTDecodeError):
            decode_packet(packet)

    async def test_framer_resyncs_past_a_truncated_connect(self):
        """The framer drops the malformed CONNECT byte-by-byte and does not
        raise IncompletePacket forever (which would stall the connection)."""
        framer = PacketFramer()
        framer.feed(b'\x10\x06\x00\x04MQTT')
        # Draining the framer must terminate without raising.
        assert list(framer) == []


# ===========================================================================
# 1.19d — topic validation on PUBLISH / SUBSCRIBE
# ===========================================================================

class TestTopicValidation:
    async def test_publish_wildcard_topic_qos1_rejected_and_not_routed(self):
        broker = BrokerActor()
        sub, pub = RecordingConn(), RecordingConn()
        await _attach(broker, sub, client_id='sub')
        await _subscribe(broker, sub, 1, [('a/#', 1)])
        sub.outbox.clear()
        await _attach(broker, pub, client_id='pub')
        # A Topic *Name* with a wildcard is illegal (§3.3.2.1).
        await broker._handle(ClientPublish(
            publish=MQTTPublish(topic='a/#', payload=b'x', qos=1, packet_id=9),
            sender=pub))
        pubacks = [p for p in pub.packets() if isinstance(p, MQTTPuback)]
        assert pubacks and pubacks[0].reason_code == ReasonCode.TOPIC_NAME_INVALID
        # Not routed to the subscriber, and not retained.
        assert not [p for p in sub.packets() if isinstance(p, MQTTPublish)]
        assert broker._retained == {}

    async def test_publish_invalid_topic_qos0_disconnects(self):
        broker, pub = BrokerActor(), RecordingConn()
        await _attach(broker, pub, client_id='pub')
        pub.outbox.clear()
        await broker._handle(ClientPublish(
            publish=MQTTPublish(topic='bad/+', payload=b'x', qos=0), sender=pub))
        # QoS 0 has no ack channel — reject via DISCONNECT + Close (§4.13).
        assert any(isinstance(m, Send) and isinstance(m.packet, MQTTDisconnect)
                   and m.packet.reason_code == ReasonCode.TOPIC_NAME_INVALID
                   for m in pub.outbox)
        assert any(isinstance(m, Close) for m in pub.outbox)

    async def test_subscribe_invalid_filter_gets_0x8f(self):
        broker, conn = BrokerActor(), RecordingConn()
        await _attach(broker, conn)
        # '#' not terminal — an invalid Topic Filter (§4.7.1.2).
        await _subscribe(broker, conn, 5, [('a/#/b', 0)])
        suback = [p for p in conn.packets() if isinstance(p, MQTTSuback)][0]
        assert suback.reason_codes == [ReasonCode.TOPIC_FILTER_INVALID]
        assert broker._sessions['c1']['subscriptions'] == []

    async def test_subscribe_mixed_valid_and_invalid_keeps_ordering(self):
        broker, conn = BrokerActor(), RecordingConn()
        await _attach(broker, conn)
        await _subscribe(broker, conn, 6, [('ok/1', 1), ('bad/#/x', 2), ('ok/2', 0)])
        suback = [p for p in conn.packets() if isinstance(p, MQTTSuback)][0]
        assert suback.reason_codes == [1, ReasonCode.TOPIC_FILTER_INVALID, 0]


# ===========================================================================
# 1.19c — QoS 2 inbound duplicate PUBLISH is not re-delivered
# ===========================================================================

class TestQoS2InboundDedup:
    async def test_duplicate_qos2_publish_reacks_but_delivers_once(self):
        broker = BrokerActor()
        sub, pub = RecordingConn(), RecordingConn()
        await _attach(broker, sub, client_id='sub')
        await _subscribe(broker, sub, 1, [('t', 2)])
        sub.outbox.clear()
        await _attach(broker, pub, client_id='pub')

        publish = MQTTPublish(topic='t', payload=b'x', qos=2, packet_id=7)
        await broker._handle(ClientPublish(publish=publish, sender=pub))
        # Retransmit (DUP) of the same id before PUBREL.
        await broker._handle(ClientPublish(
            publish=MQTTPublish(topic='t', payload=b'x', qos=2, packet_id=7, dup=True),
            sender=pub))

        # PUBREC re-sent both times ...
        assert len([p for p in pub.packets() if isinstance(p, MQTTPubrec)]) == 2
        # ... but the subscriber only ever saw the message once.
        assert len([p for p in sub.packets() if isinstance(p, MQTTPublish)]) == 1


# ===========================================================================
# 1.19f — reconnect replay of unacked outbound QoS 1/2 with DUP=1
# ===========================================================================

class TestReconnectReplay:
    async def test_qos1_replayed_with_dup_flag(self):
        broker = BrokerActor()
        broker._sessions['c1'] = _new_broker_session()
        broker._sessions['c1']['_expiry'] = 3600
        broker._sessions['c1']['pending_qos1_out'][5] = MQTTPublish(
            topic='alerts', payload=b'!', qos=1, packet_id=5)
        conn = RecordingConn()
        await _attach(broker, conn, client_id='c1', clean_start=False)
        replayed = [p for p in conn.packets() if isinstance(p, MQTTPublish)]
        assert len(replayed) == 1 and replayed[0].packet_id == 5
        assert replayed[0].dup is True

    async def test_qos2_publish_stage_replayed_with_dup(self):
        broker = BrokerActor()
        session = _new_broker_session()
        session['_expiry'] = 3600
        session['pending_qos2_out'][9] = {
            'state': 'PUBLISH_SENT',
            'packet': MQTTPublish(topic='m', payload=b'p', qos=2, packet_id=9)}
        broker._sessions['c1'] = session
        conn = RecordingConn()
        await _attach(broker, conn, client_id='c1', clean_start=False)
        replayed = [p for p in conn.packets() if isinstance(p, MQTTPublish)]
        assert len(replayed) == 1 and replayed[0].packet_id == 9
        assert replayed[0].dup is True

    async def test_qos2_pubrel_stage_replays_pubrel(self):
        broker = BrokerActor()
        session = _new_broker_session()
        session['_expiry'] = 3600
        session['pending_qos2_out'][11] = {'state': 'PUBREL_SENT'}
        broker._sessions['c1'] = session
        conn = RecordingConn()
        await _attach(broker, conn, client_id='c1', clean_start=False)
        pubrels = [p for p in conn.packets() if isinstance(p, MQTTPubrel)]
        assert len(pubrels) == 1 and pubrels[0].packet_id == 11

    async def test_outbound_qos2_keeps_publish_for_replay(self):
        """A delivered QoS 2 PUBLISH is stored with its packet so it can be
        replayed; after PUBREC it advances to PUBREL_SENT and drops the packet."""
        broker = BrokerActor()
        sub, pub = RecordingConn(), RecordingConn()
        await _attach(broker, sub, client_id='sub')
        await _subscribe(broker, sub, 1, [('t', 2)])
        await _attach(broker, pub, client_id='pub')
        await broker._handle(ClientPublish(
            publish=MQTTPublish(topic='t', payload=b'x', qos=2, packet_id=3),
            sender=pub))
        out = broker._sessions['sub']['pending_qos2_out']
        (pid, entry), = out.items()
        assert entry['state'] == 'PUBLISH_SENT'
        assert isinstance(entry['packet'], MQTTPublish)
        # Subscriber acks with PUBREC → advance to PUBREL, packet no longer kept.
        await broker._handle(ClientPubrec(packet_id=pid, sender=sub))
        assert out[pid]['state'] == 'PUBREL_SENT'
        assert out[pid].get('packet') is None


# ===========================================================================
# 1.19b — session takeover
# ===========================================================================

class TestSessionTakeover:
    async def test_second_connect_evicts_prior_connection(self):
        broker = BrokerActor()
        first, second = RecordingConn(), RecordingConn()
        await _attach(broker, first, client_id='dup')
        first.outbox.clear()
        await _attach(broker, second, client_id='dup')

        # The prior connection is told to disconnect (0x8E) then closed.
        assert any(isinstance(m, Send) and isinstance(m.packet, MQTTDisconnect)
                   and m.packet.reason_code == ReasonCode.SESSION_TAKEN_OVER
                   for m in first.outbox)
        assert any(isinstance(m, Close) for m in first.outbox)
        # The registry now points at the new connection.
        assert broker._clients['dup'] is second

    async def test_taken_over_connection_detach_is_noop(self):
        """The old connection's teardown Detach must not fire the (new) Will or
        disturb the taken-over session — its id mapping is already gone."""
        broker = BrokerActor()
        first, second, watcher = RecordingConn(), RecordingConn(), RecordingConn()
        await _attach(broker, watcher, client_id='watch')
        await _subscribe(broker, watcher, 1, [('will/t', 0)])
        watcher.outbox.clear()
        await _attach(broker, first, client_id='dup',
                      will_topic='will/t', will_payload=b'bye')
        await _attach(broker, second, client_id='dup')
        # Old connection tears down abnormally after being taken over.
        await broker._handle(Detach(graceful=False, sender=first))
        assert not [p for p in watcher.packets() if isinstance(p, MQTTPublish)]
        assert broker._clients['dup'] is second


# ===========================================================================
# 1.19e — No Local, Retain As Published, and $share rejection
# ===========================================================================

class TestSubscriptionOptions:
    async def test_no_local_does_not_echo_own_publish(self):
        broker, conn = BrokerActor(), RecordingConn()
        await _attach(broker, conn, client_id='self')
        await _subscribe(broker, conn, 1, [('t', 0)], options=[{'no_local': True}])
        conn.outbox.clear()
        await broker._handle(ClientPublish(
            publish=MQTTPublish(topic='t', payload=b'x', qos=0), sender=conn))
        assert not [p for p in conn.packets() if isinstance(p, MQTTPublish)]

    async def test_no_local_still_delivers_from_other_client(self):
        broker = BrokerActor()
        sub, pub = RecordingConn(), RecordingConn()
        await _attach(broker, sub, client_id='sub')
        await _subscribe(broker, sub, 1, [('t', 0)], options=[{'no_local': True}])
        sub.outbox.clear()
        await _attach(broker, pub, client_id='pub')
        await broker._handle(ClientPublish(
            publish=MQTTPublish(topic='t', payload=b'y', qos=0), sender=pub))
        assert [p for p in sub.packets() if isinstance(p, MQTTPublish)]

    async def test_retain_as_published_preserves_retain_flag(self):
        broker = BrokerActor()
        sub, pub = RecordingConn(), RecordingConn()
        await _attach(broker, sub, client_id='sub')
        await _subscribe(broker, sub, 1, [('t', 0)],
                         options=[{'retain_as_published': True}])
        sub.outbox.clear()
        await _attach(broker, pub, client_id='pub')
        await broker._handle(ClientPublish(
            publish=MQTTPublish(topic='t', payload=b'z', qos=0, retain=True),
            sender=pub))
        delivered = [p for p in sub.packets() if isinstance(p, MQTTPublish)][0]
        assert delivered.retain is True

    async def test_without_rap_retain_flag_cleared_on_forward(self):
        broker = BrokerActor()
        sub, pub = RecordingConn(), RecordingConn()
        await _attach(broker, sub, client_id='sub')
        await _subscribe(broker, sub, 1, [('t', 0)])  # no RAP
        sub.outbox.clear()
        await _attach(broker, pub, client_id='pub')
        await broker._handle(ClientPublish(
            publish=MQTTPublish(topic='t', payload=b'z', qos=0, retain=True),
            sender=pub))
        delivered = [p for p in sub.packets() if isinstance(p, MQTTPublish)][0]
        assert delivered.retain is False

    async def test_shared_subscription_rejected(self):
        broker, conn = BrokerActor(), RecordingConn()
        await _attach(broker, conn)
        await _subscribe(broker, conn, 1, [('$share/g/t', 0)])
        suback = [p for p in conn.packets() if isinstance(p, MQTTSuback)][0]
        assert suback.reason_codes == [
            ReasonCode.SHARED_SUBSCRIPTIONS_NOT_SUPPORTED]
        assert broker._sessions['c1']['subscriptions'] == []


# ===========================================================================
# 1.19h — packet-id allocation skips in-flight ids
# ===========================================================================

class TestPacketIdAllocation:
    async def test_alloc_pid_skips_in_flight_ids(self):
        session = _new_broker_session()
        session['pending_qos1_out'] = {1: object(), 2: object()}
        session['pending_qos2_out'] = {3: {'state': 'PUBLISH_SENT'}}
        session['_next_pid'] = 0
        assert BrokerActor._alloc_pid(session) == 4  # 1,2,3 all in use

    async def test_alloc_pid_wraps_and_skips(self):
        session = _new_broker_session()
        session['pending_qos1_out'] = {1: object()}
        session['_next_pid'] = 65534
        assert BrokerActor._alloc_pid(session) == 65535
        # Next wraps to 1, which is in use → 2.
        assert BrokerActor._alloc_pid(session) == 2


# ===========================================================================
# 1.19a — keep-alive idle timeout fires the Will and closes
# ===========================================================================

class _SilentReader(AbstractReader):
    """Never yields data and never reports EOF — a peer gone silent."""

    async def read(self, n: int) -> bytes:
        await asyncio.sleep(0.005)
        return b''

    def at_eof(self) -> bool:
        return False


class _RecordingBroker(BrokerActor):
    """A real BrokerActor (satisfies the isinstance hint) that records inbound
    messages instead of enqueueing them, so the test can inspect the Detach."""

    def __init__(self):
        super().__init__()
        self.inbox = []

    async def send(self, msg) -> None:
        self.inbox.append(msg)


class _NullWriter(AbstractWriter):
    async def write(self, data: bytes) -> None:
        pass

    async def drain(self) -> None:
        pass


def _ctx():
    return ProtocolContext(peername=('127.0.0.1', 5), sockname=('0.0.0.0', 1883),
                           ssl=False, aggregator=None, connection_id='c',
                           protocol='mqtt')


class TestKeepAliveTimeout:
    async def test_silent_peer_past_deadline_detaches_abnormally(self):
        broker = _RecordingBroker()
        conn = MQTT5Actor(_NullWriter(), broker, _ctx())
        # Sub-second interval so the 1.5× deadline (0.075 s) elapses fast.
        conn._keep_alive = 0.05
        await asyncio.wait_for(conn.read_loop(_SilentReader()), timeout=2.0)
        assert conn._done is True
        detaches = [m for m in broker.inbox if isinstance(m, Detach)]
        assert detaches and detaches[-1].graceful is False

    async def test_keepalive_zero_never_times_out(self):
        broker = _RecordingBroker()
        conn = MQTT5Actor(_NullWriter(), broker, _ctx())
        conn._keep_alive = 0  # disabled (§3.1.2.10)
        with pytest.raises(asyncio.TimeoutError):
            # No deadline → the loop idles forever; the test timeout proves it
            # never self-terminates on keep-alive.
            await asyncio.wait_for(conn.read_loop(_SilentReader()), timeout=0.3)
        assert conn._done is False
