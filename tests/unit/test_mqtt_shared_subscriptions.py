"""MQTT 5 §4.8.2 Shared Subscriptions — BrokerActor unit tests (Sprint 75).

Drives ``BrokerActor._handle`` directly with recording connection actors, same
harness as ``test_mqtt_broker_actor.py``.  Covers the load-balanced fan-out
contract: exactly-one delivery per share group per message, round-robin
rotation, membership changes (UNSUBSCRIBE / detach), the no-connected-member
case, the No Local protocol error (§3.8.3.1), retained-message exclusion
(§4.8.2), and the Retain Handling 1 rider (§3.3.1.3).
"""
import pytest

from blackbull.actor import Actor
from blackbull.mqtt.broker import (
    BrokerActor,
    Attach, ClientSubscribe, ClientUnsubscribe, ClientPublish,
    Detach, Send, Close,
)
from blackbull.mqtt.messages import (
    MQTTConnect, MQTTDisconnect, MQTTPublish, MQTTSubscribe, MQTTSuback,
    MQTTUnsubscribe, ReasonCode,
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


async def _publish(broker, conn, topic='t', payload=b'x', qos=0, **kw):
    await broker._handle(ClientPublish(
        publish=MQTTPublish(topic=topic, payload=payload, qos=qos, **kw),
        sender=conn))


async def _group(broker, n, filter_='$share/pool/t', qos=0):
    """Attach *n* clients subscribed to the same share group; returns them."""
    members = []
    for i in range(n):
        conn = RecordingConn()
        await _attach(broker, conn, client_id=f'm{i}')
        await _subscribe(broker, conn, i + 1, [(filter_, qos)])
        conn.outbox.clear()
        members.append(conn)
    return members


# ===========================================================================
# SUBACK contract — the 0x9E fence is gone (spec change vs Sprint 70)
# ===========================================================================

class TestSharedSubscribeAccepted:
    async def test_share_filter_granted_not_rejected(self):
        broker, conn = BrokerActor(), RecordingConn()
        await _attach(broker, conn, client_id='c1')
        await _subscribe(broker, conn, 1, [('$share/g/t', 1)])
        suback = [p for p in conn.packets() if isinstance(p, MQTTSuback)][0]
        assert suback.reason_codes == [1]  # granted QoS, not 0x9E
        assert [s[0] for s in broker._sessions['c1']['subscriptions']] == \
            ['$share/g/t']

    async def test_malformed_share_filters_rejected_0x8f(self):
        """§4.8.2 — no filter part, empty ShareName, wildcard ShareName, and
        an empty filter portion are all 0x8F per-entry rejections."""
        broker, conn = BrokerActor(), RecordingConn()
        await _attach(broker, conn, client_id='c1')
        await _subscribe(broker, conn, 1, [
            ('$share/g', 0), ('$share//t', 0), ('$share/g+/t', 0),
            ('$share/g/', 0),
        ])
        suback = [p for p in conn.packets() if isinstance(p, MQTTSuback)][0]
        assert suback.reason_codes == [ReasonCode.TOPIC_FILTER_INVALID] * 4
        assert broker._sessions['c1']['subscriptions'] == []


# ===========================================================================
# §4.8.2 — exactly-one delivery, round-robin rotation
# ===========================================================================

class TestSharedDelivery:
    async def test_round_robin_exactly_one_per_message(self):
        broker = BrokerActor()
        a, b = await _group(broker, 2)
        pub = RecordingConn()
        await _attach(broker, pub, client_id='pub')

        for i in range(4):
            await _publish(broker, pub, payload=b'%d' % i)

        # Each message reaches exactly one member; rotation alternates.
        assert [p.payload for p in a.publishes()] == [b'0', b'2']
        assert [p.payload for p in b.publishes()] == [b'1', b'3']

    async def test_non_shared_subscriber_always_receives(self):
        broker = BrokerActor()
        a, b = await _group(broker, 2)
        plain = RecordingConn()
        await _attach(broker, plain, client_id='plain')
        await _subscribe(broker, plain, 9, [('t', 0)])
        pub = RecordingConn()
        await _attach(broker, pub, client_id='pub')

        for i in range(4):
            await _publish(broker, pub, payload=b'%d' % i)

        assert len(plain.publishes()) == 4                       # broadcast
        assert len(a.publishes()) + len(b.publishes()) == 4      # exactly one

    async def test_groups_rotate_independently(self):
        """Two share groups on the same filter each get one copy per message."""
        broker = BrokerActor()
        g1 = await _group(broker, 2, filter_='$share/g1/t')
        g2a = RecordingConn()
        await _attach(broker, g2a, client_id='g2a')
        await _subscribe(broker, g2a, 1, [('$share/g2/t', 0)])
        pub = RecordingConn()
        await _attach(broker, pub, client_id='pub')

        for i in range(2):
            await _publish(broker, pub, payload=b'%d' % i)

        assert len(g1[0].publishes()) + len(g1[1].publishes()) == 2
        assert len(g2a.publishes()) == 2   # sole member of its own group

    async def test_client_with_shared_and_non_shared_gets_non_shared_copy(self):
        broker = BrokerActor()
        both, other = RecordingConn(), RecordingConn()
        await _attach(broker, both, client_id='both')
        await _subscribe(broker, both, 1, [('$share/g/t', 0), ('t', 0)])
        await _attach(broker, other, client_id='other')
        await _subscribe(broker, other, 1, [('$share/g/t', 0)])
        both.outbox.clear()
        pub = RecordingConn()
        await _attach(broker, pub, client_id='pub')

        for i in range(2):
            await _publish(broker, pub, payload=b'%d' % i)

        # Non-shared copy every message, plus the shared copy when rotation
        # picks this client (shared and non-shared are independent
        # subscriptions).  Rotation: msg 0 -> both, msg 1 -> other.
        assert [p.payload for p in both.publishes()] == [b'0', b'0', b'1']
        assert [p.payload for p in other.publishes()] == [b'1']

    async def test_delivery_at_member_granted_qos(self):
        broker = BrokerActor()
        a, b = RecordingConn(), RecordingConn()
        await _attach(broker, a, client_id='a')
        await _subscribe(broker, a, 1, [('$share/g/t', 1)])
        await _attach(broker, b, client_id='b')
        await _subscribe(broker, b, 1, [('$share/g/t', 0)])
        pub = RecordingConn()
        await _attach(broker, pub, client_id='pub')

        await _publish(broker, pub, qos=1, packet_id=7)   # -> a (qos 1)
        await _publish(broker, pub, qos=1, packet_id=8)   # -> b (qos 0)

        (pa,), (pb,) = a.publishes(), b.publishes()
        assert pa.qos == 1 and pa.packet_id is not None
        assert pb.qos == 0 and pb.packet_id is None

    async def test_wildcard_share_filter_matches(self):
        broker = BrokerActor()
        (a,) = await _group(broker, 1, filter_='$share/pool/sensors/+')
        pub = RecordingConn()
        await _attach(broker, pub, client_id='pub')
        await _publish(broker, pub, topic='sensors/temp')
        assert len(a.publishes()) == 1


# ===========================================================================
# §4.8.2 — membership changes and offline members
# ===========================================================================

class TestSharedMembership:
    async def test_unsubscribe_removes_member_from_rotation(self):
        broker = BrokerActor()
        a, b = await _group(broker, 2)
        await broker._handle(ClientUnsubscribe(
            unsubscribe=MQTTUnsubscribe(packet_id=5, topics=['$share/pool/t']),
            sender=b))
        pub = RecordingConn()
        await _attach(broker, pub, client_id='pub')

        for i in range(3):
            await _publish(broker, pub, payload=b'%d' % i)

        assert len(a.publishes()) == 3
        assert b.publishes() == []

    async def test_disconnected_member_skipped(self):
        """§4.8.2.3 — while any member is connected, disconnected members
        (even with a live session) receive nothing."""
        broker = BrokerActor()
        a, b = await _group(broker, 2)
        await broker._handle(Detach(graceful=True, sender=b))
        pub = RecordingConn()
        await _attach(broker, pub, client_id='pub')

        for i in range(3):
            await _publish(broker, pub, payload=b'%d' % i)

        assert len(a.publishes()) == 3
        assert b.publishes() == []

    async def test_no_connected_member_drops_message(self):
        """Documented behaviour: with no group member connected the message is
        not queued — matching the broker's existing no-offline-queue semantics
        for non-shared subscriptions."""
        broker = BrokerActor()
        a, b = await _group(broker, 2)
        await broker._handle(Detach(graceful=True, sender=a))
        await broker._handle(Detach(graceful=True, sender=b))
        pub = RecordingConn()
        await _attach(broker, pub, client_id='pub')

        await _publish(broker, pub, payload=b'lost')   # must not raise

        assert a.publishes() == [] and b.publishes() == []


# ===========================================================================
# §3.8.3.1 — No Local on a shared subscription is a Protocol Error
# ===========================================================================

class TestSharedNoLocal:
    async def test_no_local_on_shared_disconnects_0x82(self):
        broker, conn = BrokerActor(), RecordingConn()
        await _attach(broker, conn, client_id='c1')
        conn.outbox.clear()
        await _subscribe(broker, conn, 1, [('$share/g/t', 0)],
                         options=[{'no_local': True}])
        # §4.13 — Protocol Error: DISCONNECT 0x82 then close; no SUBACK.
        disconnects = [p for p in conn.packets() if isinstance(p, MQTTDisconnect)]
        assert disconnects and disconnects[0].reason_code == ReasonCode.PROTOCOL_ERROR
        assert [m for m in conn.outbox if isinstance(m, Close)]
        assert not [p for p in conn.packets() if isinstance(p, MQTTSuback)]
        assert broker._sessions['c1']['subscriptions'] == []

    async def test_no_local_on_plain_filter_still_fine(self):
        broker, conn = BrokerActor(), RecordingConn()
        await _attach(broker, conn, client_id='c1')
        await _subscribe(broker, conn, 1, [('t', 0)],
                         options=[{'no_local': True}])
        suback = [p for p in conn.packets() if isinstance(p, MQTTSuback)][0]
        assert suback.reason_codes == [0]


# ===========================================================================
# §4.8.2 retained exclusion + §3.3.1.3 Retain Handling 1 rider
# ===========================================================================

class TestRetainedInteraction:
    async def test_shared_subscription_never_receives_retained(self):
        broker = BrokerActor()
        pub = RecordingConn()
        await _attach(broker, pub, client_id='pub')
        await _publish(broker, pub, payload=b'r', retain=True)

        conn = RecordingConn()
        await _attach(broker, conn, client_id='c1')
        await _subscribe(broker, conn, 1, [('$share/g/t', 0)])
        assert conn.publishes() == []

    async def test_rh1_delivers_retained_on_new_subscription(self):
        broker = BrokerActor()
        pub = RecordingConn()
        await _attach(broker, pub, client_id='pub')
        await _publish(broker, pub, payload=b'r', retain=True)

        conn = RecordingConn()
        await _attach(broker, conn, client_id='c1')
        await _subscribe(broker, conn, 1, [('t', 0)],
                         options=[{'retain_handling': 1}])
        assert len(conn.publishes()) == 1

    async def test_rh1_skips_retained_on_existing_subscription(self):
        broker = BrokerActor()
        pub = RecordingConn()
        await _attach(broker, pub, client_id='pub')
        await _publish(broker, pub, payload=b'r', retain=True)

        conn = RecordingConn()
        await _attach(broker, conn, client_id='c1')
        await _subscribe(broker, conn, 1, [('t', 0)],
                         options=[{'retain_handling': 1}])
        conn.outbox.clear()
        # Re-SUBSCRIBE to the same filter: subscription already exists.
        await _subscribe(broker, conn, 2, [('t', 0)],
                         options=[{'retain_handling': 1}])
        assert conn.publishes() == []

    async def test_rh0_redelivers_retained_on_resubscribe(self):
        broker = BrokerActor()
        pub = RecordingConn()
        await _attach(broker, pub, client_id='pub')
        await _publish(broker, pub, payload=b'r', retain=True)

        conn = RecordingConn()
        await _attach(broker, conn, client_id='c1')
        await _subscribe(broker, conn, 1, [('t', 0)])
        conn.outbox.clear()
        await _subscribe(broker, conn, 2, [('t', 0)])   # RH defaults to 0
        assert len(conn.publishes()) == 1


# ===========================================================================
# Rotation-state hygiene
# ===========================================================================

class TestRotationState:
    async def test_rotation_state_pruned_when_group_empties(self):
        broker = BrokerActor()
        a, b = await _group(broker, 2)
        pub = RecordingConn()
        await _attach(broker, pub, client_id='pub')
        await _publish(broker, pub)
        assert ('pool', 't') in broker._share_rotation

        await broker._handle(ClientUnsubscribe(
            unsubscribe=MQTTUnsubscribe(packet_id=5, topics=['$share/pool/t']),
            sender=a))
        await broker._handle(Detach(graceful=True, sender=b))  # session dropped
        assert ('pool', 't') not in broker._share_rotation
