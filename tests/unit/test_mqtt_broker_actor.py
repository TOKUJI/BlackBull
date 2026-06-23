"""Isolated unit tests for the MQTT ``BrokerActor`` (Sprint 53, Phase 1).

These drive ``BrokerActor._handle`` directly with recording connection actors,
so the broker's routing/session/retained/Will logic is verified without sockets,
the reader loop, or the connection actor.  The wire-level conformance suite
(driven through the harness seam) remains the end-to-end oracle.
"""
import pytest

from blackbull.actor import Actor
from blackbull.mqtt.actor import (
    BrokerActor,
    Attach, ClientSubscribe, ClientUnsubscribe, ClientPublish,
    ClientPubrel, ClientPubrec, ClientPuback,
    Detach, Send, Close,
    _new_broker_session,
)
from blackbull.mqtt.messages import (
    MQTTConnect, MQTTConnack, MQTTPublish, MQTTPuback, MQTTPubrec,
    MQTTPubcomp, MQTTSubscribe, MQTTSuback, MQTTUnsubscribe, MQTTUnsuback,
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


def _connect(client_id='c1', clean_start=True, **kw):
    return MQTTConnect(client_id=client_id, clean_start=clean_start,
                       keep_alive=kw.pop('keep_alive', 60), **kw)


async def _attach(broker, conn, **kw):
    await broker._handle(Attach(connect=_connect(**kw), sender=conn))


# --------------------------------------------------------------------------

async def test_connect_replies_connack_success():
    broker, conn = BrokerActor(), RecordingConn()
    await _attach(broker, conn, client_id='c1')
    pkts = conn.packets()
    assert len(pkts) == 1
    assert isinstance(pkts[0], MQTTConnack)
    assert pkts[0].reason_code == 0x00
    assert pkts[0].session_present is False


async def test_connect_unsupported_version_rejected_and_closed():
    broker, conn = BrokerActor(), RecordingConn()
    await broker._handle(Attach(
        connect=MQTTConnect(client_id='c', clean_start=True, keep_alive=0,
                            proto_level=4),
        sender=conn))
    # CONNACK 0x84 then Close
    assert isinstance(conn.outbox[0], Send)
    assert conn.outbox[0].packet.reason_code == 0x84
    assert isinstance(conn.outbox[1], Close)


async def test_subscribe_acked_with_granted_qos():
    broker, conn = BrokerActor(), RecordingConn()
    await _attach(broker, conn)
    await broker._handle(ClientSubscribe(
        subscribe=MQTTSubscribe(packet_id=10, subscriptions=[('sensors/+/temp', 1)]),
        sender=conn))
    subacks = [p for p in conn.packets() if isinstance(p, MQTTSuback)]
    assert len(subacks) == 1
    assert subacks[0].packet_id == 10
    assert subacks[0].reason_codes == [1]


async def test_publish_routed_to_subscriber_and_publisher_acked():
    from blackbull.mqtt.messages import MQTTSubscribe
    broker = BrokerActor()
    sub, pub = RecordingConn(), RecordingConn()

    await _attach(broker, sub, client_id='sub')
    await broker._handle(ClientSubscribe(
        subscribe=MQTTSubscribe(packet_id=1, subscriptions=[('chat/general', 1)]),
        sender=sub))
    sub.outbox.clear()

    await _attach(broker, pub, client_id='pub')
    await broker._handle(ClientPublish(
        publish=MQTTPublish(topic='chat/general', payload=b'hi', qos=1,
                            packet_id=100),
        sender=pub))

    # Subscriber received the PUBLISH
    sub_pubs = [p for p in sub.packets() if isinstance(p, MQTTPublish)]
    assert len(sub_pubs) == 1
    assert sub_pubs[0].topic == 'chat/general'
    assert sub_pubs[0].payload == b'hi'
    # Publisher got its PUBACK
    pubacks = [p for p in pub.packets() if isinstance(p, MQTTPuback)]
    assert pubacks and pubacks[0].packet_id == 100


async def test_retained_delivered_on_late_subscribe():
    from blackbull.mqtt.messages import MQTTSubscribe
    broker = BrokerActor()
    pub = RecordingConn()
    await _attach(broker, pub, client_id='pub')
    await broker._handle(ClientPublish(
        publish=MQTTPublish(topic='status/server', payload=b'up', qos=0,
                            retain=True),
        sender=pub))

    sub = RecordingConn()
    await _attach(broker, sub, client_id='sub')
    await broker._handle(ClientSubscribe(
        subscribe=MQTTSubscribe(packet_id=1, subscriptions=[('status/#', 0)]),
        sender=sub))

    retained = [p for p in sub.packets() if isinstance(p, MQTTPublish)]
    assert len(retained) == 1
    assert retained[0].payload == b'up'
    assert retained[0].retain is True


async def test_qos2_inbound_handshake():
    broker, conn = BrokerActor(), RecordingConn()
    await _attach(broker, conn)
    await broker._handle(ClientPublish(
        publish=MQTTPublish(topic='t', payload=b'x', qos=2, packet_id=7),
        sender=conn))
    assert any(isinstance(p, MQTTPubrec) and p.packet_id == 7
               for p in conn.packets())
    conn.outbox.clear()
    await broker._handle(ClientPubrel(packet_id=7, sender=conn))
    assert any(isinstance(p, MQTTPubcomp) and p.packet_id == 7
               for p in conn.packets())


async def test_will_delivered_on_abnormal_detach():
    from blackbull.mqtt.messages import MQTTSubscribe
    broker = BrokerActor()
    sub, pub = RecordingConn(), RecordingConn()

    await _attach(broker, sub, client_id='sub')
    await broker._handle(ClientSubscribe(
        subscribe=MQTTSubscribe(packet_id=1, subscriptions=[('will/topic', 0)]),
        sender=sub))
    sub.outbox.clear()

    await _attach(broker, pub, client_id='pub',
                  will_topic='will/topic', will_payload=b'bye')
    await broker._handle(Detach(graceful=False, sender=pub))

    wills = [p for p in sub.packets() if isinstance(p, MQTTPublish)]
    assert len(wills) == 1
    assert wills[0].topic == 'will/topic'
    assert wills[0].payload == b'bye'


async def test_will_not_delivered_on_graceful_detach():
    from blackbull.mqtt.messages import MQTTSubscribe
    broker = BrokerActor()
    sub, pub = RecordingConn(), RecordingConn()

    await _attach(broker, sub, client_id='sub')
    await broker._handle(ClientSubscribe(
        subscribe=MQTTSubscribe(packet_id=1, subscriptions=[('will/topic', 0)]),
        sender=sub))
    sub.outbox.clear()

    await _attach(broker, pub, client_id='pub',
                  will_topic='will/topic', will_payload=b'bye')
    await broker._handle(Detach(graceful=True, sender=pub))

    assert not [p for p in sub.packets() if isinstance(p, MQTTPublish)]


async def test_session_resume_replays_pending_qos1():
    broker = BrokerActor()
    broker._sessions['c1'] = _new_broker_session()
    broker._sessions['c1']['_expiry'] = 3600
    broker._sessions['c1']['pending_qos1_out'][5] = MQTTPublish(
        topic='alerts', payload=b'!', qos=1, packet_id=5)

    conn = RecordingConn()
    await _attach(broker, conn, client_id='c1', clean_start=False)

    pkts = conn.packets()
    assert isinstance(pkts[0], MQTTConnack)
    assert pkts[0].session_present is True
    replayed = [p for p in pkts if isinstance(p, MQTTPublish)]
    assert len(replayed) == 1 and replayed[0].packet_id == 5


async def test_unsubscribe_stops_delivery():
    from blackbull.mqtt.messages import MQTTSubscribe
    broker = BrokerActor()
    sub, pub = RecordingConn(), RecordingConn()

    await _attach(broker, sub, client_id='sub')
    await broker._handle(ClientSubscribe(
        subscribe=MQTTSubscribe(packet_id=1, subscriptions=[('news', 0)]),
        sender=sub))
    await broker._handle(ClientUnsubscribe(
        unsubscribe=MQTTUnsubscribe(packet_id=2, topics=['news']),
        sender=sub))
    assert any(isinstance(p, MQTTUnsuback) for p in sub.packets())
    sub.outbox.clear()

    await _attach(broker, pub, client_id='pub')
    await broker._handle(ClientPublish(
        publish=MQTTPublish(topic='news', payload=b'x', qos=0), sender=pub))
    assert not [p for p in sub.packets() if isinstance(p, MQTTPublish)]
