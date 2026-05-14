"""Tests for blackbull/server/response.py — Responder registry and respond() paths."""
import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from blackbull.server.response import (
    ResponderFactory, Responder, PingResponder,
    WindowUpdateResponder, PriorityResponder, PriorityUpdateResponder,
    SettingsResponder,
)
from blackbull.protocol.frame import FrameFactory
from blackbull.protocol.frame_types import FrameTypes, SettingFrameFlags


def _make_h2_frame(type_byte, flags, stream_id: int, payload: bytes) -> bytes:
    length = len(payload)
    return (length.to_bytes(3, 'big')
            + type_byte
            + bytes([flags])
            + stream_id.to_bytes(4, 'big')
            + payload)


# ---------------------------------------------------------------------------
# ResponderFactory
# ---------------------------------------------------------------------------

def test_create_unknown_frame_type_raises():
    frame = MagicMock()
    frame.FrameType.return_value = object()  # not in registry
    with pytest.raises(ValueError, match='Unsupported FrameType'):
        ResponderFactory.create(frame)


# ---------------------------------------------------------------------------
# Responder base class — registration guards and abstract methods
# ---------------------------------------------------------------------------

def test_subclass_frame_type_none_not_registered():
    before = dict(Responder._registry)

    class AbstractSub(Responder):
        FRAME_TYPE = None

    assert Responder._registry == before  # nothing new added


def test_subclass_duplicate_frame_type_raises():
    with pytest.raises(ValueError, match='Duplicate FRAME_TYPE'):
        class DupA(Responder):
            FRAME_TYPE = 'test_dup_sentinel'

        class DupB(Responder):
            FRAME_TYPE = 'test_dup_sentinel'

    # Clean up so we don't pollute registry for other tests
    Responder._registry.pop('test_dup_sentinel', None)


@pytest.mark.asyncio
async def test_respond_not_implemented():
    frame = MagicMock()
    r = Responder(frame)
    with pytest.raises(NotImplementedError):
        await r.respond(MagicMock())


def test_frame_type_not_implemented():
    with pytest.raises(NotImplementedError):
        Responder.FrameType()


# ---------------------------------------------------------------------------
# PingResponder
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ping_responder_sends_ack():
    frame = MagicMock()
    frame.stream_id = 0
    frame.payload = b'\x00' * 8

    ack_frame = MagicMock()
    handler = MagicMock()
    handler.factory.create.return_value = ack_frame
    handler.send_frame = AsyncMock()

    responder = PingResponder(frame)
    await responder.respond(handler)

    handler.send_frame.assert_awaited_once_with(ack_frame)


# ---------------------------------------------------------------------------
# WindowUpdateResponder — connection-level (stream_id == 0)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_window_update_connection_level_credits_all_senders():
    frame = MagicMock()
    frame.stream_id = 0
    frame.window_size = 1024

    sender_a = MagicMock()
    sender_a.connection_window_size = 0
    sender_b = MagicMock()
    sender_b.connection_window_size = 100

    handler = MagicMock()
    handler._senders = {'a': sender_a, 'b': sender_b}

    responder = WindowUpdateResponder(frame)
    await responder.respond(handler)

    assert sender_a.connection_window_size == 1024
    assert sender_b.connection_window_size == 1124
    sender_a._window_open.set.assert_called_once()
    sender_b._window_open.set.assert_called_once()


# ---------------------------------------------------------------------------
# PriorityResponder — existing stream and new stream branches
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_priority_responder_updates_existing_stream():
    frame = MagicMock()
    frame.stream_id = 3
    frame.dependent_stream = 0
    frame.weight = 42
    frame.exclusion = False

    existing = MagicMock()
    handler = MagicMock()
    handler.root_stream.find_child.return_value = existing

    responder = PriorityResponder(frame)
    await responder.respond(handler)

    assert existing.weight == 42


@pytest.mark.asyncio
async def test_priority_responder_creates_new_stream():
    frame = MagicMock()
    frame.stream_id = 5
    frame.dependent_stream = 1
    frame.weight = 10
    frame.exclusion = False

    new_stream = MagicMock()
    handler = MagicMock()
    # First find_child returns None (stream doesn't exist yet)
    # Second call (for dependent_stream) returns a parent stub
    parent_stub = MagicMock()
    parent_stub.add_child.return_value = new_stream
    handler.root_stream.find_child.side_effect = [None, parent_stub]

    responder = PriorityResponder(frame)
    await responder.respond(handler)

    parent_stub.add_child.assert_called_once_with(5)
    assert new_stream.weight == 10


# ---------------------------------------------------------------------------
# PriorityUpdateResponder — existing and new stream branches
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_priority_update_existing_stream_stores_hint():
    frame = MagicMock()
    frame.prioritized_stream_id = 3
    frame.parsed_priority = {'urgency': 2}
    frame.priority_field = 'u=2'

    stream = MagicMock()
    stream.scope = {}
    handler = MagicMock()
    handler.find_stream.return_value = stream

    responder = PriorityUpdateResponder(frame)
    await responder.respond(handler)

    assert stream.priority_hint == {'urgency': 2}
    assert stream.scope['http2_priority'] == {'urgency': 2}


@pytest.mark.asyncio
async def test_priority_update_new_stream_precreated():
    frame = MagicMock()
    frame.prioritized_stream_id = 7
    frame.parsed_priority = {'urgency': 1}
    frame.priority_field = 'u=1'

    new_stream = MagicMock()
    new_stream.scope = None  # scope not yet available

    handler = MagicMock()
    # First call: None (stream doesn't exist), second call: the new stream
    handler.find_stream.side_effect = [None, new_stream]

    responder = PriorityUpdateResponder(frame)
    await responder.respond(handler)

    handler.root_stream.add_child.assert_called_once_with(7)
    assert new_stream.priority_hint == {'urgency': 1}


# ---------------------------------------------------------------------------
# RFC 7540 §6.5.2 — SettingsResponder must not mutate our HPACK decoder
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_settings_responder_does_not_mutate_decoder_table_size():
    """Per RFC 7540 §6.5.2, peer's SETTINGS_HEADER_TABLE_SIZE bounds OUR encoder only.

    Regression: the old SettingsResponder set handler.factory.header_table_size,
    which mutated the decoder via FrameFactory.__setattr__.  Subsequent HEADERS
    decoding raised hpack InvalidTableSizeError unless the peer's encoder also
    sent a Dynamic Table Size Update — which browsers never do, causing a silent
    connection failure.
    """
    factory = FrameFactory()
    before = factory.decoder.header_table_size

    wire = _make_h2_frame(
        FrameTypes.SETTINGS,
        SettingFrameFlags.INIT,
        0,
        (0x1).to_bytes(2, 'big') + (65536).to_bytes(4, 'big'),
    )
    frame = factory.load(wire)

    handler = SimpleNamespace(
        factory=factory,
        _senders={},
        send_frame=AsyncMock(),
    )
    await SettingsResponder(frame).respond(handler)

    assert factory.decoder.header_table_size == before, (
        f'SettingsResponder must NOT change factory.decoder.header_table_size; '
        f'was {before}, now {factory.decoder.header_table_size}'
    )
