"""QoS 3 is malformed, and the broker used to route it.

`decode_publish_flags` masks two bits (§3.3.1) and returns whatever they
hold, so `0b11` decodes to `qos=3`.  Nothing downstream rejects it:
`_on_publish` acknowledges only qos 1 and 2, so a qos-3 PUBLISH is
delivered to subscribers and retained with **no acknowledgement at all**.

MQTT 5 §3.3.1-4 makes it a Malformed Packet, and §4.13 says the server
sends DISCONNECT with reason 0x81 and closes.  That is what these assert.

Behaviour, not codec symmetry — the same reason `test_mqtt_hardening.py`
exists.
"""
from __future__ import annotations

import pytest

from blackbull.actor import Actor
from blackbull.mqtt.broker import (
    Attach, BrokerActor, ClientPublish, ClientSubscribe, Close, Send,
)
from blackbull.mqtt.messages import (
    MQTTConnect, MQTTPublish, MQTTSubscribe, ReasonCode, decode_publish_flags,
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

    def closes(self):
        return [m for m in self.outbox if isinstance(m, Close)]


class TestTheDecoderRejectsIt:
    def test_qos_3_is_not_decoded_as_a_value(self):
        """`0b0110` is DUP=0, QoS=3, RETAIN=0."""
        with pytest.raises(Exception):
            decode_publish_flags(0b0110)

    @pytest.mark.parametrize('flags,qos', [
        (0b0000, 0), (0b0010, 1), (0b0100, 2),
    ])
    def test_the_legal_values_still_decode(self, flags, qos):
        assert decode_publish_flags(flags).qos == qos

    def test_dup_and_retain_still_decode(self):
        f = decode_publish_flags(0b1101)          # DUP, QoS=2, RETAIN
        assert (f.qos, f.dup, f.retain) == (2, True, True)


class TestTheBrokerRefusesToRouteIt:
    """Drives the broker the way `test_mqtt_hardening.py` does."""

    async def _attach(self, broker, conn, client_id='c1'):
        await broker._handle(Attach(
            connect=MQTTConnect(client_id=client_id, clean_start=True,
                                keep_alive=60),
            sender=conn))
        conn.outbox.clear()

    async def _subscribe(self, broker, conn, topic):
        await broker._handle(ClientSubscribe(
            subscribe=MQTTSubscribe(packet_id=1, subscriptions=[(topic, 0)]),
            sender=conn))
        conn.outbox.clear()

    async def test_a_qos_3_publish_disconnects_with_0x81(self):
        broker, pub = BrokerActor(), RecordingConn()
        await self._attach(broker, pub)

        await broker._handle(ClientPublish(
            publish=MQTTPublish(topic='a/b', payload=b'x', qos=3,
                                packet_id=9), sender=pub))

        codes = [p.reason_code for p in pub.packets()
                 if hasattr(p, 'reason_code')]
        assert ReasonCode.MALFORMED_PACKET in codes, (
            f'expected DISCONNECT 0x81; sent {pub.packets()!r}')
        assert pub.closes(), 'the connection must be closed (§4.13)'

    async def test_it_is_not_delivered_to_subscribers(self):
        """The damage the missing acknowledgement hid: it routed anyway."""
        broker, sub, pub = BrokerActor(), RecordingConn(), RecordingConn()
        await self._attach(broker, sub, 'sub')
        await self._subscribe(broker, sub, 'a/b')
        await self._attach(broker, pub, 'pub')

        await broker._handle(ClientPublish(
            publish=MQTTPublish(topic='a/b', payload=b'x', qos=3,
                                packet_id=9), sender=pub))

        delivered = [p for p in sub.packets() if isinstance(p, MQTTPublish)]
        assert delivered == [], (
            f'a malformed packet reached a subscriber: {delivered!r}')

    @pytest.mark.parametrize('qos', [0, 1, 2])
    async def test_qos_0_1_2_still_route(self, qos):
        """The rejection must not narrow what already worked."""
        broker, sub, pub = BrokerActor(), RecordingConn(), RecordingConn()
        await self._attach(broker, sub, 'sub')
        await self._subscribe(broker, sub, 'a/b')
        await self._attach(broker, pub, 'pub')

        await broker._handle(ClientPublish(
            publish=MQTTPublish(topic='a/b', payload=b'x', qos=qos,
                                packet_id=7 if qos else None),
            sender=pub))

        delivered = [p for p in sub.packets() if isinstance(p, MQTTPublish)]
        assert delivered, f'qos={qos} was not delivered'
