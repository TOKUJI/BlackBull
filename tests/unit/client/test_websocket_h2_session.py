"""Sprint 72 — WebSocketH2Session on the shared WS codec (1.20b / F.2, 1.20c).

The session previously rolled a third, private WebSocket frame parser that
ignored FIN, RSV, and MASK bits entirely — no fragmentation reassembly, no
control-frame handling.  It now reuses the same ``WebSocketRecipient`` /
``FragmentAssembler`` stack as the server and the H1 client, fed by a small
``AbstractReader`` adapter over the raw-stream DATA-frame queue.

Tests drive a real ``HTTP2Client`` (never connected: ``_writer`` stubbed
with a byte-capturing writer, ``send_raw_frame`` recorded) and hand-feed
``FrameFactory``-loaded DATA frames into the raw-stream queue.
"""
from __future__ import annotations

import asyncio
import struct

import pytest

from blackbull.client.http2 import HTTP2Client
from blackbull.client.websocket_h2 import WebSocketH2Session
from blackbull.protocol.frame import FrameFactory
from blackbull.protocol.frame_types import DataFrameFlags, FrameTypes
from blackbull.server.sender import AbstractWriter
from blackbull.server.ws_codec import WSFrameBits, WSOpcode, encode_frame


class _CaptureWriter(AbstractWriter):
    def __init__(self) -> None:
        self.data = bytearray()

    async def write(self, data: bytes) -> None:
        self.data += data


def _make_session(stream_id: int = 1):
    client = HTTP2Client('localhost', 1)
    writer = _CaptureWriter()
    client._writer = writer
    sent_raw: list = []

    async def _record_raw(frame):
        sent_raw.append(frame)

    client.send_raw_frame = _record_raw  # type: ignore[method-assign]
    queue = client.register_raw_stream(stream_id)
    factory = FrameFactory()
    session = WebSocketH2Session(client, factory, stream_id, queue)
    return session, queue, factory, writer, sent_raw


def _data_frame(factory, ws_bytes: bytes, stream_id: int = 1,
                end_stream: bool = False):
    flags = DataFrameFlags.END_STREAM if end_stream else 0
    return factory.create(FrameTypes.DATA, flags, stream_id, data=ws_bytes)


def _fragment(payload: bytes, opcode: WSOpcode | int, fin: bool) -> bytes:
    """Server→client (unmasked) WS frame with an explicit FIN bit."""
    frame = bytearray(encode_frame(payload, opcode=opcode))
    if not fin:
        frame[0] &= ~WSFrameBits.FIN & 0xFF
    return bytes(frame)


def _sent_ws_bytes(writer: _CaptureWriter, stream_id: int = 1) -> bytes:
    """Extract the WS bytes the session wrote, out of captured H2 frames."""
    factory = FrameFactory()
    buf = bytes(writer.data)
    out = bytearray()
    while buf:
        length = int.from_bytes(buf[:3], 'big')
        frame = factory.load(buf[:9 + length])
        if (frame.FrameType() == FrameTypes.DATA
                and frame.stream_id == stream_id):
            out += frame.payload
        buf = buf[9 + length:]
    return bytes(out)


# ═══════════════════════════════════════════════════════════════════════
# 1.20b / F.2 — shared-codec receive path
# ═══════════════════════════════════════════════════════════════════════

class TestSharedCodecReceive:
    @pytest.mark.asyncio
    async def test_fragmented_message_reassembled(self):
        """RFC 6455 §5.4 — TEXT(FIN=0) + CONTINUATION(FIN=1) is one message.
        The old private parser ignored FIN and returned the first fragment
        as a complete message."""
        session, queue, factory, writer, _ = _make_session()
        queue.put_nowait(_data_frame(
            factory, _fragment(b'Hel', WSOpcode.TEXT, fin=False)))
        queue.put_nowait(_data_frame(
            factory, _fragment(b'lo', WSOpcode.CONTINUATION, fin=True)))
        opcode, payload = await session.receive(timeout=2.0)
        assert (opcode, payload) == (WSOpcode.TEXT, b'Hello')

    @pytest.mark.asyncio
    async def test_ws_frame_split_across_data_frames(self):
        """One WS frame's bytes may span several H2 DATA frames."""
        session, queue, factory, writer, _ = _make_session()
        ws = encode_frame(b'spanning', opcode=WSOpcode.BINARY)
        queue.put_nowait(_data_frame(factory, ws[:3]))
        queue.put_nowait(_data_frame(factory, ws[3:]))
        opcode, payload = await session.receive(timeout=2.0)
        assert (opcode, payload) == (WSOpcode.BINARY, b'spanning')

    @pytest.mark.asyncio
    async def test_ping_is_auto_ponged_masked(self):
        """RFC 6455 §5.5.2/§5.5.3 — PING answered with a PONG carrying the
        same payload; §5.1 — client frames MUST be masked."""
        session, queue, factory, writer, _ = _make_session()
        queue.put_nowait(_data_frame(
            factory, encode_frame(b'probe', opcode=WSOpcode.PING)))
        queue.put_nowait(_data_frame(
            factory, encode_frame(b'after', opcode=WSOpcode.TEXT)))
        opcode, payload = await session.receive(timeout=2.0)
        assert (opcode, payload) == (WSOpcode.TEXT, b'after')
        pong = _sent_ws_bytes(writer)
        assert pong, 'no PONG bytes written'
        assert pong[0] & 0x0F == WSOpcode.PONG
        assert pong[1] & WSFrameBits.MASK_BIT, 'client PONG must be masked'

    @pytest.mark.asyncio
    async def test_server_close_yields_close_tuple(self):
        session, queue, factory, writer, _ = _make_session()
        queue.put_nowait(_data_frame(
            factory,
            encode_frame((1000).to_bytes(2, 'big'), opcode=WSOpcode.CLOSE)))
        opcode, payload = await session.receive(timeout=2.0)
        assert opcode == WSOpcode.CLOSE
        assert payload == struct.pack('>H', 1000)

    @pytest.mark.asyncio
    async def test_end_stream_without_close_disconnects(self):
        """END_STREAM with no CLOSE frame = abnormal closure (1006)."""
        session, queue, factory, writer, _ = _make_session()
        queue.put_nowait(_data_frame(factory, b'', end_stream=True))
        opcode, payload = await session.receive(timeout=2.0)
        assert opcode == WSOpcode.CLOSE
        assert payload == struct.pack('>H', 1006)

    @pytest.mark.asyncio
    async def test_receive_timeout_message_shape(self):
        session, queue, factory, writer, _ = _make_session()
        with pytest.raises(TimeoutError, match='no WebSocket frame within'):
            await session.receive(timeout=0.05)
        await session.close(drain_timeout=0.05)


# ═══════════════════════════════════════════════════════════════════════
# 1.20c — close() drains the peer CLOSE and stops the recipient
# ═══════════════════════════════════════════════════════════════════════

class TestCloseDiscipline:
    @pytest.mark.asyncio
    async def test_close_completes_when_peer_echoes_close(self):
        session, queue, factory, writer, sent_raw = _make_session()
        queue.put_nowait(_data_frame(
            factory,
            encode_frame((1000).to_bytes(2, 'big'), opcode=WSOpcode.CLOSE)))
        await asyncio.wait_for(session.close(), 2)
        # The outgoing CLOSE rode a DATA frame with END_STREAM.
        assert any(
            f.FrameType() == FrameTypes.DATA and f.end_stream
            for f in sent_raw)
        task = session._recipient._reader_task
        assert task is None or task.done()

    @pytest.mark.asyncio
    async def test_close_times_out_on_silent_peer(self):
        session, queue, factory, writer, _ = _make_session()
        await asyncio.wait_for(session.close(drain_timeout=0.1), 2)
        task = session._recipient._reader_task
        assert task is None or task.done()

    @pytest.mark.asyncio
    async def test_close_is_idempotent(self):
        session, queue, factory, writer, sent_raw = _make_session()
        await asyncio.wait_for(session.close(drain_timeout=0.1), 2)
        n = len(sent_raw)
        await asyncio.wait_for(session.close(drain_timeout=0.1), 2)
        assert len(sent_raw) == n

    @pytest.mark.asyncio
    async def test_close_skips_drain_after_disconnect_seen(self):
        """If receive() already surfaced the peer CLOSE, close() must not
        sit in the drain loop waiting for a second one."""
        session, queue, factory, writer, _ = _make_session()
        queue.put_nowait(_data_frame(
            factory,
            encode_frame((1000).to_bytes(2, 'big'), opcode=WSOpcode.CLOSE)))
        opcode, _payload = await session.receive(timeout=2.0)
        assert opcode == WSOpcode.CLOSE
        # Generous outer bound, tiny inner drain budget would be wrong here:
        # the drain must be skipped entirely, so even a long drain_timeout
        # returns immediately.
        await asyncio.wait_for(session.close(drain_timeout=30.0), 2)
