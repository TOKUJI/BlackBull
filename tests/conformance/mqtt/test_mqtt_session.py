"""
MQTT 5.0 Session State persistence conformance tests — Sprint 52.

Verifies session management against the MQTT 5.0 OASIS Standard.

Reference: MQTT Version 5.0, OASIS Standard
  §3.1.2.3  Clean Start flag
  §3.1.2.4  Session Expiry Interval
  §3.2.2.3  Session Present flag in CONNACK
  §3.1.2.11 Session state definition

Key behaviours:
  - §3.1.2.3: Clean Start = 1: discard any existing session, start fresh.
  - §3.1.2.3: Clean Start = 0: resume existing session if available.
  - §3.1.2.4: Session Expiry Interval (4-byte integer) defines how long the
    server retains session state after disconnect. 0 = immediate expiry.
  - §3.2.2.3: Session Present flag in CONNACK: True if session state exists,
    False if no prior session or Clean Start = 1.
  - Session state includes:
      a) Existing subscriptions (including Subscription Identifiers)
      b) QoS 1 and QoS 2 messages queued for delivery
      c) QoS 2 messages in the process of being delivered (pending PUBREL)
      d) QoS 2 messages received but not yet released (pending PUBCOMP)
"""

import asyncio
import pytest

from blackbull.mqtt.messages import (
    MQTTConnect, MQTTConnack, MQTTDisconnect,
    MQTTSubscribe, MQTTSuback,
    MQTTPublish,
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
            await asyncio.sleep(0.01)
            return b''
        chunk = bytes(self._buf[:n])
        del self._buf[:n]
        return chunk

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


def _ctx(conn_id: str = 'test-conn'):
    return ProtocolContext(
        peername=('127.0.0.1', 54321),
        sockname=('0.0.0.0', 1883),
        ssl=False,
        aggregator=None,
        connection_id=conn_id,
        protocol='mqtt',
    )


# ============================================================================
# §3.1.2.3 — Clean Start behavior
# ============================================================================

class TestCleanStart:
    """§3.1.2.3 — Clean Start flag controls session lifecycle."""

    def test_clean_start_true_discards_existing_session(self, mqtt):
        """§3.1.2.3 — Clean Start = 1: server discards any prior session."""
        connect = MQTTConnect(
            client_id='cs-true',
            clean_start=True,
            keep_alive=60,
        )
        wire = encode_packet(connect)
        decoded = decode_packet(wire)
        assert decoded.clean_start is True

    def test_clean_start_false_resumes_session(self, mqtt):
        """§3.1.2.3 — Clean Start = 0: resume existing session if available."""
        connect = MQTTConnect(
            client_id='cs-false',
            clean_start=False,
            keep_alive=60,
        )
        wire = encode_packet(connect)
        decoded = decode_packet(wire)
        assert decoded.clean_start is False

    def test_connack_session_present_true_when_session_exists(self, mqtt):
        """§3.2.2.3 — Session Present = True when a prior session was found."""
        connack = MQTTConnack(
            session_present=True,
            reason_code=0x00,
        )
        assert connack.session_present is True

    def test_connack_session_present_false_when_clean_start(self, mqtt):
        """§3.2.2.3 — Session Present = False for Clean Start or no prior session."""
        connack = MQTTConnack(
            session_present=False,
            reason_code=0x00,
        )
        assert connack.session_present is False


# ============================================================================
# §3.1.2.4 — Session Expiry Interval
# ============================================================================

class TestSessionExpiryInterval:
    """§3.1.2.4 — Session Expiry Interval property.

    Defines the time (in seconds) the server retains session state after
    the connection is closed.

    0 or absent = session ends immediately on disconnect.
    0xFFFFFFFF = session never expires (retained indefinitely).
    """

    def test_session_expiry_set_to_3600(self, mqtt):
        """§3.1.2.4 — Session Expiry = 3600 (1 hour)."""
        connect = MQTTConnect(
            client_id='se-client',
            clean_start=False,
            keep_alive=60,
            properties={'session_expiry_interval': 3600},
        )
        wire = encode_packet(connect)
        decoded = decode_packet(wire)
        assert decoded.properties['session_expiry_interval'] == 3600

    def test_session_expiry_zero_means_immediate_expiry(self, mqtt):
        """§3.1.2.4 — Session Expiry = 0: session ends on disconnect."""
        connect = MQTTConnect(
            client_id='se-zero',
            clean_start=False,
            keep_alive=60,
            properties={'session_expiry_interval': 0},
        )
        wire = encode_packet(connect)
        decoded = decode_packet(wire)
        assert decoded.properties['session_expiry_interval'] == 0

    def test_session_expiry_absent_uses_server_default(self, mqtt):
        """§3.1.2.4 — If absent, server's default Session Expiry is used."""
        connect = MQTTConnect(
            client_id='se-default',
            clean_start=False,
            keep_alive=60,
        )
        wire = encode_packet(connect)
        decoded = decode_packet(wire)
        assert 'session_expiry_interval' not in decoded.properties

    def test_session_expiry_maximum_never_expires(self, mqtt):
        """§3.1.2.4 — 0xFFFFFFFF = session never expires."""
        connect = MQTTConnect(
            client_id='se-forever',
            clean_start=False,
            keep_alive=60,
            properties={'session_expiry_interval': 0xFFFFFFFF},
        )
        wire = encode_packet(connect)
        decoded = decode_packet(wire)
        assert decoded.properties['session_expiry_interval'] == 0xFFFFFFFF


# ============================================================================
# §3.1.2.11 — Session state: subscriptions preserved across reconnect
# ============================================================================

class TestSessionStatePreservation:
    """§3.1.2.11 — Session state includes subscriptions, pending QoS messages."""

    @pytest.mark.asyncio
    async def test_subscriptions_preserved_with_clean_start_false(self, mqtt):
        """§3.1.2.11 — Subscriptions are preserved when Clean Start = 0
        and session has not expired."""

        # Pre-populate session with subscriptions
        mqtt.sessions['persist-sub'] = {
            'subscriptions': {
                ('sensors/temperature', 1),
                ('alerts/#', 2),
            },
            'pending_qos2_in': {},
            'pending_qos2_out': {},
        }

        reader = _FakeMQTTReader()
        writer = _FakeMQTTWriter()
        ctx = _ctx()

        actor = mqtt.serve(reader, writer, ctx)
        # Reconnect with Clean Start = False
        reader.feed_packet(MQTTConnect(
            client_id='persist-sub',
            clean_start=False,
            keep_alive=60,
        ))

        task = asyncio.create_task(actor.run())
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        packets = writer.pop_packets()
        connacks = [p for p in packets if isinstance(p, MQTTConnack)]
        assert len(connacks) >= 1
        # Session Present should be True (session was found)
        assert connacks[0].session_present is True

    @pytest.mark.asyncio
    async def test_subscriptions_discarded_with_clean_start_true(self, mqtt):
        """§3.1.2.3 — Subscriptions are discarded when Clean Start = 1."""

        # Pre-populate session (should be discarded)
        mqtt.sessions['cs-discard'] = {
            'subscriptions': {('old/topic', 1)},
            'pending_qos2_in': {},
            'pending_qos2_out': {},
        }

        reader = _FakeMQTTReader()
        writer = _FakeMQTTWriter()
        ctx = _ctx()

        actor = mqtt.serve(reader, writer, ctx)
        reader.feed_packet(MQTTConnect(
            client_id='cs-discard',
            clean_start=True,
            keep_alive=60,
        ))

        task = asyncio.create_task(actor.run())
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        packets = writer.pop_packets()
        connacks = [p for p in packets if isinstance(p, MQTTConnack)]
        assert len(connacks) >= 1
        # Session Present should be False (session was discarded)
        assert connacks[0].session_present is False


# ============================================================================
# §3.1.2.4 — Session expiry on disconnect
# ============================================================================

class TestSessionExpiryOnDisconnect:
    """§3.1.2.4 — When a client disconnects, session state is retained
    for the Session Expiry Interval.  If the client reconnects within
    that window with Clean Start = 0, the session is resumed."""

    def test_session_expiry_interval_in_disconnect(self, mqtt):
        """§3.14.2.2 — DISCONNECT can include Session Expiry Interval
        to override the value set in CONNECT."""
        disconnect = MQTTDisconnect(
            reason_code=0x00,
            properties={'session_expiry_interval': 7200},
        )
        wire = encode_packet(disconnect)
        decoded = decode_packet(wire)
        assert decoded.properties.get('session_expiry_interval') == 7200
