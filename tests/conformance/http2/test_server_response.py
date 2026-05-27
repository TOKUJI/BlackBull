"""Tests for blackbull/server/response.py — Responder registry and respond() paths."""
import asyncio
import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from blackbull.server.response import (
    ResponderFactory, Responder, PingResponder,
    WindowUpdateResponder, PriorityResponder, PriorityUpdateResponder,
    SettingsResponder,
)
from blackbull.protocol.frame import FrameFactory
from blackbull.protocol.frame_types import (
    FrameTypes, SettingFrameFlags, DataFrameFlags, DEFAULT_MAX_FRAME_SIZE,
)


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
    frame.flags = 0  # not a PING-ACK; we must echo it back
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
    frame.length = 4  # valid frame length
    frame.window_size = 1024

    sender_a = MagicMock()
    sender_a.connection_window_size = 0
    sender_b = MagicMock()
    sender_b.connection_window_size = 100

    handler = MagicMock()
    handler._senders = {'a': sender_a, 'b': sender_b}
    handler._connection_window_size = 65535

    responder = WindowUpdateResponder(frame)
    await responder.respond(handler)

    assert sender_a.connection_window_size == 1024
    assert sender_b.connection_window_size == 1124
    sender_a.wake_window.assert_called_once()
    sender_b.wake_window.assert_called_once()


@pytest.mark.asyncio
async def test_window_update_connection_level_updates_handler_tracking():
    """Connection-level WINDOW_UPDATE must update handler._connection_window_size
    so that stream senders created after the update inherit the correct budget."""
    frame = MagicMock()
    frame.stream_id = 0
    frame.length = 4  # valid frame length
    frame.window_size = 4128769  # default startup increment (4 MiB - 65535)

    handler = MagicMock()
    handler._senders = {}
    handler._connection_window_size = 65535

    responder = WindowUpdateResponder(frame)
    await responder.respond(handler)

    assert handler._connection_window_size == 65535 + 4128769


# ---------------------------------------------------------------------------
# WindowUpdateResponder — stream-level (stream_id > 0)
# ---------------------------------------------------------------------------

def _decode_h2_headers(written: bytes, factory: FrameFactory) -> list[tuple[bytes, bytes]]:
    """Decode the HEADERS-frame payload in ``written`` via the factory's HPACK decoder.

    Returns the decoded header list (including the ``:status`` pseudo-header
    as a regular entry).  Assumes a single HEADERS frame at offset 0 and
    raises if no HEADERS frame is present.
    """
    from blackbull.protocol.frame_types import FrameTypes
    if not written:
        raise AssertionError('no bytes were written')
    length = int.from_bytes(written[0:3], 'big')
    type_byte = written[3:4]
    if type_byte != FrameTypes.HEADERS.value:
        raise AssertionError(
            f'expected HEADERS frame, got type byte {type_byte!r}'
        )
    payload = written[9:9 + length]
    return factory.decoder.decode(payload)


# ---------------------------------------------------------------------------
# RFC 9110 §6.6.1 — Date SHOULD be present in HTTP responses.
# Sprint 11 brought HTTP/2 to parity with HTTP/1.1 here; the cache lives
# in _http_date() so the per-response cost is one int comparison.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_http2_sender_auto_emits_date_on_bytes_path():
    """Bytes-payload responses get a ``date`` header even when the app sent none."""
    from blackbull.server.sender import HTTP2Sender, AsyncioWriter

    written = bytearray()
    mock_writer = MagicMock()
    mock_writer.write = MagicMock(side_effect=lambda d: written.extend(d))
    mock_writer.drain = AsyncMock()

    factory = FrameFactory()
    sender = HTTP2Sender(AsyncioWriter(mock_writer), factory, stream_id=1)

    await sender(b'pong')

    decoded = _decode_h2_headers(bytes(written), factory)
    names = [k.lower() if isinstance(k, (bytes, bytearray)) else k.lower().encode()
             for k, _ in decoded]
    assert b'date' in names, f'expected auto-emitted date header; got {names!r}'


@pytest.mark.asyncio
async def test_http2_sender_auto_emits_date_on_asgi_event_path():
    """ASGI streaming path (``http.response.start``) also gets the auto-date."""
    from blackbull.server.sender import HTTP2Sender, AsyncioWriter
    from blackbull.asgi import ASGIEvent

    written = bytearray()
    mock_writer = MagicMock()
    mock_writer.write = MagicMock(side_effect=lambda d: written.extend(d))
    mock_writer.drain = AsyncMock()

    factory = FrameFactory()
    sender = HTTP2Sender(AsyncioWriter(mock_writer), factory, stream_id=1)

    await sender({'type': ASGIEvent.HTTP_RESPONSE_START, 'status': 200, 'headers': []})

    decoded = _decode_h2_headers(bytes(written), factory)
    names = [k.lower() if isinstance(k, (bytes, bytearray)) else k.lower().encode()
             for k, _ in decoded]
    assert b'date' in names, f'expected auto-emitted date header; got {names!r}'


@pytest.mark.asyncio
async def test_http2_sender_does_not_duplicate_app_provided_date():
    """If the app already sent a Date header, the auto-emit must not duplicate it.

    Case-insensitive check (RFC 9110 §5.1): ``Date``, ``date``, ``DATE`` all match.
    """
    from blackbull.server.sender import HTTP2Sender, AsyncioWriter

    written = bytearray()
    mock_writer = MagicMock()
    mock_writer.write = MagicMock(side_effect=lambda d: written.extend(d))
    mock_writer.drain = AsyncMock()

    factory = FrameFactory()
    sender = HTTP2Sender(AsyncioWriter(mock_writer), factory, stream_id=1)

    custom_date = b'Wed, 01 Jan 2025 00:00:00 GMT'
    await sender(b'pong', headers=[(b'Date', custom_date)])

    decoded = _decode_h2_headers(bytes(written), factory)
    date_values = [
        (v if isinstance(v, (bytes, bytearray)) else v.encode())
        for k, v in decoded
        if (k.lower() if isinstance(k, (bytes, bytearray)) else k.lower().encode()) == b'date'
    ]
    assert len(date_values) == 1, f'expected exactly one date header; got {date_values!r}'
    assert bytes(date_values[0]) == custom_date, (
        f'app-provided Date must win; got {date_values[0]!r}'
    )


@pytest.mark.asyncio
async def test_window_update_stream_level_only_credits_stream_window():
    """Stream-level WINDOW_UPDATE must not inflate the connection window.

    Regression: sender.window_update() previously incremented both
    connection_window_size and stream_window_size, over-crediting the
    connection-level flow-control budget on every stream-level update.
    """
    from blackbull.server.sender import HTTP2Sender, AsyncioWriter

    mock_writer = MagicMock()
    mock_writer.write = MagicMock()
    mock_writer.drain = AsyncMock()
    factory = FrameFactory()

    sender = HTTP2Sender(AsyncioWriter(mock_writer), factory, stream_id=1)
    original_conn_window = sender.connection_window_size

    sender.window_update(512)

    assert sender.stream_window_size[1] == 65535 + 512
    assert sender.connection_window_size == original_conn_window, (
        "stream-level WINDOW_UPDATE must not change connection_window_size"
    )


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


# ---------------------------------------------------------------------------
# DATA frame splitting — RFC 7540 §6.9 and §4.2
# ---------------------------------------------------------------------------

def _collect_data_frames(written: bytearray, factory: FrameFactory) -> list:
    """Parse all DATA frames from the accumulated wire bytes."""
    frames = []
    mv = memoryview(written)
    offset = 0
    while offset + 9 <= len(mv):
        length = int.from_bytes(mv[offset:offset + 3], 'big')
        ftype = bytes(mv[offset + 3:offset + 4])
        if offset + 9 + length > len(mv):
            break
        frame_bytes = bytes(mv[offset:offset + 9 + length])
        if ftype == FrameTypes.DATA:
            frames.append(factory.load(frame_bytes))
        offset += 9 + length
    return frames


@pytest.mark.asyncio
async def test_large_body_split_into_multiple_data_frames():
    """Bodies larger than max_frame_size must be split into multiple DATA frames.

    Regression for the RFC 7540 §4.2 max-frame-size violation: the old
    _write_data() sent the entire body as a single DATA frame, which exceeds
    SETTINGS_MAX_FRAME_SIZE (16384) for large responses.
    """
    from blackbull.server.sender import HTTP2Sender, AsyncioWriter

    written = bytearray()
    mock_writer = MagicMock()
    mock_writer.write = MagicMock(side_effect=lambda d: written.extend(d))
    mock_writer.drain = AsyncMock()

    factory = FrameFactory()
    sender = HTTP2Sender(AsyncioWriter(mock_writer), factory, stream_id=1)
    sender.max_frame_size = 100
    body = b'x' * 350

    await sender._write_data(body, end_stream=True)

    frames = _collect_data_frames(written, factory)
    assert len(frames) >= 2, f'Expected multiple DATA frames, got {len(frames)}'
    for f in frames:
        assert f.length <= 100, f'Frame payload {f.length} exceeds max_frame_size 100'
    assert frames[-1].end_stream, 'END_STREAM must be set on the last DATA frame'
    assert not any(f.end_stream for f in frames[:-1]), 'END_STREAM must only be on the last frame'
    assert b''.join(bytes(f.payload) for f in frames) == body, 'Reassembled body does not match'


@pytest.mark.asyncio
async def test_body_exceeding_flow_control_window_is_chunked():
    """Bodies larger than the flow-control window must wait for WINDOW_UPDATE.

    Regression for the RFC 7540 §6.9 flow-control deadlock: the old
    _write_data() blocked forever trying to send a body larger than the
    connection window, because no data ever reached the client to trigger
    a WINDOW_UPDATE.
    """
    from blackbull.server.sender import HTTP2Sender, AsyncioWriter

    written = bytearray()
    mock_writer = MagicMock()
    mock_writer.write = MagicMock(side_effect=lambda d: written.extend(d))
    mock_writer.drain = AsyncMock()

    factory = FrameFactory()
    sender = HTTP2Sender(AsyncioWriter(mock_writer), factory, stream_id=1)
    sender.max_frame_size = 300
    sender.connection_window_size = 200
    sender.stream_window_size[1] = 200

    body = b'y' * 500

    task = asyncio.create_task(sender._write_data(body, end_stream=True))
    await asyncio.sleep(0)  # let the first chunk send

    frames_after_first_chunk = _collect_data_frames(written, factory)
    assert len(frames_after_first_chunk) >= 1, 'First chunk must be sent without waiting'
    assert not frames_after_first_chunk[-1].end_stream, 'END_STREAM must not be set mid-stream'

    # Provide more credit so the rest can send
    sender.connection_window_size += 400
    sender.window_update(400)
    await task

    frames = _collect_data_frames(written, factory)
    assert frames[-1].end_stream, 'END_STREAM must be set on the final frame'
    assert b''.join(bytes(f.payload) for f in frames) == body


@pytest.mark.asyncio
async def test_settings_responder_propagates_max_frame_size_to_senders():
    """SETTINGS_MAX_FRAME_SIZE from the peer must update sender.max_frame_size.

    After applying the SETTINGS frame, future DATA frames must be split at
    the new max_frame_size boundary.
    """
    from blackbull.server.sender import HTTP2Sender, AsyncioWriter

    written = bytearray()
    mock_writer = MagicMock()
    mock_writer.write = MagicMock(side_effect=lambda d: written.extend(d))
    mock_writer.drain = AsyncMock()

    frame_factory = FrameFactory()
    sender = HTTP2Sender(AsyncioWriter(mock_writer), frame_factory, stream_id=1)
    assert sender.max_frame_size == DEFAULT_MAX_FRAME_SIZE

    # Build a SETTINGS frame advertising MAX_FRAME_SIZE = 32768
    settings_payload = (0x5).to_bytes(2, 'big') + (32768).to_bytes(4, 'big')
    wire = _make_h2_frame(FrameTypes.SETTINGS, SettingFrameFlags.INIT, 0, settings_payload)
    settings_frame = frame_factory.load(wire)

    handler = SimpleNamespace(
        factory=frame_factory,
        _senders={1: sender},
        send_frame=AsyncMock(),
    )
    await SettingsResponder(settings_frame).respond(handler)

    assert sender.max_frame_size == 32768
