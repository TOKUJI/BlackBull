"""Property tests for HTTP/2 frame serialization (RFC 7540 §4).

These tests verify frame-level invariants across the full range of stream IDs
and payload sizes — something example-based tests cannot cover systematically.
"""
import pytest
from hypothesis import given
from hypothesis import strategies as st

from blackbull.protocol.frame import FrameFactory
from blackbull.protocol.frame_types import FrameTypes, DataFrameFlags
from .strategies import stream_id, ws_payload


@pytest.mark.properties
@given(sid=stream_id, data=ws_payload)
def test_data_frame_round_trip(sid, data):
    """serialize → parse → payload and stream_id are preserved."""
    factory = FrameFactory()
    frame = factory.create(FrameTypes.DATA, DataFrameFlags.END_STREAM, sid, data=data)
    raw = frame.save()
    parsed = factory.load(raw)
    assert parsed.stream_id == sid
    assert parsed.payload == data


@pytest.mark.properties
@given(sid=stream_id, data=ws_payload)
def test_frame_header_length_matches_payload(sid, data):
    """The 3-byte length field in the frame header must equal len(payload)."""
    factory = FrameFactory()
    frame = factory.create(FrameTypes.DATA, 0, sid, data=data)
    raw = frame.save()
    declared_length = int.from_bytes(raw[:3], 'big')
    assert declared_length == len(data)
