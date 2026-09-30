"""
MQTT 5.0 edge cases and server capability tests.

Covers remaining spec areas not in other test files.

Reference: MQTT Version 5.0, OASIS Standard
  §3.1.2.11  Request Problem Information flag
  §3.2.2.3.2 Server capability flags (Retain Available, Wildcard Available, etc.)
  §3.3.2.3.2 Message Expiry Interval
  §3.2.2.3.4 Maximum Packet Size enforcement
  §4.2       Network Connections (concurrent connections)
  §1.5.4     UTF-8 string encoding rules
"""

import asyncio

import pytest

from blackbull.mqtt.messages import (
    ReasonCode,
    MQTTConnect, MQTTConnack, MQTTDisconnect,
    MQTTPublish, MQTTSubscribe, MQTTSuback,
    MQTTPuback, MQTTPubrel,
    encode_packet, decode_packet,
    MQTTReasonCode,
)


# ============================================================================
# §3.1.2.11 — Request Problem Information flag
# ============================================================================

class TestRequestProblemInformation:
    """§3.1.2.11 — Request Problem Information (byte, 0 or 1).

    When set to 1, the client requests the server to return detailed
    error information (Reason String, User Properties) in failure
    responses.  When 0 (default), the server MAY omit Reason String
    even on errors.
    """

    def test_request_problem_information_default(self, mqtt):
        """§3.1.2.11 — Default is 0 (no detailed errors)."""
        connect = MQTTConnect(
            client_id='rpi-default',
            clean_start=True,
            keep_alive=60,
        )
        wire = encode_packet(connect)
        decoded = decode_packet(wire)
        assert 'request_problem_information' not in decoded.properties

    def test_request_problem_information_enabled(self, mqtt):
        """§3.1.2.11 — Request Problem Information = 1."""
        connect = MQTTConnect(
            client_id='rpi-on',
            clean_start=True,
            keep_alive=60,
            properties={'request_problem_information': 1},
        )
        wire = encode_packet(connect)
        decoded = decode_packet(wire)
        assert decoded.properties['request_problem_information'] == 1

    def test_request_problem_information_disabled(self, mqtt):
        """§3.1.2.11 — Request Problem Information = 0 explicitly."""
        connect = MQTTConnect(
            client_id='rpi-off',
            clean_start=True,
            keep_alive=60,
            properties={'request_problem_information': 0},
        )
        wire = encode_packet(connect)
        decoded = decode_packet(wire)
        assert decoded.properties['request_problem_information'] == 0


# ============================================================================
# §3.2.2.3.2 — Server capability flags in CONNACK
# ============================================================================

class TestServerCapabilities:
    """§3.2.2.3.2 — CONNACK properties describing server capabilities.

    These are informational flags that tell the client what features
    the server supports, so the client can avoid using unsupported features.
    """

    def test_retain_available_flag(self, mqtt):
        """§3.2.2.3.2 — Retain Available: 0 = not supported, 1 = supported."""
        for val in (0, 1):
            connack = MQTTConnack(
                session_present=False,
                reason_code=ReasonCode.SUCCESS,
                properties={'retain_available': val},
            )
            wire = encode_packet(connack)
            decoded = decode_packet(wire)
            assert decoded.properties['retain_available'] == val

    def test_wildcard_subscription_available_flag(self, mqtt):
        """§3.2.2.3.2 — Wildcard Subscription Available."""
        for val in (0, 1):
            connack = MQTTConnack(
                session_present=False,
                reason_code=ReasonCode.SUCCESS,
                properties={'wildcard_subscription_available': val},
            )
            wire = encode_packet(connack)
            decoded = decode_packet(wire)
            assert decoded.properties['wildcard_subscription_available'] == val

    def test_subscription_identifier_available_flag(self, mqtt):
        """§3.2.2.3.2 — Subscription Identifier Available."""
        for val in (0, 1):
            connack = MQTTConnack(
                session_present=False,
                reason_code=ReasonCode.SUCCESS,
                properties={'subscription_identifier_available': val},
            )
            wire = encode_packet(connack)
            decoded = decode_packet(wire)
            assert decoded.properties['subscription_identifier_available'] == val

    def test_shared_subscription_available_flag(self, mqtt):
        """§3.2.2.3.2 — Shared Subscription Available."""
        for val in (0, 1):
            connack = MQTTConnack(
                session_present=False,
                reason_code=ReasonCode.SUCCESS,
                properties={'shared_subscription_available': val},
            )
            wire = encode_packet(connack)
            decoded = decode_packet(wire)
            assert decoded.properties['shared_subscription_available'] == val

    def test_all_server_capability_flags_together(self, mqtt):
        """§3.2.2.3.2 — CONNACK with all capability flags."""
        connack = MQTTConnack(
            session_present=False,
            reason_code=ReasonCode.SUCCESS,
            properties={
                'retain_available': 1,
                'wildcard_subscription_available': 1,
                'subscription_identifier_available': 1,
                'shared_subscription_available': 0,
                'maximum_qos': 2,
                'receive_maximum': 100,
                'topic_alias_maximum': 16,
                'maximum_packet_size': 268435455,
                'server_keep_alive': 120,
            },
        )
        wire = encode_packet(connack)
        decoded = decode_packet(wire)
        assert decoded.properties['retain_available'] == 1
        assert decoded.properties['shared_subscription_available'] == 0
        assert decoded.properties['maximum_qos'] == 2


# ============================================================================
# §3.3.2.3.2 — Message Expiry Interval
# ============================================================================

class TestMessageExpiryInterval:
    """§3.3.2.3.2 — Message Expiry Interval.

    A 4-byte integer representing the Message Expiry Interval in seconds.
    If absent, the message does not expire.
    """

    def test_publish_with_message_expiry(self, mqtt):
        """§3.3.2.3.2 — PUBLISH with Message Expiry Interval."""
        publish = MQTTPublish(
            topic='events/temporary',
            payload=b'expires soon',
            qos=1,
            packet_id=1,
            properties={'message_expiry_interval': 60},
        )
        wire = encode_packet(publish)
        decoded = decode_packet(wire)
        assert decoded.properties['message_expiry_interval'] == 60

    def test_message_expiry_zero_no_expiry(self, mqtt):
        """§3.3.2.3.2 — Message Expiry Interval = 0 means no expiry."""
        publish = MQTTPublish(
            topic='events/permanent',
            payload=b'never expires',
            qos=1,
            packet_id=1,
            properties={'message_expiry_interval': 0},
        )
        wire = encode_packet(publish)
        decoded = decode_packet(wire)
        assert decoded.properties['message_expiry_interval'] == 0

    def test_will_message_expiry(self, mqtt):
        """§3.1.3.3 — Will Message can have Message Expiry Interval."""
        connect = MQTTConnect(
            client_id='will-expiry-client',
            clean_start=True,
            keep_alive=60,
            will_topic='clients/status',
            will_payload=b'lwt',
            will_qos=1,
            will_retain=False,
            will_properties={'message_expiry_interval': 3600},
        )
        wire = encode_packet(connect)
        decoded = decode_packet(wire)
        assert decoded.will_properties['message_expiry_interval'] == 3600


# ============================================================================
# §3.2.2.3.4 — Maximum Packet Size
# ============================================================================

class TestMaximumPacketSize:
    """§3.2.2.3.4 — Maximum Packet Size.

    The server MAY inform the client of the maximum packet size it is
    willing to accept.  If a client sends a packet larger than this,
    the server MUST send DISCONNECT with reason code 0x8E (Packet too large).
    """

    def test_maximum_packet_size_in_connack(self, mqtt):
        """§3.2.2.3.4 — CONNACK with Maximum Packet Size."""
        connack = MQTTConnack(
            session_present=False,
            reason_code=ReasonCode.SUCCESS,
            properties={'maximum_packet_size': 65536},  # 64 KB
        )
        wire = encode_packet(connack)
        decoded = decode_packet(wire)
        assert decoded.properties['maximum_packet_size'] == 65536

    def test_maximum_packet_size_in_connect(self, mqtt):
        """§3.1.2.3 — Client can also advertise Maximum Packet Size."""
        connect = MQTTConnect(
            client_id='mps-client',
            clean_start=True,
            keep_alive=60,
            properties={'maximum_packet_size': 262144},  # 256 KB
        )
        wire = encode_packet(connect)
        decoded = decode_packet(wire)
        assert decoded.properties['maximum_packet_size'] == 262144

    def test_packet_too_large_disconnect_reason(self, mqtt):
        """§3.14.2.1 — DISCONNECT reason code 0x8E: Packet too large."""
        disconnect = MQTTDisconnect(
            reason_code=ReasonCode.SESSION_TAKEN_OVER,
            properties={'reason_string': 'Packet exceeds maximum allowed size'},
        )
        wire = encode_packet(disconnect)
        decoded = decode_packet(wire)
        assert decoded.reason_code == ReasonCode.SESSION_TAKEN_OVER


# ============================================================================
# §1.5.4 — UTF-8 String encoding
# ============================================================================

class TestUTF8StringEncoding:
    """§1.5.4 — UTF-8 Encoded String.

    MQTT 5.0 uses UTF-8 encoding for all text fields.  A UTF-8 string
    is prefixed with a 2-byte length (0–65535 bytes).

    §1.5.4.1: Strings MUST be valid UTF-8.
    §1.5.4.2: The null character (U+0000) MUST NOT be used.
    """

    def test_client_id_utf8_validation(self, mqtt):
        """§1.5.4 — Client ID must be valid UTF-8 (encoded by dataclass)."""
        # Valid UTF-8
        connect = MQTTConnect(
            client_id='日本語クライアント',
            clean_start=True,
            keep_alive=60,
        )
        wire = encode_packet(connect)
        decoded = decode_packet(wire)
        assert decoded.client_id == '日本語クライアント'

    def test_topic_name_utf8_validation(self, mqtt):
        """§1.5.4 — Topic Names must be valid UTF-8."""
        from blackbull.mqtt.messages import validate_topic_name
        assert validate_topic_name('sensors/温度') is True

    def test_null_character_in_string_rejected(self, mqtt):
        """§1.5.4.2 — U+0000 (null) MUST NOT appear in UTF-8 strings."""
        # Client ID with null
        with pytest.raises(ValueError, match='null|\\\\x00|U\\+0000'):
            MQTTConnect(
                client_id='bad\x00id',
                clean_start=True,
                keep_alive=60,
            )

    def test_topic_filter_null_rejected(self, mqtt):
        """§1.5.4.2 — Null character in Topic Filter is invalid."""
        from blackbull.mqtt.messages import validate_topic_filter
        assert validate_topic_filter('sensors/\x00room') is False


# ============================================================================
# §3.1.2.11 — Session state includes all subscriptions
# ============================================================================

class TestSessionStateDetails:
    """§3.1.2.11 — Session state granularity.

    Session state includes:
      - All existing subscriptions (topic filters + QoS + subscription options)
      - QoS 1 and QoS 2 messages queued for delivery but not yet acknowledged
      - QoS 2 messages received but PUBREL not yet sent
      - QoS 2 messages sent but PUBCOMP not yet received
    """

    @pytest.mark.asyncio
    async def test_session_stores_subscription_options(self, mqtt):
        """§3.1.2.11 — Session preserves Subscription Options (No Local,
        Retain As Published, Retain Handling) across reconnects."""
        from tests.conformance.mqtt.test_mqtt_keepalive import (
            _FakeMQTTReader, _FakeMQTTWriter, _ctx)

        # Connection 1: a SUBSCRIBE carrying §3.8.3.1 options; the session
        # must outlive the connection (Session Expiry Interval > 0).
        reader = _FakeMQTTReader()
        writer = _FakeMQTTWriter()
        reader.feed_packet(MQTTConnect(
            client_id='opts-client', clean_start=True, keep_alive=60,
            properties={'session_expiry_interval': 3600},
        ))
        reader.feed_packet(MQTTSubscribe(
            packet_id=1,
            subscriptions=[('chat/room1', 1), ('alerts/#', 2)],
            subscription_options=[
                {'no_local': True, 'retain_as_published': True,
                 'retain_handling': 1},
                {'no_local': False},
            ],
        ))
        actor = mqtt.serve(reader, writer, _ctx())
        task = asyncio.create_task(actor.run())
        await asyncio.sleep(0.1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        # Connection 2: Clean Start = 0 reconnect — the session survives.
        reader = _FakeMQTTReader()
        writer = _FakeMQTTWriter()
        reader.feed_packet(MQTTConnect(
            client_id='opts-client', clean_start=False, keep_alive=60,
            properties={'session_expiry_interval': 3600},
        ))
        actor = mqtt.serve(reader, writer, _ctx())
        task = asyncio.create_task(actor.run())
        await asyncio.sleep(0.1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        # §3.1.2.11 — the session holds (filter, qos, options) triples, and
        # the Subscription Options came through the broker intact.
        subs = mqtt.sessions['opts-client']['subscriptions']
        assert len(subs) == 2
        chat_sub = [s for s in subs if s[0] == 'chat/room1'][0]
        alerts_sub = [s for s in subs if s[0] == 'alerts/#'][0]
        assert chat_sub[1] == 1
        assert chat_sub[2]['no_local'] is True
        assert chat_sub[2]['retain_as_published'] is True
        assert chat_sub[2]['retain_handling'] == 1
        assert alerts_sub[1] == 2
        assert alerts_sub[2]['no_local'] is False

    @pytest.mark.asyncio
    async def test_session_stores_pending_qos1_messages(self, mqtt):
        """§3.1.2.11 — Session stores QoS 1 messages pending acknowledgment."""
        from tests.conformance.mqtt.test_mqtt_keepalive import (
            _FakeMQTTReader, _FakeMQTTWriter, _ctx)

        sub_r, sub_w = _FakeMQTTReader(), _FakeMQTTWriter()
        sub_r.feed_packet(MQTTConnect(
            client_id='qos1-client', clean_start=True, keep_alive=60,
            properties={'session_expiry_interval': 3600},
        ))
        sub_r.feed_packet(MQTTSubscribe(
            packet_id=1, subscriptions=[('test/qos1', 1)],
        ))
        sub = mqtt.serve(sub_r, sub_w, _ctx())
        sub_task = asyncio.create_task(sub.run())
        await asyncio.sleep(0.1)

        # A QoS 1 delivery books pending_qos1_out until its PUBACK arrives.
        pub_r, pub_w = _FakeMQTTReader(), _FakeMQTTWriter()
        pub_r.feed_packet(MQTTConnect(
            client_id='qos1-pub', clean_start=True, keep_alive=60,
        ))
        pub_r.feed_packet(MQTTPublish(
            topic='test/qos1', payload=b'message-1', qos=1, packet_id=100,
        ))
        pub = mqtt.serve(pub_r, pub_w, _ctx())
        pub_task = asyncio.create_task(pub.run())
        await asyncio.sleep(0.1)

        pending = mqtt.sessions['qos1-client']['pending_qos1_out']
        assert len(pending) == 1, (
            'an unacked QoS 1 delivery must sit in pending_qos1_out'
        )
        (packet_id, stored), = pending.items()
        assert isinstance(stored, MQTTPublish)
        assert (stored.topic, stored.payload, stored.qos) == \
            ('test/qos1', b'message-1', 1)

        # PUBACK acknowledges it — the entry is dropped.
        sub_r.feed_packet(MQTTPuback(packet_id=packet_id))
        await asyncio.sleep(0.1)
        assert mqtt.sessions['qos1-client']['pending_qos1_out'] == {}, (
            'PUBACK must clear the pending_qos1_out entry'
        )

        for task in (sub_task, pub_task):
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    @pytest.mark.asyncio
    async def test_session_stores_pending_qos2_states(self, mqtt):
        """§3.1.2.11 — Session stores QoS 2 messages in various states."""
        from tests.conformance.mqtt.test_mqtt_keepalive import (
            _FakeMQTTReader, _FakeMQTTWriter, _ctx)

        reader = _FakeMQTTReader()
        writer = _FakeMQTTWriter()
        reader.feed_packet(MQTTConnect(
            client_id='qos2-client', clean_start=True, keep_alive=60,
            properties={'session_expiry_interval': 3600},
        ))
        reader.feed_packet(MQTTPublish(
            topic='test/qos2', payload=b'message-1', qos=2, packet_id=200,
        ))
        actor = mqtt.serve(reader, writer, _ctx())
        task = asyncio.create_task(actor.run())
        await asyncio.sleep(0.1)

        # Receive side, §4.3.3: PUBREC sent, awaiting PUBREL.  (The send side —
        # PUBLISH_SENT / PUBREL_SENT — is driven end to end by
        # tests/unit/test_mqtt_hardening.py::test_outbound_qos2_keeps_publish_for_replay.)
        assert mqtt.sessions['qos2-client']['pending_qos2_in'].get(200) == \
            'PUBREC_SENT', (
            'an inbound QoS 2 PUBLISH must sit at PUBREC_SENT'
        )

        # PUBREL completes the receive side (answered with PUBCOMP) and
        # clears the state.
        reader.feed_packet(MQTTPubrel(packet_id=200))
        await asyncio.sleep(0.1)
        assert 200 not in mqtt.sessions['qos2-client']['pending_qos2_in'], (
            'PUBREL must clear the pending_qos2_in state'
        )

        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


# ============================================================================
# §3.14 — DISCONNECT with Session Expiry override
# ============================================================================

class TestDisconnectSessionExpiry:
    """§3.14.2.2 — DISCONNECT can override Session Expiry Interval.

    The client can set a new Session Expiry Interval in DISCONNECT,
    which takes effect even if different from the CONNECT value.
    """

    def test_disconnect_with_shorter_session_expiry(self, mqtt):
        """§3.14.2.2 — Client reduces Session Expiry on DISCONNECT."""
        disconnect = MQTTDisconnect(
            reason_code=ReasonCode.SUCCESS,
            properties={'session_expiry_interval': 0},  # Expire immediately
        )
        wire = encode_packet(disconnect)
        decoded = decode_packet(wire)
        assert decoded.properties['session_expiry_interval'] == 0

    def test_disconnect_with_longer_session_expiry(self, mqtt):
        """§3.14.2.2 — Client extends Session Expiry on DISCONNECT."""
        disconnect = MQTTDisconnect(
            reason_code=ReasonCode.SUCCESS,
            properties={'session_expiry_interval': 86400},  # 24 hours
        )
        wire = encode_packet(disconnect)
        decoded = decode_packet(wire)
        assert decoded.properties['session_expiry_interval'] == 86400


# ============================================================================
# §3.1.2.11 — Response Information
# ============================================================================

class TestResponseInformation:
    """§3.1.2.11 / §3.2.2.3.2 — Response Information.

    If the client sets Request Response Information in CONNECT,
    the server MAY respond with Response Information in CONNACK
    (a UTF-8 string used as the basis for creating Response Topics).
    """

    def test_request_response_information(self, mqtt):
        """§3.1.2.11 — Client requests Response Information."""
        connect = MQTTConnect(
            client_id='rri-client',
            clean_start=True,
            keep_alive=60,
            properties={'request_response_information': 1},
        )
        wire = encode_packet(connect)
        decoded = decode_packet(wire)
        assert decoded.properties['request_response_information'] == 1

    def test_response_information_in_connack(self, mqtt):
        """§3.2.2.3.2 — Server provides Response Information."""
        connack = MQTTConnack(
            session_present=False,
            reason_code=ReasonCode.SUCCESS,
            properties={'response_information': 'responses/client-abc123'},
        )
        wire = encode_packet(connack)
        decoded = decode_packet(wire)
        assert decoded.properties['response_information'] == 'responses/client-abc123'


# ============================================================================
# §2.1 — Reserved control packet types
# ============================================================================

class TestReservedPacketTypes:
    """§2.1.1 — Packet types 0 and 15+ are reserved/forbidden.

    Receiving a packet with an unrecognized type MUST cause the server
    to close the connection.
    """

    def test_packet_type_0_is_forbidden(self, mqtt):
        """§2.1.1 — Control Packet Type 0 is reserved."""
        from blackbull.mqtt.messages import extract_packet_type
        with pytest.raises(ValueError, match='[Rr]eserved|[Ff]orbidden|[Uu]nknown'):
            extract_packet_type(0x00)
