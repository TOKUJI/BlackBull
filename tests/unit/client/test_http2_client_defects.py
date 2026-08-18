"""HTTP/2 client defects — the connecting role, where the peer is the server.

The attack-surface map covers BlackBull as the *listening* party only
(§9).  These are the defects a code review found in the other direction,
each verified present on master before the fix.  They are not a coverage
audit — that is `proposals/client-surface-audit.md`.

What ties them together is that every one of them turns a misbehaving or
merely-departed server into a client-side failure worse than the
disconnect itself: streams failed that the server said it had processed,
a connection window that never reopens, a coroutine that never returns.
"""
from __future__ import annotations

import asyncio

import pytest

from blackbull.client.exceptions import ConnectionError as ClientConnectionError
from blackbull.client.http2 import HTTP2Client
from blackbull.protocol.frame_types import ErrorCodes, FrameTypes
from blackbull.server.recipient import AbstractReader
from blackbull.server.sender import AbstractWriter

pytestmark = pytest.mark.asyncio


class _RecordingWriter(AbstractWriter):
    def __init__(self) -> None:
        self.frames: list = []

    async def write(self, data: bytes) -> None:
        self.frames.append(data)


class _BlockingReader(AbstractReader):
    """Feeds queued bytes, then blocks — a live peer that has gone quiet."""

    def __init__(self, data: bytes = b''):
        self._buf = bytearray(data)
        self._arrived = asyncio.Event()
        self.eof = False
        if data:
            self._arrived.set()

    def feed(self, data: bytes) -> None:
        self._buf += data
        self._arrived.set()

    def close(self) -> None:
        self.eof = True
        self._arrived.set()

    async def read(self, n: int) -> bytes:
        await self._wait_for(1)
        chunk = bytes(self._buf[:n])
        del self._buf[:n]
        return chunk

    async def _wait_for(self, n: int) -> None:
        while len(self._buf) < n:
            if self.eof:
                raise asyncio.IncompleteReadError(bytes(self._buf), n)
            self._arrived.clear()
            await self._arrived.wait()

    async def readexactly(self, n: int) -> bytes:
        await self._wait_for(n)
        chunk = bytes(self._buf[:n])
        del self._buf[:n]
        return chunk


def _client(writer=None, reader=None) -> HTTP2Client:
    c = HTTP2Client('localhost', 1)
    c._writer = writer or _RecordingWriter()
    if reader is not None:
        c._reader = reader
    return c


def _frame_types_written(writer: _RecordingWriter) -> list[int]:
    return [buf[3] for buf in writer.frames if len(buf) >= 4]


# ===========================================================================
# B-2 — GOAWAY's last_stream_id is not the frame's stream_id
# ===========================================================================

class TestGoawayLastStreamId:
    async def test_a_processed_stream_is_not_failed_by_goaway(self):
        """RFC 9113 §6.8 — GOAWAY's own stream identifier MUST be 0.

        ``last_stream_id`` is the first four *payload* bytes, and it is the
        peer's promise about what it did process.  Reading the header field
        instead makes every stream look unprocessed, so a graceful shutdown
        fails responses the server had already handled — the exact opposite
        of what GOAWAY exists to communicate.
        """
        from blackbull.client.http2 import _PendingResponse

        c = _client()
        loop = asyncio.get_running_loop()
        processed = loop.create_future()
        unprocessed = loop.create_future()
        c._responses = {1: _PendingResponse(future=processed),
                        5: _PendingResponse(future=unprocessed)}

        goaway = c._factory.create(
            FrameTypes.GOAWAY, 0, 0,
            data=(3).to_bytes(4, 'big') + int(ErrorCodes.NO_ERROR).to_bytes(4, 'big'))
        c._on_goaway(goaway)

        assert not processed.done(), (
            'stream 1 was failed by a GOAWAY that said streams up to 3 were '
            'processed')
        assert unprocessed.done(), 'stream 5 is past last_stream_id and must fail'
        with pytest.raises(ClientConnectionError):
            unprocessed.result()

    async def test_the_field_read_is_the_payload_one(self):
        """A GOAWAY frame's header stream_id is 0; only the payload carries it."""
        c = _client()
        goaway = c._factory.create(
            FrameTypes.GOAWAY, 0, 0,
            data=(7).to_bytes(4, 'big') + int(ErrorCodes.NO_ERROR).to_bytes(4, 'big'))
        assert goaway.stream_id == 0
        assert goaway.last_stream_id == 7


# ===========================================================================
# C-M1 — DATA for an unknown stream must still return connection credit
# ===========================================================================

class TestUnknownStreamCredit:
    async def test_connection_credit_is_returned_for_an_unknown_stream(self):
        """RFC 9113 §6.9 — the connection window is shared by every stream.

        Dropping the frame without crediting leaks the connection window,
        and once it reaches zero *every* stream's body stalls in the
        server's ``_write_data``.  A stream that closed while its DATA was
        in flight is normal, not hostile — this is reachable without a bad
        peer.
        """
        from blackbull.client.http2 import _WINDOW_UPDATE_THRESHOLD

        writer = _RecordingWriter()
        c = _client(writer)
        c._control_sender = c._send_raw_frame_direct if hasattr(
            c, '_send_raw_frame_direct') else None

        sent: list = []

        async def _capture(frame):
            sent.append(frame)

        c._control_sender = _capture

        payload = b'x' * (_WINDOW_UPDATE_THRESHOLD + 1)
        data = c._factory.create(FrameTypes.DATA, 0, 99, data=payload)
        await c._on_response_data(data)

        updates = [f for f in sent if f.FrameType() == FrameTypes.WINDOW_UPDATE]
        assert updates, (
            'DATA for an unknown stream returned no flow-control credit — '
            'the connection window leaks by every such frame')
        assert all(f.stream_id == 0 for f in updates), (
            'credit for a stream that does not exist must be connection-level')


# ===========================================================================
# B-7 — a departed peer must not leave request() waiting forever
# ===========================================================================

class TestConnectionLost:
    async def test_request_after_the_peer_disconnects_raises(self):
        """``HTTP1Client`` raises ``ConnectionError`` here; the two disagreed.

        After the receive loop ends at EOF nobody remains to resolve a
        future, so ``request()`` awaited one that could never complete.
        """
        reader = _BlockingReader()
        c = _client(reader=reader)
        c._control_sender = lambda frame: asyncio.sleep(0)

        loop_task = asyncio.ensure_future(c._receive_loop())
        reader.close()
        await loop_task

        # Bounded on purpose: the defect *is* a hang, and an unbounded await
        # would wedge the suite instead of reporting it.
        with pytest.raises(ClientConnectionError):
            await asyncio.wait_for(c.request('GET', '/'), timeout=1.0)

    async def test_pending_requests_are_failed_when_the_loop_ends(self):
        """Existing behaviour pin — the loop already does this."""
        from blackbull.client.http2 import _PendingResponse

        reader = _BlockingReader()
        c = _client(reader=reader)
        fut = asyncio.get_running_loop().create_future()
        c._responses = {1: _PendingResponse(future=fut)}

        loop_task = asyncio.ensure_future(c._receive_loop())
        reader.close()
        await loop_task

        assert fut.done()
        with pytest.raises(ClientConnectionError):
            fut.result()


# ===========================================================================
# C-M4 — a send failure must not leak the pending future
# ===========================================================================

class TestPendingLeak:
    async def test_a_failed_send_removes_its_pending_entry(self):
        c = _client()

        class _Boom(Exception):
            pass

        async def _explode(_frame):
            raise _Boom('write failed')

        c._make_sender = lambda sid: _explode          # type: ignore[assignment]

        with pytest.raises(_Boom):
            await c.request('GET', '/')

        assert c._responses == {}, (
            f'a failed send left {len(c._responses)} pending future(s) behind; '
            f'the dict grows unbounded until GOAWAY or __aexit__')


# ===========================================================================
# C-M7 — a half-delivered frame must not park the connection forever
# ===========================================================================

class TestPartialFrameDeadline:
    async def test_a_frame_header_without_its_payload_times_out(self):
        """The bound is on an *unfinished frame*, not on waiting for one.

        Waiting indefinitely for the next frame is legitimate — server
        streaming and long-polling both do it.  Waiting indefinitely for
        the rest of a frame the peer already began is not: the peer
        committed to those bytes.  This is the client-side twin of the
        server's ``BB_HEADER_TIMEOUT``.
        """
        # A DATA frame header declaring 100 payload bytes, and no payload.
        header = (100).to_bytes(3, 'big') + FrameTypes.DATA + bytes([0]) \
            + (1).to_bytes(4, 'big')
        reader = _BlockingReader(header)
        c = _client(reader=reader)
        c._frame_read_timeout = 0.05

        # The client's own bound must fire, not the test's: an abandoned
        # frame is treated as the peer being gone, which is what ``None``
        # already means to the receive loop.  The outer wait_for is twenty
        # times the client's bound, so it only trips if the client has none.
        frame = await asyncio.wait_for(c._receive_frame(), timeout=1.0)
        assert frame is None, (
            'a frame the peer began and abandoned did not end the read')

    async def test_waiting_for_the_next_frame_is_not_bounded(self):
        """A quiet peer between frames is normal and must not be closed."""
        reader = _BlockingReader()
        c = _client(reader=reader)
        c._frame_read_timeout = 0.05

        # Six times the client's bound: if it wrongly applied that bound to
        # the gap *between* frames, this returns None instead of timing out.
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(c._receive_frame(), timeout=0.3)
