"""End-to-end HTTP/2 with connection-level segment coalescing enabled
(Sprint 59, connection-level-tcp-segment-coalescing).

The coalescer batches per-stream response frames.  The load-bearing
correctness property is **wire order**: because response header blocks are
HPACK-encoded into the connection's shared encoder immediately before they are
handed to the coalescer, the coalescer's FIFO flush must keep them in encode
order — otherwise the peer's HPACK decoder desyncs and every subsequent header
block corrupts.  These tests drive real responses across two streams with
``BB_H2_CONN_BUFFER_US`` enabled and decode what reaches the wire with a single
HPACK decoder, so a reordering regression fails loudly.
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock
from hpack import Encoder, Decoder

from blackbull.env import reset_settings_cache
from blackbull.server.http2_actor import HTTP2Actor
from blackbull.server.sender import AsyncioWriter
from blackbull.protocol.frame_types import (FrameTypes, HeaderFrameFlags,
                                            FrameFlags)


def _make_h2_frame(type_byte, flags, stream_id: int, payload: bytes) -> bytes:
    return (len(payload).to_bytes(3, 'big') + type_byte + bytes([flags])
            + stream_id.to_bytes(4, 'big') + payload)


def _make_get(stream_id: int, path: bytes, encoder: Encoder) -> bytes:
    block = encoder.encode([(b':method', b'GET'), (b':path', path),
                            (b':scheme', b'https')])
    flags: FrameFlags = HeaderFrameFlags.END_HEADERS | HeaderFrameFlags.END_STREAM
    return _make_h2_frame(FrameTypes.HEADERS, flags, stream_id, block)


def _iter_frames(buf: bytes):
    i = 0
    while i + 9 <= len(buf):
        length = int.from_bytes(buf[i:i + 3], 'big')
        # FrameTypes is a bytes-valued enum, so keep the type byte as bytes.
        ftype, flags = buf[i + 3:i + 4], buf[i + 4]
        sid = int.from_bytes(buf[i + 5:i + 9], 'big')
        payload = buf[i + 9:i + 9 + length]
        yield ftype, flags, sid, payload
        i += 9 + length


@pytest.fixture
def coalescing_enabled(monkeypatch):
    """Enable a 40ms coalescing window for the duration of a test."""
    monkeypatch.setenv('BB_H2_CONN_BUFFER_US', '40000')
    reset_settings_cache()
    yield
    reset_settings_cache()


def _build_actor(app):
    """An HTTP2Actor over a recording writer, with control frames mocked out
    (only stream responses reach the writer).  Returns (actor, writer_mock)."""
    writer = MagicMock()
    writer.drain = AsyncMock()
    writer.close = MagicMock()
    actor = HTTP2Actor(None, AsyncioWriter(writer), app, aggregator=None)
    actor.send_frame = AsyncMock()
    return actor, writer


def _decode_response_statuses(writer) -> list[tuple[int, bytes]]:
    """Walk everything written to the transport, decode HEADERS blocks with a
    single decoder (wire order), and return (stream_id, :status) pairs."""
    buf = b''.join(call.args[0] for call in writer.write.call_args_list)
    decoder = Decoder()
    out: list[tuple[int, bytes]] = []
    for ftype, flags, sid, payload in _iter_frames(buf):
        if ftype == FrameTypes.HEADERS:
            headers = dict(decoder.decode(payload))
            out.append((sid, headers.get(':status')))
    return out


@pytest.mark.asyncio
async def test_coalescing_disabled_by_default_still_delivers(monkeypatch):
    """Sanity: with the feature off (default), two responses decode fine."""
    reset_settings_cache()

    async def app(scope, receive, send):
        await send({'type': 'http.response.start', 'status': 200,
                    'headers': [(b'content-type', b'text/plain')]})
        await send({'type': 'http.response.body', 'body': b'ok'})

    enc = Encoder()
    actor, writer = _build_actor(app)
    actor.receive = AsyncMock(side_effect=[
        _make_get(1, b'/a', enc), _make_get(3, b'/b', enc), None])
    await actor.run()

    statuses = _decode_response_statuses(writer)
    assert [sid for sid, _ in statuses] == [1, 3]
    assert all(status == '200' for _, status in statuses)


@pytest.mark.asyncio
async def test_two_streams_decode_in_order_with_coalescing(coalescing_enabled):
    """With coalescing ON, both stream responses must still decode against a
    single HPACK decoder — proving FIFO flush preserved encode order."""
    async def app(scope, receive, send):
        await send({'type': 'http.response.start', 'status': 200,
                    'headers': [(b'content-type', b'text/plain'),
                                (b'x-path', scope['path'].encode())]})
        await send({'type': 'http.response.body', 'body': b'body'})

    enc = Encoder()
    actor, writer = _build_actor(app)
    actor.receive = AsyncMock(side_effect=[
        _make_get(1, b'/first', enc), _make_get(3, b'/second', enc), None])
    await actor.run()

    statuses = _decode_response_statuses(writer)
    # Both streams answered, decoded cleanly (no HPACK desync), stream 1's
    # header block ahead of stream 3's on the wire (encode order).
    assert [sid for sid, _ in statuses] == [1, 3], (
        f'expected stream 1 then 3 in wire order; got {statuses}')
    assert all(status == '200' for _, status in statuses)


@pytest.mark.asyncio
async def test_trailing_batch_flushed_on_teardown(coalescing_enabled):
    """A response held in the coalescing window when the connection tears down
    must still be delivered (the run()/GOAWAY flush path), not dropped."""
    async def app(scope, receive, send):
        await send({'type': 'http.response.start', 'status': 200, 'headers': []})
        await send({'type': 'http.response.body', 'body': b'x'})

    enc = Encoder()
    actor, writer = _build_actor(app)
    # Three streams: at least one response is buffered behind the immediate
    # first write and only leaves via the teardown flush.
    actor.receive = AsyncMock(side_effect=[
        _make_get(1, b'/1', enc), _make_get(3, b'/3', enc),
        _make_get(5, b'/5', enc), None])
    await actor.run()

    statuses = _decode_response_statuses(writer)
    assert sorted(sid for sid, _ in statuses) == [1, 3, 5], (
        f'all three responses must survive teardown; got {statuses}')
    assert all(status == '200' for _, status in statuses)
