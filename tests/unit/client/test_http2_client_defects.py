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


# ===========================================================================
# bounds 5 — the frame length is peer-declared, and it was never checked
# ===========================================================================

class TestFrameSizeCheck:
    """``_receive_frame`` parsed a 3-byte length and handed it to
    ``readexactly`` unexamined — up to 16 MiB of the peer's choosing, per
    frame, allocated before anything looked at the frame at all.

    There is nothing here to announce.  RFC 9113 §6.5.2 makes 2^14 the
    *initial* SETTINGS_MAX_FRAME_SIZE, in force from connection start, and the
    client advertises no MAX_FRAME_SIZE of its own — so an explicit setting
    could only loosen the bound.  What was missing is the receive-side check.

    The refusal is a **connection** error even for a frame on a non-zero
    stream, which §4.2 would otherwise permit to be a stream error.  We refuse
    *before* reading the payload, so those octets stay in the socket and the
    next 9-byte read would land mid-payload: a stream error here desyncs the
    reader for every stream.  The server refuses the same way for the same
    reason.  Draining the payload first to keep the stream option open would
    turn a refusal into unbounded work the peer chooses.
    """

    _LIMIT = 16384

    @staticmethod
    def _capture(c: HTTP2Client) -> list:
        sent: list = []

        async def _send(frame):
            sent.append(frame)

        # Without this ``_send_raw_frame``'s assert fires, ``_fail_connection``
        # logs it and swallows it, and a GOAWAY assertion passes vacuously.
        c._control_sender = _send
        return sent

    @staticmethod
    def _header(length: int, *, stream_id: int = 1,
                type_: bytes = FrameTypes.DATA) -> bytes:
        """A frame header declaring *length* payload octets."""
        return (length.to_bytes(3, 'big') + type_ + bytes([0])
                + stream_id.to_bytes(4, 'big'))

    def _oversize(self, declared: int, *, trailing: int = 64,
                  stream_id: int = 1):
        """A client whose peer declared *declared* octets and sent *trailing*.

        The gap is the point: a check that reads first would block on the
        octets the peer never sent.
        """
        reader = _BlockingReader(
            self._header(declared, stream_id=stream_id) + b'x' * trailing)
        c = _client(reader=reader)
        return c, reader, self._capture(c)

    async def test_an_oversize_frame_is_refused_with_frame_size_error(self):
        """RFC 9113 §4.2 — FRAME_SIZE_ERROR, told to the peer."""
        c, _reader, sent = self._oversize(self._LIMIT + 1)

        await asyncio.wait_for(c._receive_loop(), timeout=1.0)

        goaway = [f for f in sent if f.FrameType() == FrameTypes.GOAWAY]
        assert goaway, 'an over-sized frame was refused without a GOAWAY'
        assert goaway[0].error_code == ErrorCodes.FRAME_SIZE_ERROR

    async def test_the_refusal_is_not_a_silent_close(self):
        """``None`` from ``_receive_frame`` means EOF to the receive loop, so
        a refusal spelled that way is indistinguishable from the peer hanging
        up — the caller cannot tell a rejection from a disconnect."""
        c, _reader, _sent = self._oversize(self._LIMIT + 1)

        await asyncio.wait_for(c._receive_loop(), timeout=1.0)

        assert c._connection_lost, 'the connection was left usable'
        assert c._failure, (
            'the refusal recorded no reason — a caller cannot tell it from '
            'the peer simply going away')
        assert str(self._LIMIT + 1) in c._failure

    async def test_the_payload_is_never_read(self):
        """The whole memory argument: refusing *before* the read is what keeps
        a peer-declared length from sizing an allocation."""
        c, reader, _sent = self._oversize(self._LIMIT + 1, trailing=64)

        await asyncio.wait_for(c._receive_loop(), timeout=1.0)

        assert len(reader._buf) == 64, (
            'the declared payload was consumed before it was refused — the '
            'peer still chose the read size')

    async def test_a_frame_at_the_limit_is_accepted(self):
        """The boundary, and the reason the default refuses nothing legal: a
        conforming peer may send exactly ``SETTINGS_MAX_FRAME_SIZE``."""
        payload = b'x' * self._LIMIT
        reader = _BlockingReader(self._header(self._LIMIT) + payload)
        c = _client(reader=reader)
        self._capture(c)

        frame = await asyncio.wait_for(c._receive_frame(), timeout=1.0)

        assert frame is not None and frame.payload == payload

    async def test_zero_disables_the_bound(self, monkeypatch):
        """The opt-out every bound owes a fault-injection scenario."""
        monkeypatch.setenv('BB_CLIENT_H2_MAX_FRAME_SIZE', '0')
        payload = b'x' * (self._LIMIT + 1)
        reader = _BlockingReader(self._header(len(payload)) + payload)
        c = _client(reader=reader)
        sent = self._capture(c)

        frame = await asyncio.wait_for(c._receive_frame(), timeout=1.0)

        assert frame is not None and frame.payload == payload
        assert not sent, 'a disabled bound still refused the frame'

    async def test_the_escape_hatch_inherits_the_check(self):
        """``receive_raw_frame`` reads the same wire through the same method,
        so one rule covers both paths — otherwise the bound is escapable by
        calling the public escape hatch instead of running the loop."""
        from blackbull.client.http2 import _ConnectionFailed

        c, reader, sent = self._oversize(self._LIMIT + 1)

        with pytest.raises(_ConnectionFailed):
            await asyncio.wait_for(c.receive_raw_frame(), timeout=1.0)

        assert len(reader._buf) == 64
        assert [f for f in sent if f.FrameType() == FrameTypes.GOAWAY]

    async def test_the_refusal_is_logged_as_a_cap_hit(self, caplog):
        """A bound that fires invisibly cannot be sized by an operator."""
        import logging

        caplog.set_level(logging.WARNING, logger='blackbull.caps')
        c, _reader, _sent = self._oversize(self._LIMIT + 99)

        await asyncio.wait_for(c._receive_loop(), timeout=1.0)

        hits = [r for r in caplog.records
                if getattr(r, 'cap', None) == 'client_h2_max_frame_size']
        assert hits, 'the refusal emitted no cap-hit record'
        assert hits[0].limit == self._LIMIT
        assert hits[0].requested == self._LIMIT + 99
        assert hits[0].protocol == 'http2'

    async def test_an_ordinary_frame_is_untouched(self, caplog):
        """The control — a bound that refuses conformant traffic is a defect,
        not a defence."""
        import logging

        caplog.set_level(logging.WARNING, logger='blackbull.caps')
        payload = b'hello'
        reader = _BlockingReader(self._header(len(payload)) + payload)
        c = _client(reader=reader)
        sent = self._capture(c)

        frame = await asyncio.wait_for(c._receive_frame(), timeout=1.0)

        assert frame is not None and frame.payload == payload
        assert not sent
        assert not [r for r in caplog.records if getattr(r, 'cap', None)]


# ===========================================================================
# BLA-317 — a parse failure was reported as the peer's HPACK fault, whatever
# it actually was
# ===========================================================================

class TestParseFailureNamesItsOwnReason:
    """``_load`` wrapped ``FrameFactory.load`` in one handler that answered
    COMPRESSION_ERROR for every failure.

    §5.4.1 and §4.3 make COMPRESSION_ERROR mean *the HPACK decoder state is
    unusable* — a claim about the connection, which a peer may answer by
    discarding a pool.  It is false for a RST_STREAM of the wrong length.

    The RFC assigns both halves of a refusal, and each frame's own section
    assigns them: §6.4 (RST_STREAM length), §6.7 (PING length), §6.1 (DATA
    padding), §6.6 (PUSH_PROMISE padding), §4.3 (a block that will not
    decode).  Every one of those five says *connection* error, so nothing
    here narrows to a stream error — §4.2's stream/connection split governs
    a frame that merely exceeds SETTINGS_MAX_FRAME_SIZE, which is
    ``_receive_frame``'s check, not this one.
    """

    @staticmethod
    def _capture(c: HTTP2Client) -> list:
        sent: list = []

        async def _send(frame):
            sent.append(frame)

        # ``_send_raw_frame`` asserts on this; ``_fail_connection`` swallows
        # the AssertionError, so without it a GOAWAY assertion passes vacuously.
        c._control_sender = _send
        return sent

    @staticmethod
    def _wire(type_: bytes, flags: int, stream_id: int, payload: bytes) -> bytes:
        return (len(payload).to_bytes(3, 'big') + type_ + bytes([flags])
                + stream_id.to_bytes(4, 'big') + payload)

    async def _drive(self, wire: bytes):
        """Run the receive loop over *wire*; return the client and what it sent."""
        c = _client(reader=_BlockingReader(wire))
        sent = self._capture(c)
        await asyncio.wait_for(c._receive_loop(), timeout=1.0)
        return c, sent

    @staticmethod
    def _goaway(sent: list):
        return [f for f in sent if f.FrameType() == FrameTypes.GOAWAY]

    # -- the code each section names ------------------------------------

    async def test_a_short_rst_stream_is_a_frame_size_error(self):
        """RFC 9113 §6.4 — a RST_STREAM of any length but 4 octets."""
        c, sent = await self._drive(
            self._wire(FrameTypes.RST_STREAM, 0, 1, b'\x00\x00\x00'))

        goaway = self._goaway(sent)
        assert goaway, 'a malformed RST_STREAM was refused without a GOAWAY'
        assert goaway[-1].error_code == ErrorCodes.FRAME_SIZE_ERROR
        assert 'field block' not in (c._failure or ''), (
            'the refusal blamed the field block for a frame-length breach')

    async def test_a_short_ping_is_a_frame_size_error(self):
        """RFC 9113 §6.7 — a PING of any length but 8 octets."""
        _c, sent = await self._drive(
            self._wire(FrameTypes.PING, 0, 0, b'\x00' * 7))

        goaway = self._goaway(sent)
        assert goaway, 'a malformed PING was refused without a GOAWAY'
        assert goaway[-1].error_code == ErrorCodes.FRAME_SIZE_ERROR

    async def test_data_padding_over_the_frame_is_a_protocol_error(self):
        """RFC 9113 §6.1 — padding at least as long as the payload."""
        _c, sent = await self._drive(
            self._wire(FrameTypes.DATA, 0x8, 1, bytes([255, 1, 2])))

        goaway = self._goaway(sent)
        assert goaway, 'over-long DATA padding was refused without a GOAWAY'
        assert goaway[-1].error_code == ErrorCodes.PROTOCOL_ERROR

    async def test_push_promise_padding_over_the_frame_is_a_protocol_error(self):
        """RFC 9113 §6.6 borrows §6.2's padding rule.  The block never
        decoded, so the connection ends either way — but it ends for the
        reason it actually failed for."""
        _c, sent = await self._drive(
            self._wire(FrameTypes.PUSH_PROMISE, 0x8 | 0x4, 1,
                       bytes([255, 0, 0, 0, 2, 0])))

        goaway = self._goaway(sent)
        assert goaway, 'over-long PUSH_PROMISE padding was refused without a GOAWAY'
        assert goaway[-1].error_code == ErrorCodes.PROTOCOL_ERROR

    async def test_a_continued_block_reports_its_own_reason_too(self):
        """The reassembly site decodes the block the reader could not, so it
        is the second place a parse can fail — and it answered
        COMPRESSION_ERROR for a padding breach as well."""
        _c, sent = await self._drive(
            self._wire(FrameTypes.PUSH_PROMISE, 0x8, 1,
                       bytes([255, 0, 0, 0, 2, 0]))
            + self._wire(FrameTypes.CONTINUATION, 0x4, 1, b'\x00'))

        goaway = self._goaway(sent)
        assert goaway, 'a padding breach in a split block sent no GOAWAY'
        assert goaway[-1].error_code == ErrorCodes.PROTOCOL_ERROR

    async def test_an_undecodable_field_block_is_still_a_compression_error(self):
        """The one case the blanket handler had right, and the reason it is
        not enough to map the exceptions one at a time: §4.3 — hpack may have
        applied part of the block to the connection-wide table before raising."""
        _c, sent = await self._drive(
            self._wire(FrameTypes.HEADERS, 0x1 | 0x4, 1, b'\xff\xff\xff\xff\xff'))

        goaway = self._goaway(sent)
        assert goaway, 'an undecodable block was refused without a GOAWAY'
        assert goaway[-1].error_code == ErrorCodes.COMPRESSION_ERROR

    async def test_a_bug_on_our_side_is_not_reported_as_the_peers(self):
        """INTERNAL_ERROR (§7).  A TypeError in our own parser is not evidence
        about the peer's encoder, and telling the peer its HPACK state is
        unusable invites it to throw away a connection pool over our defect."""
        c = _client(reader=_BlockingReader(
            self._wire(FrameTypes.DATA, 0, 1, b'hello')))
        sent = self._capture(c)

        def _boom(_data):
            raise TypeError("'NoneType' object is not subscriptable")

        c._factory.load = _boom            # type: ignore[method-assign]

        await asyncio.wait_for(c._receive_loop(), timeout=1.0)

        goaway = self._goaway(sent)
        assert goaway, 'a parser bug ended the connection without a GOAWAY'
        assert goaway[-1].error_code == ErrorCodes.INTERNAL_ERROR

    # -- the blast radius each section assigns ---------------------------

    async def test_a_malformed_rst_stream_is_not_answered_with_a_rst_stream(self):
        """§6.4 makes this a *connection* error, and §5.4.2 forbids answering
        a RST_STREAM with a RST_STREAM.  The wire is synchronised here — the
        payload was read in full — so a stream error would be available; the
        RFC simply does not assign one."""
        c, sent = await self._drive(
            self._wire(FrameTypes.RST_STREAM, 0, 1, b'\x00\x00\x00'))

        assert not [f for f in sent if f.FrameType() == FrameTypes.RST_STREAM], (
            'a malformed RST_STREAM was answered with a RST_STREAM')
        assert c._connection_lost, 'the connection was left usable'

    async def test_every_named_refusal_still_ends_the_connection(self):
        """Nothing narrows.  §6.1, §6.4, §6.7 and §4.3 all say connection
        error, so the only thing this changes is which reason is given."""
        cases = {
            'RST_STREAM': self._wire(FrameTypes.RST_STREAM, 0, 1, b'\x00' * 3),
            'PING': self._wire(FrameTypes.PING, 0, 0, b'\x00' * 7),
            'DATA': self._wire(FrameTypes.DATA, 0x8, 1, bytes([255, 1, 2])),
            'HEADERS': self._wire(FrameTypes.HEADERS, 0x5, 1, b'\xff' * 5),
        }
        for name, wire in cases.items():
            c, sent = await self._drive(wire)
            assert c._connection_lost, f'{name}: the connection survived'
            assert c._failure, f'{name}: the refusal recorded no reason'
            assert self._goaway(sent), f'{name}: no GOAWAY told the peer why'

    async def test_a_well_formed_frame_of_each_kind_is_untouched(self):
        """The control — a check that refuses conformant traffic is a defect."""
        c = _client(reader=_BlockingReader(
            self._wire(FrameTypes.RST_STREAM, 0, 1, b'\x00' * 4)
            + self._wire(FrameTypes.PING, 0, 0, b'\x00' * 8)))
        sent = self._capture(c)

        first = await asyncio.wait_for(c._receive_frame(), timeout=1.0)
        second = await asyncio.wait_for(c._receive_frame(), timeout=1.0)

        assert first is not None and first.FrameType() == FrameTypes.RST_STREAM
        assert second is not None and second.FrameType() == FrameTypes.PING
        assert not sent, 'a conformant frame was refused'
