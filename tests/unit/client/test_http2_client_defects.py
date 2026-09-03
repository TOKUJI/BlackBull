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
import struct

import pytest

from blackbull.client.exceptions import ConnectionError as ClientConnectionError
from blackbull.client.exceptions import HandshakeError
from blackbull.client.http2 import HTTP2Client
from blackbull.client.websocket_h2 import WebSocketH2Client, WebSocketH2Session
from blackbull.protocol.frame import FrameFactory
from blackbull.protocol.frame_types import ErrorCodes, FrameTypes
from blackbull.server.recipient import AbstractReader
from blackbull.server.sender import AbstractWriter
from blackbull.server.ws_codec import WSOpcode

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


# ===========================================================================
# C-M6 — a raw stream must end when the connection does
# ===========================================================================

class TestRawStreamTeardown:
    """Teardown failed every pending response and left ``_raw_streams``
    untouched, so a WebSocket-over-H2 consumer parked on its queue outlived
    the connection feeding it — woken only by its own caller's deadline, if
    it had one, and never by the disconnect itself.
    """

    @staticmethod
    def _session(client: HTTP2Client, stream_id: int = 1) -> WebSocketH2Session:
        queue = client.register_raw_stream(stream_id)
        return WebSocketH2Session(client, FrameFactory(), stream_id, queue)

    async def test_a_parked_consumer_is_woken_when_the_loop_ends(self):
        """The peer vanishes; the session must surface abnormal closure."""
        reader = _BlockingReader()
        c = _client(reader=reader)
        session = self._session(c)

        loop_task = asyncio.ensure_future(c._receive_loop())
        reader.close()
        await loop_task

        # The bound is the assertion: unsignalled, this waits out the
        # caller's deadline instead of ending on the disconnect.
        opcode, payload = await session.receive(timeout=1.0)
        assert (opcode, payload) == (WSOpcode.CLOSE, struct.pack('>H', 1006))

    async def test_a_parked_consumer_is_woken_by_deliberate_teardown(self):
        """``__aexit__`` cancels a running loop, whose ``finally`` signals;
        the second signal then lands on an empty registry and must not
        disturb the one disconnect the session has already been given."""
        reader = _BlockingReader()
        c = _client(reader=reader)
        session = self._session(c)
        c._receive_task = asyncio.ensure_future(c._receive_loop())
        await asyncio.sleep(0)          # let the loop reach the reader

        await c.__aexit__(None, None, None)

        opcode, payload = await session.receive(timeout=1.0)
        assert (opcode, payload) == (WSOpcode.CLOSE, struct.pack('>H', 1006))

    async def test_a_consumer_is_woken_when_no_loop_ever_ran(self):
        """A client whose receive loop never started — adopted, or torn down
        before its first turn — has no ``finally`` to run, so ``__aexit__``
        is the only thing left that can end the stream."""
        c = _client()
        session = self._session(c)

        await c.__aexit__(None, None, None)

        opcode, payload = await session.receive(timeout=1.0)
        assert (opcode, payload) == (WSOpcode.CLOSE, struct.pack('>H', 1006))

    async def test_the_handshake_stops_waiting_when_the_connection_ends(self):
        """The other queue consumer: Extended CONNECT awaiting its response.

        ``response_timeout`` is thirty times the test's own bound, so this
        only passes if the handshake ended because the connection did.
        """
        reader = _BlockingReader()
        c = _client(reader=reader)
        ws_client = WebSocketH2Client('localhost', 1)
        ws_client._client = c

        task = asyncio.ensure_future(
            ws_client.connect('/ws', response_timeout=30.0))
        await asyncio.sleep(0)
        assert 1 in c._raw_streams, 'the handshake never registered its stream'

        loop_task = asyncio.ensure_future(c._receive_loop())
        reader.close()
        await loop_task

        with pytest.raises(HandshakeError, match='expected HEADERS'):
            await asyncio.wait_for(task, timeout=1.0)

    async def test_the_registry_is_emptied_when_the_loop_ends(self):
        """The same leak ``_senders`` had: nothing dropped the entry."""
        reader = _BlockingReader()
        c = _client(reader=reader)
        c.register_raw_stream(1)

        loop_task = asyncio.ensure_future(c._receive_loop())
        reader.close()
        await loop_task

        assert c._raw_streams == {}

    async def test_the_registry_is_emptied_by_deliberate_teardown(self):
        c = _client()
        c.register_raw_stream(1)

        await c.__aexit__(None, None, None)

        assert c._raw_streams == {}

    async def test_registering_after_the_connection_is_lost_is_refused(self):
        """``request()`` refuses here; registration did not, and its consumer
        then parked on a queue no receive loop was left to fill."""
        reader = _BlockingReader()
        c = _client(reader=reader)

        loop_task = asyncio.ensure_future(c._receive_loop())
        reader.close()
        await loop_task

        with pytest.raises(ClientConnectionError):
            c.register_raw_stream(1)

    async def test_a_stream_that_already_left_is_not_touched(self):
        """The control.  ``unregister_raw_stream`` is how a session departs
        normally; teardown must neither signal nor resurrect it."""
        reader = _BlockingReader()
        c = _client(reader=reader)
        queue = c.register_raw_stream(1)
        c.unregister_raw_stream(1)

        loop_task = asyncio.ensure_future(c._receive_loop())
        reader.close()
        await loop_task
        await c.__aexit__(None, None, None)

        assert queue.empty(), 'a stream that had already left was signalled'
        assert c._raw_streams == {}


# ===========================================================================
# bounds 6 — the raw-stream queue is unbounded, and flow control does not
#            bound it: RFC 9113 §6.9.1 charges the payload only, so a
#            zero-length DATA frame buys queue depth for free
# ===========================================================================

class TestRawStreamQueueDepth:
    """``register_raw_stream`` handed out an unbounded queue, and every frame
    on that stream but WINDOW_UPDATE and SETTINGS lands in it.  A peer that
    sends faster than the registrant drains — or one that sends frames costing
    it no flow-control credit at all — grows that queue with nothing to stop
    it, and the bound that stops it must refuse the one stream rather than the
    connection every other stream is riding on.
    """

    @staticmethod
    def _capture(c: HTTP2Client) -> list:
        """Collect the frames the client sends to the peer."""
        sent: list = []

        async def _send(frame):
            sent.append(frame)

        c._control_sender = _send
        return sent

    @staticmethod
    def _data(c: HTTP2Client, stream_id: int, payload: bytes = b'') -> bytes:
        return c._factory.create(
            FrameTypes.DATA, 0, stream_id, data=payload).save()

    @staticmethod
    async def _wait_until(pred, timeout: float = 1.0) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while not pred() and loop.time() < deadline:
            await asyncio.sleep(0.001)

    @staticmethod
    async def _end(c: HTTP2Client, task) -> None:
        c._reader.close()
        await task

    def _flood(self, monkeypatch, depth: int, frames: int,
               payload: bytes = b''):
        """Two raw streams; only the first is flooded.  Returns the client,
        the receive-loop task, the frames sent to the peer and both queues.
        """
        monkeypatch.setenv('BB_CLIENT_RAW_QUEUE_DEPTH', str(depth))
        c = _client(reader=_BlockingReader())
        sent = self._capture(c)
        flooded = c.register_raw_stream(1)
        quiet = c.register_raw_stream(3)
        c._reader.feed(self._data(c, 3, b'ok')
                       + b''.join(self._data(c, 1, payload)
                                  for _ in range(frames)))
        return c, asyncio.ensure_future(c._receive_loop()), sent, flooded, quiet

    async def test_the_flooded_stream_is_refused_and_its_consumer_woken(
            self, monkeypatch):
        """The queue fills, so the stream goes — and its consumer is told,
        rather than left parked on a queue nothing will fill again.  That the
        *connection* survives is the next test's claim."""
        c, task, _sent, flooded, _quiet = self._flood(monkeypatch, 4, 8)
        session = WebSocketH2Session(c, FrameFactory(), 1, flooded)
        await self._wait_until(lambda: 1 not in c._raw_streams)

        assert 1 not in c._raw_streams, 'the flooded stream was not refused'
        opcode, close = await session.receive(timeout=1.0)
        assert (opcode, close) == (WSOpcode.CLOSE, struct.pack('>H', 1006))
        await self._end(c, task)

    async def test_the_peer_is_told_to_slow_down(self, monkeypatch):
        """RFC 9113 §7 ENHANCE_YOUR_CALM — the peer is generating load faster
        than this end consumes it, which is what that code is for and what the
        header-aggregate breach already sends."""
        c, task, sent, _flooded, _quiet = self._flood(monkeypatch, 4, 8)
        await self._wait_until(lambda: 1 not in c._raw_streams)

        resets = [f for f in sent if f.FrameType() == FrameTypes.RST_STREAM]
        assert [(f.stream_id, f.error_code) for f in resets] == [
            (1, ErrorCodes.ENHANCE_YOUR_CALM)]
        await self._end(c, task)

    async def test_the_other_stream_on_that_connection_is_untouched(
            self, monkeypatch):
        """The point of the bound: one stream refused, never the connection.

        The receive loop must still be running, the well-behaved stream still
        registered and unsignalled, and still fed by the loop afterwards.
        """
        c, task, _sent, _flooded, quiet = self._flood(monkeypatch, 4, 8)
        await self._wait_until(lambda: 1 not in c._raw_streams)

        assert 1 not in c._raw_streams, 'the flood was never refused'
        assert not task.done(), 'one stream\'s bound ended the receive loop'
        assert 3 in c._raw_streams, 'the well-behaved stream was dropped too'
        assert quiet.qsize() == 1, 'the well-behaved stream was signalled too'
        assert quiet.get_nowait().FrameType() == FrameTypes.DATA

        c._reader.feed(self._data(c, 3, b'more'))
        await self._wait_until(lambda: quiet.qsize() == 1)
        assert quiet.qsize() == 1, 'the well-behaved stream stopped receiving'
        await self._end(c, task)

    async def test_teardown_signals_every_stream_past_a_full_one(
            self, monkeypatch):
        """The C-M6 regression guard.

        ``_end_raw_streams`` wakes a parked consumer with a synthetic
        RST_STREAM, and it runs in the receive loop's ``finally``.  A bound
        makes that ``put_nowait`` raise on a full queue, which leaves every
        later stream unsignalled, the registry uncleared, and the exception
        escaping the loop.
        """
        monkeypatch.setenv('BB_CLIENT_RAW_QUEUE_DEPTH', '2')
        reader = _BlockingReader()
        c = _client(reader=reader)
        full = c.register_raw_stream(1)
        later = c.register_raw_stream(3)
        for _ in range(2):
            full.put_nowait(c._factory.create(FrameTypes.DATA, 0, 1, data=b''))
        assert full.full()

        loop_task = asyncio.ensure_future(c._receive_loop())
        reader.close()
        await loop_task

        assert c._raw_streams == {}, 'the registry leaked past a full queue'
        assert full.get_nowait().FrameType() == FrameTypes.RST_STREAM
        assert later.get_nowait().FrameType() == FrameTypes.RST_STREAM

    async def test_a_clean_teardown_keeps_what_already_arrived(
            self, monkeypatch):
        """Displacement is for a full queue only.  A consumer merely behind
        must still be able to drain the frames the peer really sent."""
        monkeypatch.setenv('BB_CLIENT_RAW_QUEUE_DEPTH', '4')
        c = _client()
        queue = c.register_raw_stream(1)
        for payload in (b'a', b'b'):
            queue.put_nowait(
                c._factory.create(FrameTypes.DATA, 0, 1, data=payload))

        c._end_raw_streams()

        assert [queue.get_nowait().payload for _ in range(2)] == [b'a', b'b']
        assert queue.get_nowait().FrameType() == FrameTypes.RST_STREAM

    async def test_displaced_payload_still_returns_connection_credit(
            self, monkeypatch):
        """RFC 9113 §6.9 — the connection window is shared by every stream.

        A raw stream's DATA is credited when its consumer drains it, so a
        refusal that discards the backlog and keeps its credit shrinks the
        window for every other stream: refusing one stream by stalling the
        rest.  Same rule ``_on_response_data`` follows for a stream it no
        longer tracks.
        """
        from blackbull.client.http2 import _WINDOW_UPDATE_THRESHOLD

        c, task, sent, _flooded, _quiet = self._flood(
            monkeypatch, 4, 5, b'x' * (_WINDOW_UPDATE_THRESHOLD // 4 + 1))
        await self._wait_until(lambda: 1 not in c._raw_streams)

        credit = [f for f in sent
                  if f.FrameType() == FrameTypes.WINDOW_UPDATE
                  and f.stream_id == 0]
        assert credit, 'the displaced payload kept the connection window'
        await self._end(c, task)

    async def test_zero_disables_the_bound(self, monkeypatch):
        """The escape hatch, and the behaviour every existing raw-stream
        consumer had before the bound."""
        c, task, sent, flooded, _quiet = self._flood(monkeypatch, 0, 200)
        await self._wait_until(lambda: flooded.qsize() == 200)

        assert flooded.qsize() == 200
        assert 1 in c._raw_streams
        assert not [f for f in sent if f.FrameType() == FrameTypes.RST_STREAM]
        await self._end(c, task)

    async def test_traffic_under_the_bound_is_untouched(self, monkeypatch):
        """The control — a bound that refuses conformant traffic is a defect,
        not a defence."""
        c, task, sent, flooded, _quiet = self._flood(monkeypatch, 8, 6)
        await self._wait_until(lambda: flooded.qsize() == 6)

        assert flooded.qsize() == 6
        assert 1 in c._raw_streams
        assert not [f for f in sent if f.FrameType() == FrameTypes.RST_STREAM]
        await self._end(c, task)
