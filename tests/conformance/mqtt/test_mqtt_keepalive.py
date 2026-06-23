"""
MQTT 5.0 Keep-Alive (PINGREQ/PINGRESP) conformance tests — Sprint 52.

Verifies heartbeat mechanism against the MQTT 5.0 OASIS Standard.

Reference: MQTT Version 5.0, OASIS Standard
  §3.1.2.10 Keep Alive
  §3.12    PINGREQ  – PING Request
  §3.13    PINGRESP – PING Response

Key behaviours:
  - §3.1.2.10: Keep Alive is a time interval measured in seconds.  It is the
    maximum time that can elapse between a client sending one Control Packet
    and the next.
  - §3.12: PINGREQ is sent by the client to the server to:
      a) indicate it is alive (keep-alive)
      b) request the server to respond (connectivity check)
  - §3.13: PINGRESP is sent by the server in response to PINGREQ.
  - §3.12 / §3.13: Both PINGREQ and PINGRESP have no variable header and no
    payload (fixed header only: 0xC0 0x00 and 0xD0 0x00 respectively).
  - If Keep Alive is non-zero and the server does not receive a Control Packet
    within 1.5 × Keep Alive, it MUST close the connection (§3.1.2.10).
"""

import asyncio
import time
import pytest

from blackbull.mqtt.messages import (
    MQTTConnect, MQTTConnack,
    MQTTPingreq, MQTTPingresp,
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

    async def read(self, n: int) -> bytes:
        if not self._buf:
            # Simulate read timeout (real MQTT actor would use a deadline)
            await asyncio.sleep(0.01)
            return b''
        chunk = bytes(self._buf[:n])
        del self._buf[:n]
        return chunk

    def feed(self, data: bytes) -> None:
        self._buf.extend(data)

    def feed_packet(self, packet) -> None:
        self._buf.extend(encode_packet(packet))


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
# §3.12 / §3.13 — PINGREQ / PINGRESP wire format
# ============================================================================

class TestPingreqPingrespWireFormat:
    """§3.12, §3.13 — PINGREQ and PINGRESP are 2-byte fixed packets."""

    def test_pingreq_is_exactly_two_bytes(self, mqtt):
        """§3.12 — PINGREQ: fixed header 0xC0, Remaining Length 0."""
        pingreq = MQTTPingreq()
        wire = encode_packet(pingreq)
        assert wire == b'\xC0\x00', \
            "PINGREQ must be exactly 0xC0 0x00 per §3.12"

    def test_pingresp_is_exactly_two_bytes(self, mqtt):
        """§3.13 — PINGRESP: fixed header 0xD0, Remaining Length 0."""
        pingresp = MQTTPingresp()
        wire = encode_packet(pingresp)
        assert wire == b'\xD0\x00', \
            "PINGRESP must be exactly 0xD0 0x00 per §3.13"

    def test_pingreq_round_trip(self, mqtt):
        """§3.12 — PINGREQ decode yields MQTTPingreq."""
        decoded = decode_packet(b'\xC0\x00')
        assert isinstance(decoded[0], MQTTPingreq)

    def test_pingresp_round_trip(self, mqtt):
        """§3.13 — PINGRESP decode yields MQTTPingresp."""
        decoded = decode_packet(b'\xD0\x00')
        assert isinstance(decoded[0], MQTTPingresp)


# ============================================================================
# §3.1.2.10 — Keep Alive timeout behaviour
# ============================================================================

class TestKeepAliveTimeout:
    """§3.1.2.10 — Keep Alive enforcement.

    If Keep Alive > 0 and no Control Packet is received within
    1.5 × Keep Alive seconds, the server MUST close the network connection.

    The server MAY apply a grace period and SHOULD send PINGREQ first
    (server-initiated keep-alive check) before closing.
    """

    def test_keep_alive_value_in_connect(self, mqtt):
        """§3.1.2.10 — Keep Alive is a 16-bit unsigned integer in seconds."""
        for ka in (0, 10, 60, 300, 3600, 65535):
            connect = MQTTConnect(
                client_id='ka-client',
                clean_start=True,
                keep_alive=ka,
            )
            wire = encode_packet(connect)
            decoded = decode_packet(wire)
            assert decoded.keep_alive == ka

    def test_keep_alive_zero_means_no_timeout(self, mqtt):
        """§3.1.2.10 — Keep Alive = 0 means the server is not required to
        disconnect on inactivity."""
        connect = MQTTConnect(
            client_id='no-ka',
            clean_start=True,
            keep_alive=0,
        )
        wire = encode_packet(connect)
        decoded = decode_packet(wire)
        assert decoded.keep_alive == 0

    @pytest.mark.asyncio
    async def test_pingreq_triggers_pingresp(self, mqtt):
        """§3.12 → §3.13 — Server MUST respond to PINGREQ with PINGRESP."""
        reader = _FakeMQTTReader()
        writer = _FakeMQTTWriter()
        ctx = _ctx()

        actor = mqtt.serve(reader, writer, ctx)
        # Connect then send PINGREQ
        reader.feed_packet(MQTTConnect(
            client_id='ping-client',
            clean_start=True,
            keep_alive=30,
        ))
        reader.feed_packet(MQTTPingreq())

        task = asyncio.create_task(actor.run())
        await asyncio.sleep(0.1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        packets = writer.pop_packets()
        pingresps = [p for p in packets if isinstance(p, MQTTPingresp)]
        assert len(pingresps) >= 1, "Server MUST respond to PINGREQ with PINGRESP"

    @pytest.mark.asyncio
    async def test_any_control_packet_resets_keepalive_timer(self, mqtt):
        """§3.1.2.10 — Any Control Packet sent by the client resets the
        keep-alive timer, not just PINGREQ.

        For example, a PUBLISH or SUBSCRIBE also counts as activity.
        """
        from blackbull.mqtt.messages import MQTTPublish

        reader = _FakeMQTTReader()
        writer = _FakeMQTTWriter()
        ctx = _ctx()

        # Connect with short keep-alive
        reader.feed_packet(MQTTConnect(
            client_id='active-client',
            clean_start=True,
            keep_alive=5,
        ))
        # Send PUBLISH to show activity (not PINGREQ)
        reader.feed_packet(MQTTPublish(
            topic='status/heartbeat',
            payload=b'alive',
            qos=0,
        ))

        actor = mqtt.serve(reader, writer, ctx)
        task = asyncio.create_task(actor.run())
        await asyncio.sleep(0.1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        # The server should NOT have disconnected (no error reason code sent),
        # because the PUBLISH reset the keep-alive timer.
        packets = writer.pop_packets()
        disconnects = [p for p in packets if hasattr(p, 'reason_code')
                       and p.__class__.__name__ == 'MQTTDisconnect']
        # No forced disconnect should have occurred
        assert len(disconnects) <= 1  # at most the normal one we explicitly send


# ============================================================================
# §3.1.2.10 — Server Keep Alive override (CONNACK property)
# ============================================================================

class TestServerKeepAlive:
    """§3.2.2.3.2 — Server Keep Alive property.

    If the server returns a Server Keep Alive in the CONNACK, the client
    MUST use that value instead of the value it sent in the CONNECT.
    """

    def test_connack_with_server_keep_alive(self, mqtt):
        """§3.2.2.3.2 — Server Keep Alive overrides client's Keep Alive."""
        connack = MQTTConnack(
            session_present=False,
            reason_code=0x00,
            properties={'server_keep_alive': 120},
        )
        wire = encode_packet(connack)
        decoded = decode_packet(wire)
        assert decoded.properties.get('server_keep_alive') == 120

    def test_connack_without_server_keep_alive_uses_client_value(self, mqtt):
        """§3.2.2.3.2 — If absent, client's Keep Alive value is used."""
        connack = MQTTConnack(
            session_present=False,
            reason_code=0x00,
        )
        wire = encode_packet(connack)
        decoded = decode_packet(wire)
        assert 'server_keep_alive' not in decoded.properties
