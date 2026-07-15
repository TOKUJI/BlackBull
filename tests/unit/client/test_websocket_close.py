"""Sprint 72 (1.20c) — H1 client close discipline + recipient shutdown.

``WebSocketSession.close()`` previously sent a best-effort CLOSE frame and
returned: the peer's echoed CLOSE was never drained and the recipient's
background read-loop task was left running (RFC 6455 §7.1.2 expects the
closing handshake to complete; a leaked task warns at loop shutdown).
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from blackbull.client.websocket import WebSocketSession
from blackbull.server.recipient import AbstractReader, WebSocketRecipient
from blackbull.server.sender import AbstractWriter
from blackbull.server.ws_codec import WSOpcode, encode_frame


class _ScriptedReader(AbstractReader):
    """Serves a fixed byte script, then blocks forever (silent peer)."""

    def __init__(self, data: bytes = b'') -> None:
        self._buf = bytearray(data)
        self._starved = asyncio.Event()

    async def read(self, n: int) -> bytes:
        if not self._buf:
            await self._starved.wait()  # never set — models a silent peer
        chunk = bytes(self._buf[:n])
        del self._buf[:n]
        return chunk


class _CaptureWriter(AbstractWriter):
    def __init__(self) -> None:
        self.data = bytearray()

    async def write(self, data: bytes) -> None:
        self.data += data


def _session(peer_bytes: bytes = b'') -> WebSocketSession:
    reader = _ScriptedReader(peer_bytes)
    writer = _CaptureWriter()
    raw_writer = MagicMock(spec=asyncio.StreamWriter)
    return WebSocketSession(reader, writer, raw_writer=raw_writer,
                            subprotocol=None)


class TestSessionClose:
    @pytest.mark.asyncio
    async def test_close_drains_peer_close_and_stops_reader(self):
        peer_close = encode_frame((1000).to_bytes(2, 'big'),
                                  opcode=WSOpcode.CLOSE)
        ws = _session(peer_close)
        await asyncio.wait_for(ws.close(), 2)
        task = ws._recipient._reader_task
        assert task is None or task.done()

    @pytest.mark.asyncio
    async def test_close_times_out_on_silent_peer(self):
        ws = _session()
        await asyncio.wait_for(ws.close(drain_timeout=0.1), 2)
        task = ws._recipient._reader_task
        assert task is None or task.done()

    @pytest.mark.asyncio
    async def test_close_skips_drain_after_disconnect_seen(self):
        peer_close = encode_frame((1000).to_bytes(2, 'big'),
                                  opcode=WSOpcode.CLOSE)
        ws = _session(peer_close)
        event = await ws.receive()
        assert event['type'] == 'websocket.disconnect'
        await asyncio.wait_for(ws.close(drain_timeout=30.0), 2)


class TestRecipientShutdown:
    @pytest.mark.asyncio
    async def test_shutdown_safe_before_start(self):
        r = WebSocketRecipient(_ScriptedReader(), _CaptureWriter(),
                               require_masked=False)
        await r.shutdown()
        await r.shutdown()  # idempotent

    @pytest.mark.asyncio
    async def test_shutdown_cancels_running_reader(self):
        r = WebSocketRecipient(_ScriptedReader(), _CaptureWriter(),
                               require_masked=False)
        r._connect_sent = True
        recv = asyncio.create_task(r())
        await asyncio.sleep(0.05)  # let the read loop start and park
        task = r._reader_task
        assert task is not None and not task.done()
        await r.shutdown()
        assert task.done()
        recv.cancel()
        with pytest.raises(asyncio.CancelledError):
            await recv
