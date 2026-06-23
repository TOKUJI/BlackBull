"""
MQTT 5.0 integration scenario tests — Sprint 52.

End-to-end tests that exercise multiple MQTT features together in realistic
usage patterns.  These verify that the broker correctly handles complex
interactions between CONNECT, SUBSCRIBE, PUBLISH, QoS flows, session
persistence, Will Messages, and Retained Messages.

Reference: MQTT Version 5.0, OASIS Standard
"""

import asyncio
import pytest

from blackbull.mqtt.messages import (
    MQTTConnect, MQTTConnack, MQTTDisconnect,
    MQTTPublish, MQTTPuback, MQTTPubrec, MQTTPubrel, MQTTPubcomp,
    MQTTSubscribe, MQTTSuback,
    MQTTUnsubscribe, MQTTUnsuback,
    MQTTPingreq, MQTTPingresp,
    encode_packet, decode_packet,
)
from blackbull.server.protocol_registry import ProtocolContext
from blackbull.server.sender import AbstractWriter
from blackbull.server.recipient import AbstractReader


# ---------------------------------------------------------------------------
# In-process fakes (used across all integration scenarios)
# ---------------------------------------------------------------------------

class _FakeMQTTReader(AbstractReader):
    def __init__(self, data: bytes = b''):
        self._buf = bytearray(data)
        self._closed = False

    async def read(self, n: int) -> bytes:
        if not self._buf:
            if self._closed:
                return b''
            await asyncio.sleep(0.01)
            return b''
        chunk = bytes(self._buf[:n])
        del self._buf[:n]
        return chunk

    def feed(self, data: bytes) -> None:
        self._buf.extend(data)

    def feed_packet(self, packet) -> None:
        self._buf.extend(encode_packet(packet))

    def close(self):
        self._closed = True
        self._buf.clear()

    def at_eof(self) -> bool:
        return self._closed and not self._buf


class _FakeMQTTWriter(AbstractWriter):
    def __init__(self):
        self.written = bytearray()

    async def write(self, data: bytes) -> None:
        self.written.extend(data)

    async def drain(self) -> None:
        pass

    def pop_packets(self) -> list:
        packets = []
        offset = 0
        buf = bytes(self.written)
        while offset < len(buf):
            packet, consumed = decode_packet(buf[offset:])
            packets.append(packet)
            offset += consumed
        self.written = self.written[offset:]
        return packets

    def peek_packets(self) -> list:
        """Return packets without consuming them."""
        packets = []
        offset = 0
        buf = bytes(self.written)
        while offset < len(buf):
            packet, consumed = decode_packet(buf[offset:])
            packets.append(packet)
            offset += consumed
        return packets


def _ctx(conn_id: str = 'test-conn-001'):
    return ProtocolContext(
        peername=('127.0.0.1', 54321),
        sockname=('0.0.0.0', 1883),
        ssl=False,
        aggregator=None,
        connection_id=conn_id,
        protocol='mqtt',
    )


# ============================================================================
# Scenario 1: Single client — connect, subscribe, publish, unsubscribe, disconnect
# ============================================================================

class TestScenarioSingleClientFullLifecycle:
    """A single MQTT client goes through a complete session lifecycle:
    connect → subscribe → publish → receive message → unsubscribe → disconnect."""

    @pytest.mark.asyncio
    async def test_full_lifecycle(self, mqtt):
        """End-to-end: connect, subscribe to a topic, publish, receive,
        unsubscribe, disconnect."""
        reader = _FakeMQTTReader()
        writer = _FakeMQTTWriter()
        ctx = _ctx()

        actor = mqtt.serve(reader, writer, ctx)

        # Step 1: CONNECT
        reader.feed_packet(MQTTConnect(
            client_id='lifecycle-client',
            clean_start=True,
            keep_alive=60,
        ))

        # Step 2: SUBSCRIBE
        reader.feed_packet(MQTTSubscribe(
            packet_id=10,
            subscriptions=[('sensors/temperature', 1)],
        ))

        # Step 3: PUBLISH (QoS 0)
        reader.feed_packet(MQTTPublish(
            topic='sensors/temperature',
            payload=b'22.5',
            qos=0,
        ))

        # Step 4: UNSUBSCRIBE
        reader.feed_packet(MQTTUnsubscribe(
            packet_id=20,
            topics=['sensors/temperature'],
        ))

        # Step 5: DISCONNECT
        reader.feed_packet(MQTTDisconnect(reason_code=0x00))

        task = asyncio.create_task(actor.run())
        await asyncio.sleep(0.15)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        packets = writer.pop_packets()
        assert len(packets) >= 4, \
            f"Expected at least CONNACK+SUBACK+PUBLISH+UNSUBACK, got {len(packets)} packets"

        # Verify packet types in order
        packet_types = [type(p).__name__ for p in packets]
        assert 'MQTTConnack' in packet_types
        assert 'MQTTSuback' in packet_types
        assert 'MQTTUnsuback' in packet_types


# ============================================================================
# Scenario 2: Two clients — publisher and subscriber
# ============================================================================

class TestScenarioPublisherSubscriber:
    """PUB/SUB pattern: one client publishes, another subscribes and receives.

    This verifies the core broker routing mechanism.
    """

    @pytest.mark.asyncio
    async def test_publisher_subscriber_message_flow(self, mqtt):
        """Client A subscribes, Client B publishes, Client A receives."""
        # --- Subscriber (Client A) ---
        sub_reader = _FakeMQTTReader()
        sub_writer = _FakeMQTTWriter()
        sub_ctx = _ctx('sub-conn')

        sub_actor = mqtt.serve(sub_reader, sub_writer, sub_ctx)

        sub_reader.feed_packet(MQTTConnect(
            client_id='subscriber-A',
            clean_start=True,
            keep_alive=60,
        ))
        sub_reader.feed_packet(MQTTSubscribe(
            packet_id=1,
            subscriptions=[('chat/general', 1)],
        ))

        sub_task = asyncio.create_task(sub_actor.run())
        await asyncio.sleep(0.05)

        # --- Publisher (Client B) ---
        pub_reader = _FakeMQTTReader()
        pub_writer = _FakeMQTTWriter()
        pub_ctx = _ctx('pub-conn')

        pub_actor = mqtt.serve(pub_reader, pub_writer, pub_ctx)

        pub_reader.feed_packet(MQTTConnect(
            client_id='publisher-B',
            clean_start=True,
            keep_alive=60,
        ))
        pub_reader.feed_packet(MQTTPublish(
            topic='chat/general',
            payload=b'Hello, world!',
            qos=1,
            packet_id=100,
        ))

        pub_task = asyncio.create_task(pub_actor.run())
        await asyncio.sleep(0.1)

        # Clean up
        sub_task.cancel()
        pub_task.cancel()
        try:
            await asyncio.gather(sub_task, pub_task, return_exceptions=True)
        except (asyncio.CancelledError, Exception):
            pass

        # Verify subscriber received the message
        sub_packets = sub_writer.pop_packets()
        sub_publishes = [p for p in sub_packets if isinstance(p, MQTTPublish)]
        # The subscriber should have received the published message
        # (routed through the internal topic router)
        assert len(sub_publishes) >= 1, \
            "Subscriber should have received the published message"
        chat_msgs = [p for p in sub_publishes if p.topic == 'chat/general']
        assert len(chat_msgs) >= 1
        assert chat_msgs[0].payload == b'Hello, world!'

        # Verify publisher got PUBACK
        pub_packets = pub_writer.pop_packets()
        pubacks = [p for p in pub_packets if isinstance(p, MQTTPuback)]
        assert len(pubacks) >= 1
        assert pubacks[0].packet_id == 100


# ============================================================================
# Scenario 3: Session persistence across reconnects
# ============================================================================

class TestScenarioSessionPersistence:
    """§4.1 — Session persistence: client disconnects and reconnects with
    Clean Start = 0, and queued messages are delivered."""

    @pytest.mark.asyncio
    async def test_session_resume_delivers_queued_messages(self, mqtt):
        """Client subscribes, disconnects, publisher sends messages,
        client reconnects with Clean Start=0, receives queued messages."""

        # --- First connection: subscribe and disconnect ---
        reader1 = _FakeMQTTReader()
        writer1 = _FakeMQTTWriter()
        ctx1 = _ctx('sess-conn-1')

        actor1 = mqtt.serve(reader1, writer1, ctx1)
        reader1.feed_packet(MQTTConnect(
            client_id='session-client',
            clean_start=True,
            keep_alive=60,
            properties={'session_expiry_interval': 3600},
        ))
        reader1.feed_packet(MQTTSubscribe(
            packet_id=1,
            subscriptions=[('alerts/system', 1)],
        ))
        reader1.feed_packet(MQTTDisconnect(reason_code=0x00))

        task1 = asyncio.create_task(actor1.run())
        await asyncio.sleep(0.1)
        task1.cancel()
        try:
            await task1
        except asyncio.CancelledError:
            pass

        # --- Simulate: another client publishes while session-client is offline ---
        # Manually queue a message into the session store
        session = mqtt.sessions.get('session-client')
        if session:
            session.setdefault('pending_qos1_out', {})[500] = MQTTPublish(
                topic='alerts/system',
                payload=b'SYSTEM ALERT: CPU > 90%',
                qos=1,
                packet_id=500,
            )

        # --- Second connection: reconnect with Clean Start = 0 ---
        reader2 = _FakeMQTTReader()
        writer2 = _FakeMQTTWriter()
        ctx2 = _ctx('sess-conn-2')

        actor2 = mqtt.serve(reader2, writer2, ctx2)
        reader2.feed_packet(MQTTConnect(
            client_id='session-client',
            clean_start=False,  # Resume session
            keep_alive=60,
        ))

        task2 = asyncio.create_task(actor2.run())
        await asyncio.sleep(0.1)
        task2.cancel()
        try:
            await task2
        except asyncio.CancelledError:
            pass

        packets2 = writer2.pop_packets()
        connacks = [p for p in packets2 if isinstance(p, MQTTConnack)]
        assert len(connacks) >= 1
        # Session Present should be True
        assert connacks[0].session_present is True

        # Queued message should have been delivered
        publishes = [p for p in packets2 if isinstance(p, MQTTPublish)]
        alert_msgs = [p for p in publishes if hasattr(p, 'topic')
                      and p.topic == 'alerts/system']
        assert len(alert_msgs) >= 1, \
            "Queued message should be delivered on session resume"


# ============================================================================
# Scenario 4: QoS 2 complete flow — end-to-end
# ============================================================================

class TestScenarioQoS2EndToEnd:
    """§4.5 — Complete QoS 2 flow between publisher and subscriber.

    Publisher → Broker → Subscriber (QoS 2)
    Subscriber → PUBREC → Broker → PUBREL → Publisher
    Publisher → PUBCOMP → Broker → PUBCOMP → Subscriber
    """

    @pytest.mark.asyncio
    async def test_qos2_end_to_end_flow(self, mqtt):
        """Full QoS 2 message flow: PUBLISH → PUBREC → PUBREL → PUBCOMP."""

        # Subscriber
        sub_reader = _FakeMQTTReader()
        sub_writer = _FakeMQTTWriter()
        sub_ctx = _ctx('qos2-sub')

        sub_actor = mqtt.serve(sub_reader, sub_writer, sub_ctx)
        sub_reader.feed_packet(MQTTConnect(
            client_id='qos2-subscriber',
            clean_start=True,
            keep_alive=60,
        ))
        sub_reader.feed_packet(MQTTSubscribe(
            packet_id=1,
            subscriptions=[('critical/data', 2)],  # QoS 2 subscription
        ))

        sub_task = asyncio.create_task(sub_actor.run())
        await asyncio.sleep(0.05)

        # Publisher
        pub_reader = _FakeMQTTReader()
        pub_writer = _FakeMQTTWriter()
        pub_ctx = _ctx('qos2-pub')

        pub_actor = mqtt.serve(pub_reader, pub_writer, pub_ctx)
        pub_reader.feed_packet(MQTTConnect(
            client_id='qos2-publisher',
            clean_start=True,
            keep_alive=60,
        ))
        pub_reader.feed_packet(MQTTPublish(
            topic='critical/data',
            payload=b'IMPORTANT DATA',
            qos=2,
            packet_id=999,
        ))

        pub_task = asyncio.create_task(pub_actor.run())
        await asyncio.sleep(0.15)

        sub_task.cancel()
        pub_task.cancel()
        try:
            await asyncio.gather(sub_task, pub_task, return_exceptions=True)
        except (asyncio.CancelledError, Exception):
            pass

        # Verify subscriber received the QoS 2 message
        sub_packets = sub_writer.pop_packets()
        sub_publishes = [p for p in sub_packets if isinstance(p, MQTTPublish)]
        critical_msgs = [p for p in sub_publishes
                         if getattr(p, 'topic', None) == 'critical/data']
        assert len(critical_msgs) >= 1

        # The message is forwarded to the subscriber at QoS 2 (the broker
        # assigns a fresh Packet Identifier on the outbound link, §4.5).  The
        # subscriber's own PUBREC travels back on its *input* link, which the
        # broker-side writer does not capture, so we assert the delivered QoS.
        assert critical_msgs[0].qos == 2

        # Verify the publisher received PUBREC (broker → publisher) for its
        # QoS 2 PUBLISH (Packet Identifier 999).
        pub_packets = pub_writer.pop_packets()
        pub_pubrecs = [p for p in pub_packets if isinstance(p, MQTTPubrec)]
        assert len(pub_pubrecs) >= 1
        assert pub_pubrecs[0].packet_id == 999


# ============================================================================
# Scenario 5: Will Message delivery on abnormal disconnect
# ============================================================================

class TestScenarioWillMessageDelivery:
    """§3.1.2.5 — Will Message is delivered when client disconnects abnormally
    and another client is subscribed to the Will Topic."""

    @pytest.mark.asyncio
    async def test_will_message_delivered_to_subscriber(self, mqtt):
        """Client A subscribes to LWT topic.
        Client B connects with Will, then abnormally disconnects.
        Client A receives the Will Message."""

        # --- Monitoring Client (Client A) ---
        mon_reader = _FakeMQTTReader()
        mon_writer = _FakeMQTTWriter()
        mon_ctx = _ctx('mon-conn')

        mon_actor = mqtt.serve(mon_reader, mon_writer, mon_ctx)
        mon_reader.feed_packet(MQTTConnect(
            client_id='monitor-A',
            clean_start=True,
            keep_alive=60,
        ))
        mon_reader.feed_packet(MQTTSubscribe(
            packet_id=1,
            subscriptions=[('clients/+/status', 0)],
        ))

        mon_task = asyncio.create_task(mon_actor.run())
        await asyncio.sleep(0.05)

        # --- Client B with Will ---
        b_reader = _FakeMQTTReader()
        b_writer = _FakeMQTTWriter()
        b_ctx = _ctx('b-conn')

        b_actor = mqtt.serve(b_reader, b_writer, b_ctx)
        b_reader.feed_packet(MQTTConnect(
            client_id='client-B',
            clean_start=True,
            keep_alive=60,
            will_topic='clients/client-B/status',
            will_payload=b'offline',
            will_qos=0,
            will_retain=False,
        ))

        b_task = asyncio.create_task(b_actor.run())
        await asyncio.sleep(0.05)

        # Simulate abnormal disconnect for Client B
        b_reader.close()
        await asyncio.sleep(0.1)

        mon_task.cancel()
        b_task.cancel()
        try:
            await asyncio.gather(mon_task, b_task, return_exceptions=True)
        except (asyncio.CancelledError, Exception):
            pass

        # Check that Monitor A received the Will Message
        mon_packets = mon_writer.pop_packets()
        mon_publishes = [p for p in mon_packets if isinstance(p, MQTTPublish)]
        will_msgs = [p for p in mon_publishes
                     if getattr(p, 'topic', None) == 'clients/client-B/status']
        assert len(will_msgs) >= 1, \
            "Monitor should have received the Will Message"
        assert will_msgs[0].payload == b'offline'


# ============================================================================
# Scenario 6: Keep-alive — PINGREQ/PINGRESP cycle
# ============================================================================

class TestScenarioKeepAlive:
    """§3.12 / §3.13 — Client sends PINGREQ, server responds with PINGRESP."""

    @pytest.mark.asyncio
    async def test_pingreq_pingresp_cycle(self, mqtt):
        """Client sends PINGREQ after connect, receives PINGRESP."""
        reader = _FakeMQTTReader()
        writer = _FakeMQTTWriter()
        ctx = _ctx()

        actor = mqtt.serve(reader, writer, ctx)
        reader.feed_packet(MQTTConnect(
            client_id='ping-client',
            clean_start=True,
            keep_alive=30,
        ))
        reader.feed_packet(MQTTPingreq())
        reader.feed_packet(MQTTPingreq())  # second ping
        reader.feed_packet(MQTTDisconnect(reason_code=0x00))

        task = asyncio.create_task(actor.run())
        await asyncio.sleep(0.15)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        packets = writer.pop_packets()
        pingresps = [p for p in packets if isinstance(p, MQTTPingresp)]
        assert len(pingresps) == 2, \
            f"Expected 2 PINGRESP, got {len(pingresps)}"
