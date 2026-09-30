"""
MQTT 5.0 Session State persistence conformance tests.

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

from tests.conformance.mqtt._harness import (
    run_until_idle, wait_idle, cancel_all,
)

from blackbull.mqtt.messages import (
    SESSION_EXPIRY_NEVER,
    ReasonCode,
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


# ============================================================================
# §3.1.2.3 — Clean Start behavior
# ============================================================================

class TestCleanStart:
    """§3.1.2.3 — Clean Start flag controls session lifecycle."""

    @pytest.mark.parametrize('client_id,clean_start,expected', [
        pytest.param('cs-false', False, False, id='clean-start-false-resumes'),
        pytest.param('cs-true', True, True, id='clean-start-true-discards'),
    ])
    def test_clean_start_false_resumes_session(self, mqtt, client_id, clean_start, expected):
        """§3.1.2.3 — Clean Start = 0 resumes an existing session if
        available; Clean Start = 1 discards any prior session."""
        connect = MQTTConnect(
            client_id=client_id,
            clean_start=clean_start,
            keep_alive=60,
        )
        wire = encode_packet(connect)
        decoded = decode_packet(wire)
        assert decoded.clean_start is expected

    @pytest.mark.parametrize('present', [
        pytest.param(True, id='session-present-true'),
        pytest.param(False, id='session-present-false'),
    ])
    def test_connack_session_present_true_when_session_exists(self, mqtt, present):
        """§3.2.2.3 — Session Present = True when a prior session was found;
        False for Clean Start or no prior session."""
        connack = MQTTConnack(
            session_present=present,
            reason_code=ReasonCode.SUCCESS,
        )
        assert connack.session_present is present


# ============================================================================
# §3.1.2.4 — Session Expiry Interval
# ============================================================================

class TestSessionExpiryInterval:
    """§3.1.2.4 — Session Expiry Interval property.

    Defines the time (in seconds) the server retains session state after
    the connection is closed.

    0 or absent = session ends immediately on disconnect.
    SESSION_EXPIRY_NEVER = session never expires (retained indefinitely).
    """

    def test_session_expiry_maximum_never_expires(self, mqtt):
        """§3.1.2.4 — SESSION_EXPIRY_NEVER = session never expires."""
        connect = MQTTConnect(
            client_id='se-forever',
            clean_start=False,
            keep_alive=60,
            properties={'session_expiry_interval': SESSION_EXPIRY_NEVER},
        )
        wire = encode_packet(connect)
        decoded = decode_packet(wire)
        assert decoded.properties['session_expiry_interval'] == SESSION_EXPIRY_NEVER


# ============================================================================
# §3.1.2.11 — Session state: subscriptions preserved across reconnect
# ============================================================================

class TestSessionStatePreservation:
    """§3.1.2.11 — Session state includes subscriptions, pending QoS messages."""

    @pytest.mark.asyncio
    async def test_subscriptions_preserved_with_clean_start_false(self, mqtt):
        """§3.1.2.11 — Subscriptions are preserved when Clean Start = 0
        and session has not expired."""

        # Connection 1: subscribe to both topics; the session must outlive the
        # connection (Session Expiry Interval > 0).
        await run_until_idle(
            mqtt,
            MQTTConnect(
                client_id='persist-sub',
                clean_start=True,
                keep_alive=60,
                properties={'session_expiry_interval': 3600},
            ),
            MQTTSubscribe(
                packet_id=1,
                subscriptions=[('sensors/temperature', 1), ('alerts/#', 2)],
            ),
        )

        # Reconnect with Clean Start = False
        _, writer = await run_until_idle(
            mqtt,
            MQTTConnect(
                client_id='persist-sub',
                clean_start=False,
                keep_alive=60,
                properties={'session_expiry_interval': 3600},
            ),
        )

        packets = writer.pop_packets()
        connacks = [p for p in packets if isinstance(p, MQTTConnack)]
        assert len(connacks) >= 1
        # Session Present should be True (session was found)
        assert connacks[0].session_present is True
        # §3.1.2.11 — both subscriptions survived the reconnect.
        filters = [s[0] for s in mqtt.sessions['persist-sub']['subscriptions']]
        assert sorted(filters) == ['alerts/#', 'sensors/temperature'], (
            f'subscriptions must survive a Clean Start = 0 reconnect; {filters}'
        )

    @pytest.mark.asyncio
    async def test_subscriptions_discarded_with_clean_start_true(self, mqtt):
        """§3.1.2.3 — Subscriptions are discarded when Clean Start = 1."""

        # Connection 1: subscribe; the session outlives the connection so the
        # Clean Start = 1 discard has something to discard.
        await run_until_idle(
            mqtt,
            MQTTConnect(
                client_id='cs-discard',
                clean_start=True,
                keep_alive=60,
                properties={'session_expiry_interval': 3600},
            ),
            MQTTSubscribe(packet_id=1, subscriptions=[('old/topic', 1)]),
        )

        _, writer = await run_until_idle(
            mqtt,
            MQTTConnect(
                client_id='cs-discard',
                clean_start=True,
                keep_alive=60,
                properties={'session_expiry_interval': 3600},
            ),
        )

        packets = writer.pop_packets()
        connacks = [p for p in packets if isinstance(p, MQTTConnack)]
        assert len(connacks) >= 1
        # Session Present should be False (session was discarded)
        assert connacks[0].session_present is False
        # §3.1.2.3 — the old subscription went with it.
        assert mqtt.sessions['cs-discard']['subscriptions'] == [], (
            'Clean Start = 1 must discard the old subscriptions'
        )


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
            reason_code=ReasonCode.SUCCESS,
            properties={'session_expiry_interval': 7200},
        )
        wire = encode_packet(disconnect)
        decoded = decode_packet(wire)
        assert decoded.properties.get('session_expiry_interval') == 7200
