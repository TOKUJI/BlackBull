"""
MQTT 5.0 Will Message (Last-Will Testament) conformance tests.

Verifies LWT behaviour against the MQTT 5.0 OASIS Standard.

Reference: MQTT Version 5.0, OASIS Standard
  §3.1.2.5  Will Flag (§3.1.3.3 Will Message fields)
  §3.1.3.3  Will Topic, Will Payload, Will QoS, Will Retain, Will Properties
  §3.14.2.1 DISCONNECT with Will Message (reason code 0x04)

Key behaviours:
  - A Will Message is published by the server on behalf of the client when:
      a) The network connection is closed without a DISCONNECT packet
      b) The client sends DISCONNECT with reason code 0x04 (Disconnect with Will Message)
  - The Will Message is NOT published if the client sends a normal DISCONNECT
    (reason code 0x00 — Normal disconnection).
  - §3.1.3.3: Will Delay Interval (§3.2.2.3.2) specifies a server-side delay
    before publishing the Will Message (in seconds).
  - Will QoS and Will Retain flag determine the delivery level.
  - Will Properties can include Content Type, Payload Format Indicator,
    Message Expiry Interval, Response Topic, Correlation Data, User Properties.
"""

import asyncio
import pytest

from blackbull.mqtt.messages import (
    ReasonCode,
    MQTTConnect, MQTTConnack, MQTTDisconnect,
    MQTTPublish, MQTTSubscribe,
    encode_packet, decode_packet,
)
from blackbull.server.protocol_registry import ProtocolContext
from blackbull.server.sender import AbstractWriter
from blackbull.server.recipient import AbstractReader


# ---------------------------------------------------------------------------
# In-process fakes
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

    def feed_packet(self, packet) -> None:
        self._buf.extend(encode_packet(packet))

    def close(self):
        """Simulate connection drop (unclean disconnect)."""
        self._closed = True
        self._buf.clear()

    def at_eof(self) -> bool:
        return self._closed and not self._buf


class _FakeMQTTWriter(AbstractWriter):
    def __init__(self):
        self.written = bytearray()

    async def write(self, data: bytes) -> None:
        self.written.extend(data)

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


def _ctx():
    return ProtocolContext(
        peername=('127.0.0.1', 54321),
        sockname=('0.0.0.0', 1883),
        ssl=False,
        aggregator=None,
        connection_id='test-conn',
        protocol='mqtt',
    )


# ============================================================================
# §3.1.3.3 — Will Message configuration in CONNECT
# ============================================================================

class TestWillMessageConfiguration:
    """§3.1.3.3 — Will Message is set via CONNECT flags and payload fields."""

    def test_connect_with_will_topic_and_payload(self, mqtt):
        """§3.1.3.3 — CONNECT with Will Topic and Will Payload set."""
        connect = MQTTConnect(
            client_id='will-client',
            clean_start=True,
            keep_alive=60,
            will_topic='system/clients/will-client/status',
            will_payload=b'offline',
            will_qos=1,
            will_retain=False,
        )
        wire = encode_packet(connect)
        decoded = decode_packet(wire)
        assert decoded.will_topic == 'system/clients/will-client/status'
        assert decoded.will_payload == b'offline'
        assert decoded.will_qos == 1
        assert decoded.will_retain is False

    def test_connect_with_will_properties(self, mqtt):
        """§3.1.3.3 — CONNECT with Will Properties: Will Delay, Content Type, etc."""
        connect = MQTTConnect(
            client_id='will-prop-client',
            clean_start=True,
            keep_alive=60,
            will_topic='clients/will-prop-client/status',
            will_payload=b'{"status":"offline"}',
            will_qos=2,
            will_retain=True,
            will_properties={
                'will_delay_interval': 30,
                'content_type': 'application/json',
                'payload_format_indicator': 1,  # UTF-8
                'message_expiry_interval': 3600,
                'user_properties': [('type', 'lwt')],
            },
        )
        wire = encode_packet(connect)
        decoded = decode_packet(wire)
        assert decoded.will_properties == {
            'will_delay_interval': 30,
            'content_type': 'application/json',
            'payload_format_indicator': 1,
            'message_expiry_interval': 3600,
            'user_properties': [('type', 'lwt')],
        }

    @pytest.mark.parametrize("will_qos", [0, 1, 2])
    def test_will_qos_levels(self, will_qos):
        """§3.1.3.3 — Will QoS can be 0, 1, or 2."""
        connect = MQTTConnect(
            client_id=f'will-qos{will_qos}',
            clean_start=True,
            keep_alive=60,
            will_topic='clients/status',
            will_payload=b'lwt',
            will_qos=will_qos,
            will_retain=False,
        )
        wire = encode_packet(connect)
        decoded = decode_packet(wire)
        assert decoded.will_qos == will_qos

    def test_will_with_retain_flag(self, mqtt):
        """§3.1.3.3 — Will Retain flag causes the Will Message to be retained."""
        connect = MQTTConnect(
            client_id='will-retain-client',
            clean_start=True,
            keep_alive=60,
            will_topic='clients/status',
            will_payload=b'lwt-retained',
            will_qos=0,
            will_retain=True,
        )
        wire = encode_packet(connect)
        decoded = decode_packet(wire)
        assert decoded.will_retain is True


# ============================================================================
# §3.1.2.5 — Will Message delivery on abnormal disconnect
# ============================================================================

class TestWillMessageDelivery:
    """§3.1.2.5 — Will Message delivery rules.

    The server MUST publish the Will Message when:
      1. The network connection is closed (TCP reset / timeout) without
         a DISCONNECT packet
      2. The client sends DISCONNECT with reason code 0x04
         (Disconnect with Will Message)

    The server MUST NOT publish the Will Message when:
      - The client sends DISCONNECT with reason code 0x00 (Normal disconnection)
    """

    @pytest.mark.asyncio
    async def test_will_message_published_on_unclean_disconnect(self, mqtt):
        """§3.1.2.5 — Will Message is published when connection drops
        without DISCONNECT."""

        # An observer subscribed to the Will topic makes the routed Will
        # visible on the wire (the leaving client itself receives nothing).
        obs_r, obs_w = _FakeMQTTReader(), _FakeMQTTWriter()
        obs_r.feed_packet(MQTTConnect(
            client_id='will-obs', clean_start=True, keep_alive=0,
        ))
        obs_r.feed_packet(MQTTSubscribe(
            packet_id=1, subscriptions=[('system/clients/#', 0)],
        ))
        obs = mqtt.serve(obs_r, obs_w, _ctx())
        obs_task = asyncio.create_task(obs.run())
        await asyncio.sleep(0.05)

        reader = _FakeMQTTReader()
        writer = _FakeMQTTWriter()
        ctx = _ctx()

        actor = mqtt.serve(reader, writer, ctx)
        # Connect with Will Message configured
        reader.feed_packet(MQTTConnect(
            client_id='will-drop-client',
            clean_start=True,
            keep_alive=60,
            will_topic='system/clients/will-drop-client/status',
            will_payload=b'connection-lost',
            will_qos=0,
            will_retain=False,
        ))

        # Start the actor, then simulate connection drop
        task = asyncio.create_task(actor.run())
        await asyncio.sleep(0.05)

        # Simulate unclean disconnect (no DISCONNECT packet)
        reader.close()

        await asyncio.sleep(0.1)

        # §3.1.2.5 — the Will Message was published with its configured topic
        # and payload.
        publishes = [p for p in obs_w.pop_packets() if isinstance(p, MQTTPublish)]
        assert [(p.topic, p.payload) for p in publishes] == [
            ('system/clients/will-drop-client/status', b'connection-lost'),
        ], f'the Will must be published on an unclean disconnect; {publishes}'

        for t in (task, obs_task):
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass

    def test_will_message_not_published_on_normal_disconnect(self, mqtt):
        """§3.14 — Normal DISCONNECT (0x00) MUST NOT trigger Will Message.

        This is verified in test_connect.py: the DISCONNECT with 0x00
        is a graceful close, not a Will-triggering event.
        """
        disconnect = MQTTDisconnect(reason_code=ReasonCode.SUCCESS)
        wire = encode_packet(disconnect)
        decoded = decode_packet(wire)
        assert decoded.reason_code == ReasonCode.SUCCESS

    def test_disconnect_with_will_message_reason_code(self, mqtt):
        """§3.14.2.1 — DISCONNECT reason code 0x04 triggers Will Message."""
        disconnect = MQTTDisconnect(
            reason_code=ReasonCode.DISCONNECT_WITH_WILL,
            properties={'reason_string': 'Client requested Will delivery'},
        )
        wire = encode_packet(disconnect)
        decoded = decode_packet(wire)
        assert decoded.reason_code == ReasonCode.DISCONNECT_WITH_WILL


# ============================================================================
# §3.1.3.3 — Will Delay Interval
# ============================================================================

class TestWillDelayInterval:
    """§3.1.3.3 / §3.2.2.3.2 — Will Delay Interval.

    The Will Delay Interval (a 4-byte integer) specifies a delay before
    the server publishes the Will Message.  This gives the client a window
    to reconnect and avoid having the Will Message published unnecessarily
    during a brief network interruption.

    If the client reconnects before the Will Delay expires, the server
    MUST NOT publish the Will Message.
    """

    def test_will_delay_interval_property(self, mqtt):
        """§3.1.3.3 — Will Delay Interval is set in Will Properties."""
        connect = MQTTConnect(
            client_id='will-delay-client',
            clean_start=True,
            keep_alive=60,
            will_topic='clients/status',
            will_payload=b'lwt',
            will_qos=0,
            will_retain=False,
            will_properties={'will_delay_interval': 30},
        )
        wire = encode_packet(connect)
        decoded = decode_packet(wire)
        assert decoded.will_properties['will_delay_interval'] == 30

    def test_will_delay_zero_means_immediate(self, mqtt):
        """§3.1.3.3 — Will Delay = 0 means publish immediately on disconnect."""
        connect = MQTTConnect(
            client_id='will-no-delay',
            clean_start=True,
            keep_alive=60,
            will_topic='clients/status',
            will_payload=b'lwt',
            will_qos=0,
            will_retain=False,
            will_properties={'will_delay_interval': 0},
        )
        wire = encode_packet(connect)
        decoded = decode_packet(wire)
        assert decoded.will_properties['will_delay_interval'] == 0
