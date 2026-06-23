"""
Property-based tests for MQTT 5.0 packet serialization — Sprint 52.

Uses Hypothesis to verify invariants across a wide range of inputs,
complementing the example-based conformance tests.

Reference: MQTT Version 5.0, OASIS Standard
  §2.1   Structure of a Control Packet
  §2.2.1 Remaining Length (Variable Byte Integer)
"""

import pytest
from hypothesis import given, strategies as st

from blackbull.mqtt.messages import (
    encode_variable_byte_integer,
    decode_variable_byte_integer,
    encode_packet,
    decode_packet,
    MQTTPublish, MQTTConnect, MQTTSuback,
    MQTTReasonCode,
    extract_packet_type, extract_flags,
)


# ============================================================================
# Strategies
# ============================================================================

# Variable Byte Integer values: 0 to 268,435,455 (§2.2.1)
variable_byte_integer = st.integers(min_value=0, max_value=268435455)

# Valid MQTT topic names (no wildcards, at least 1 char, no nulls)
topic_name = st.text(
    alphabet=st.characters(whitelist_categories=('Lu', 'Ll', 'Nd'),
                           whitelist_characters='/-_.:+'),
    min_size=1,
    max_size=256,
).filter(lambda s: '\x00' not in s and '+' not in s and '#' not in s)

# Packet Identifiers: 1 to 65535 (§2.2.1)
packet_id = st.integers(min_value=1, max_value=65535)

# Payload data (up to 4KB for property tests)
payload = st.binary(min_size=0, max_size=4096)

# QoS levels
qos = st.sampled_from([0, 1, 2])

# Reason codes
reason_code = st.sampled_from([
    0x00, 0x10, 0x11, 0x80, 0x81, 0x82, 0x83, 0x84, 0x85,
    0x86, 0x87, 0x88, 0x89, 0x8A, 0x8C, 0x8D, 0x8E, 0x8F,
    0x90, 0x91, 0x92, 0x93, 0x94, 0x95, 0x96, 0x97, 0x98, 0x99,
])


# ============================================================================
# §2.2.1 — Variable Byte Integer round-trip property
# ============================================================================

@pytest.mark.properties
@given(value=variable_byte_integer)
def test_variable_byte_integer_round_trip(value):
    """§2.2.1 — encode then decode yields original value (∀ values 0..268435455)."""
    encoded = encode_variable_byte_integer(value)
    decoded, consumed = decode_variable_byte_integer(encoded)
    assert decoded == value, \
        f"Round-trip failed for {value}: encoded={encoded.hex()}, decoded={decoded}"
    assert 1 <= consumed <= 4, \
        f"Variable Byte Integer must consume 1-4 bytes, consumed {consumed} for {value}"


@pytest.mark.properties
@given(value=variable_byte_integer)
def test_variable_byte_integer_length_bounds(value):
    """§1.5.1 — Encoding length is 1-4 bytes depending on value range."""
    encoded = encode_variable_byte_integer(value)
    if value < 128:
        assert len(encoded) == 1
    elif value < 16384:
        assert len(encoded) == 2
    elif value < 2097152:
        assert len(encoded) == 3
    else:
        assert len(encoded) == 4


# ============================================================================
# §3.3 — PUBLISH packet round-trip property
# ============================================================================

@pytest.mark.properties
@given(topic=topic_name, qos=qos, pid=packet_id, data=payload)
def test_publish_packet_round_trip(topic, qos, pid, data):
    """§3.3 — PUBLISH: encode → decode → all fields preserved."""
    publish = MQTTPublish(
        topic=topic,
        payload=data,
        qos=qos,
        packet_id=pid if qos > 0 else None,
        retain=False,
        dup=False,
    )
    wire = encode_packet(publish)
    decoded = decode_packet(wire)
    assert isinstance(decoded, MQTTPublish)
    assert decoded.topic == topic
    assert decoded.payload == data
    assert decoded.qos == qos
    if qos > 0:
        assert decoded.packet_id == pid
    else:
        assert decoded.packet_id is None


# ============================================================================
# §2.1.1 — Fixed header invariants
# ============================================================================

@pytest.mark.properties
@given(topic=topic_name, qos=qos, data=payload)
def test_fixed_header_type_and_flags(topic, qos, data):
    """§2.1.1 — Fixed header byte 1: type (bits 7-4) + flags (bits 3-0)."""
    # §3.3.2-2: a QoS > 0 PUBLISH requires a Packet Identifier.
    publish = MQTTPublish(topic=topic, payload=data, qos=qos,
                          packet_id=1 if qos > 0 else None)
    wire = encode_packet(publish)
    assert len(wire) >= 2, "Every MQTT packet has at least 2 bytes (fixed header)"
    ptype = extract_packet_type(wire[0])
    flags = extract_flags(wire[0])
    assert ptype == 3  # PUBLISH type
    assert (flags >> 1) & 0x3 == qos  # QoS occupies bits 2-1


# ============================================================================
# §3.9 — SUBACK reason code count matches subscription count
# ============================================================================

@pytest.mark.properties
@given(num_subs=st.integers(min_value=1, max_value=10),
       pid=packet_id)
def test_suback_reason_code_count(num_subs, pid):
    """§3.9 — SUBACK contains one Reason Code per Topic Filter."""
    reason_codes = st.lists(
        st.sampled_from([0x00, 0x01, 0x02, 0x80, 0x90]),
        min_size=num_subs,
        max_size=num_subs,
    )
    # Cannot use @given on the inner list easily; generate manually
    import random
    rc_list = [random.choice([0x00, 0x01, 0x02, 0x80]) for _ in range(num_subs)]
    suback = MQTTSuback(packet_id=pid, reason_codes=rc_list)
    wire = encode_packet(suback)
    decoded = decode_packet(wire)
    assert isinstance(decoded, MQTTSuback)
    assert len(decoded.reason_codes) == num_subs
    assert decoded.reason_codes == rc_list


# ============================================================================
# §3.1 — CONNECT Keep Alive bounds
# ============================================================================

@pytest.mark.properties
@given(keep_alive=st.integers(min_value=0, max_value=65535))
def test_connect_keep_alive_bounds(keep_alive):
    """§3.1.2.10 — Keep Alive is a 16-bit unsigned integer (0–65535)."""
    connect = MQTTConnect(
        client_id='prop-test',
        clean_start=True,
        keep_alive=keep_alive,
    )
    wire = encode_packet(connect)
    decoded = decode_packet(wire)
    assert decoded.keep_alive == keep_alive


# ============================================================================
# Reason codes: valid range property
# ============================================================================

@pytest.mark.properties
@given(code=st.integers(min_value=0, max_value=255))
def test_reason_code_handles_all_byte_values(code):
    """Any byte value can be stored as a reason code without crashing."""
    rc = MQTTReasonCode(code)
    assert rc is not None
    # value is preserved
    assert rc.value == code


# ============================================================================
# Packet Identifier: non-zero for QoS 1 and 2
# ============================================================================

@pytest.mark.properties
@given(pid=packet_id, topic=topic_name, data=payload)
def test_publish_qos12_must_have_packet_id(pid, topic, data):
    """§3.3.2-2, §3.3.2-3 — QoS 1 and 2 PUBLISH must include Packet Identifier."""
    for q in (1, 2):
        publish = MQTTPublish(
            topic=topic,
            payload=data,
            qos=q,
            packet_id=pid,
        )
        wire = encode_packet(publish)
        decoded = decode_packet(wire)
        assert decoded.packet_id == pid


# ============================================================================
# Topic name: never contains wildcards after round-trip
# ============================================================================

@pytest.mark.properties
@given(topic=topic_name, data=payload)
def test_publish_topic_preserved_as_literal(topic, data):
    """§4.7.2 — Topic Names in PUBLISH are literal; no wildcard expansion."""
    publish = MQTTPublish(topic=topic, payload=data, qos=0)
    wire = encode_packet(publish)
    decoded = decode_packet(wire)
    assert decoded.topic == topic
    assert '+' not in decoded.topic, "Topic Name must not contain '+'"
    assert '#' not in decoded.topic, "Topic Name must not contain '#'"
