from __future__ import annotations

import asyncio

import pytest

from blackbull.server.http2_connection import SKIP
from blackbull.client.http2 import (HTTP2Client, _ConnectionFailed,
                                    _PendingResponse)
from blackbull.protocol.frame_types import ErrorCodes, FrameTypes
from blackbull.server.recipient import AbstractReader
from blackbull.server.sender import AbstractWriter


class _Writer(AbstractWriter):
    def __init__(self) -> None:
        self.frames: list[bytes] = []

    async def write(self, data: bytes) -> None:
        self.frames.append(data)


class _Reader(AbstractReader):
    async def read(self, n: int) -> bytes:
        raise AssertionError('the validation tests call _load directly')

    async def readexactly(self, n: int) -> bytes:
        raise AssertionError('the validation tests call _load directly')


def _wire(type_: FrameTypes, payload: bytes, *, stream_id: int = 1,
          flags: int = 0, declared_length: int | None = None) -> bytes:
    length = len(payload) if declared_length is None else declared_length
    return (length.to_bytes(3, 'big') + type_.value + bytes([flags])
            + stream_id.to_bytes(4, 'big') + payload)


def _client() -> tuple[HTTP2Client, _Writer]:
    client = HTTP2Client('localhost', 1)
    writer = _Writer()
    client._writer = writer
    client._reader = _Reader()

    async def send(frame) -> None:
        writer.frames.append(frame.save())

    client._control_sender = send
    return client, writer


async def _process(client: HTTP2Client, data: bytes) -> None:
    """One frame through the receive loop's admission and dispatch."""
    frame = await client._load(data)
    stream = await client._admit(frame)
    if stream is not SKIP:
        await client._dispatch(frame, frame.FrameType(), stream)


def _written_error(writer: _Writer) -> tuple[FrameTypes, int, int, int]:
    raw = writer.frames[-1]
    payload_offset = 13 if raw[3:4] == FrameTypes.GOAWAY.value else 9
    return (FrameTypes(raw[3:4]), raw[4], int.from_bytes(raw[5:9], 'big'),
            int.from_bytes(raw[payload_offset:payload_offset + 4], 'big'))


@pytest.mark.asyncio
async def test_malformed_headers_reset_only_its_stream_before_raw_routing():
    client, writer = _client()
    client._responses[1] = _PendingResponse(
        future=asyncio.get_running_loop().create_future())

    # PADDED requires a pad-length octet whose value is smaller than the
    # complete frame payload; this is a connection framing error.
    with pytest.raises(_ConnectionFailed):
        await client._load(_wire(
            FrameTypes.HEADERS, b'\x01', stream_id=1, flags=0x0C,
        ))
    assert _written_error(writer) == (
        FrameTypes.GOAWAY, 0, 0, int(ErrorCodes.PROTOCOL_ERROR))


@pytest.mark.asyncio
async def test_priority_rejection_preserves_unrelated_pending_stream():
    client, writer = _client()
    rejected = asyncio.get_running_loop().create_future()
    survivor = asyncio.get_running_loop().create_future()
    for stream_id, future in ((1, rejected), (3, survivor)):
        client._open_stream(stream_id)
        client._responses[stream_id] = _PendingResponse(future=future)
    await _process(client, _wire(FrameTypes.PRIORITY, (1).to_bytes(4, 'big') + b'\x00',
                                 stream_id=1))
    assert 1 not in client._responses
    assert 3 in client._responses
    assert not survivor.done()
    assert writer.frames[-1][3:4] == FrameTypes.RST_STREAM.value


@pytest.mark.asyncio
async def test_raw_stream_rejection_returns_displaced_data_credit():
    client, writer = _client()
    queue = asyncio.Queue(maxsize=1)
    queue.put_nowait(client.factory.create(FrameTypes.DATA, 0, 3,
                                            data=b'payload'))
    client._raw_streams[3] = queue

    await client._reject_stream(3, ErrorCodes.PROTOCOL_ERROR, 'bad frame')

    assert 3 not in client._raw_streams
    assert client._unacked_conn == 7
    assert writer.frames[-1][3:4] == FrameTypes.RST_STREAM.value


@pytest.mark.asyncio
async def test_raw_rejection_credits_threshold_once_and_not_control_frames():
    client, writer = _client()
    queue = asyncio.Queue(maxsize=1)
    queue.put_nowait(client.factory.create(
        FrameTypes.DATA, 0, 3, data=b'x' * 32768))
    client._raw_streams[3] = queue
    await client._reject_stream(3, ErrorCodes.PROTOCOL_ERROR, 'bad data')
    updates = [raw for raw in writer.frames if raw[3:4] == FrameTypes.WINDOW_UPDATE.value]
    assert len(updates) == 1
    assert int.from_bytes(updates[0][9:13], 'big') == 32768
    assert client._unacked_conn == 0

    queue = asyncio.Queue(maxsize=1)
    queue.put_nowait(client.factory.create(FrameTypes.PRIORITY, 0, 5,
                                            data=b'12345'))
    client._raw_streams[5] = queue
    await client._reject_stream(5, ErrorCodes.PROTOCOL_ERROR, 'bad control')
    assert len([raw for raw in writer.frames
                if raw[3:4] == FrameTypes.WINDOW_UPDATE.value]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('type_', 'payload', 'stream_id', 'flags'),
    [
        (FrameTypes.WINDOW_UPDATE, b'1234', 1, 0),
        (FrameTypes.PRIORITY, b'12345', 1, 0),
        (FrameTypes.GOAWAY, b'12345678', 0, 0),
        (FrameTypes.SETTINGS, b'123456', 0, 0),
        (FrameTypes.SETTINGS, b'', 0, 1),
    ],
)
async def test_valid_control_frame_lengths_are_accepted(type_, payload,
                                                          stream_id, flags):
    client, writer = _client()
    result = await client._load(_wire(type_, payload, stream_id=stream_id,
                                      flags=flags))
    assert result is not None
    assert writer.frames == []


@pytest.mark.asyncio
async def test_goaway_payload_longer_than_eight_bytes_is_accepted():
    client, writer = _client()
    payload = (3).to_bytes(4, 'big') + int(ErrorCodes.NO_ERROR).to_bytes(4, 'big') + b'x'
    frame = await client._load(_wire(FrameTypes.GOAWAY, payload, stream_id=0))
    assert frame is not None
    assert frame.last_stream_id == 3
    assert frame.append_data == b'x'
    assert writer.frames == []


@pytest.mark.asyncio
async def test_raw_priority_rejection_isolated_from_unrelated_raw_stream():
    client, writer = _client()
    first = asyncio.Queue(maxsize=1)
    second = asyncio.Queue(maxsize=1)
    client._raw_streams[3] = first
    client._raw_streams[5] = second
    client._open_stream(3)
    client._open_stream(5)
    sender = object()
    client._senders[5] = sender

    await _process(client, _wire(FrameTypes.PRIORITY,
                                 (3).to_bytes(4, 'big') + b'\x00', stream_id=3))

    assert 3 not in client._raw_streams
    assert client._raw_streams[5] is second
    assert second.empty()
    assert client._senders[5] is sender
    assert writer.frames[-1][3:4] == FrameTypes.RST_STREAM.value
