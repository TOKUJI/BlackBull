"""HTTP/2 protocol compliance tests (flow control, concurrency, GOAWAY, push, priority).

Each class protects a specific RFC 7540 / RFC 9218 contract.
Tests drive HTTP2Actor end-to-end using in-process fakes so no live sockets
are needed, but the full actor loop executes.
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock
from hpack import Encoder

from blackbull.server.http2_actor import HTTP2Actor
from blackbull.server.recipient import AbstractReader, IncompleteReadError
from blackbull.server.sender import AsyncioWriter
from blackbull.protocol.frame import FrameFactory
from blackbull.protocol.frame_types import (FrameTypes, FrameFlags,
                                            HeaderFrameFlags, DataFrameFlags,
                                            SettingFrameFlags)


# ---------------------------------------------------------------------------
# Wire-format helpers
# ---------------------------------------------------------------------------

def _make_h2_frame(type_byte: FrameTypes, flags: FrameFlags | int,
                   stream_id: int, payload: bytes) -> bytes:
    length = len(payload)
    return (length.to_bytes(3, 'big')
            + type_byte
            + bytes([flags])
            + stream_id.to_bytes(4, 'big')
            + payload)


def _make_headers_frame(stream_id: int = 1, end_stream: bool = False,
                        method: bytes = b'GET', path: bytes = b'/',
                        priority: tuple[int, int] | None = None) -> bytes:
    encoder = Encoder()
    block = encoder.encode([(b':method', method),
                             (b':path', path),
                             (b':scheme', b'https'), (b':authority', b'example.com')])
    flags: FrameFlags = HeaderFrameFlags.END_HEADERS
    if end_stream:
        flags |= HeaderFrameFlags.END_STREAM
    if priority is not None:
        flags |= HeaderFrameFlags.PRIORITY
        dep, weight = priority
        payload = dep.to_bytes(4, 'big') + bytes([weight]) + block
    else:
        payload = block
    return _make_h2_frame(FrameTypes.HEADERS, flags, stream_id, payload)


class _ByteByByteReader(AbstractReader):
    """Read buffer that returns at most 1 byte from read(), simulating TCP fragmentation.

    readexactly(n) correctly accumulates n bytes from the internal buffer.
    A production path that used read(n) instead of readexactly(n) would receive
    only 1 byte and misparse the HTTP/2 frame header or preface remainder.
    """

    def __init__(self, data: bytes):
        self._buf = bytearray(data)

    async def read(self, n: int) -> bytes:
        if not self._buf:
            return b''
        chunk = bytes(self._buf[:1])
        del self._buf[:1]
        return chunk

    async def readuntil(self, sep: bytes) -> bytes:
        result = bytearray()
        while True:
            if not self._buf:
                raise IncompleteReadError()
            result.append(self._buf[0])
            del self._buf[:1]
            if bytes(result).endswith(sep):
                return bytes(result)

    async def readexactly(self, n: int) -> bytes:
        if len(self._buf) < n:
            raise IncompleteReadError()
        chunk = bytes(self._buf[:n])
        del self._buf[:n]
        return chunk


def _make_h2_actor(app=None):
    """Create an HTTP2Actor with a fake writer and mocked send_frame."""
    if app is None:
        app = AsyncMock()
    writer = MagicMock()
    writer.drain = AsyncMock()
    writer.close = MagicMock()
    handler = HTTP2Actor(None, AsyncioWriter(writer), app, aggregator=None)
    handler.send_frame = AsyncMock()
    return handler, app


# ---------------------------------------------------------------------------
# RFC 7540 §6.9 — Flow control
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestHTTP2FlowControl:
    """HTTP2Actor must gate DATA sends behind the peer's flow-control window."""

    def _make_window_update_frame(self, increment: int,
                                  stream_id: int = 0) -> bytes:
        payload = increment.to_bytes(4, 'big')
        return _make_h2_frame(FrameTypes.WINDOW_UPDATE, SettingFrameFlags.INIT,
                              stream_id, payload)

    async def test_window_update_received_is_tracked(self):
        """A connection-level WINDOW_UPDATE must increase the connection window
        on all cached stream senders."""
        h_frame = _make_headers_frame(stream_id=1, end_stream=True)
        wu_frame = self._make_window_update_frame(65535, stream_id=0)
        settings = _make_h2_frame(FrameTypes.SETTINGS, SettingFrameFlags.INIT, 0, b'')

        handler, app = _make_h2_actor()
        handler.receive = AsyncMock(side_effect=[settings, h_frame, wu_frame, None])

        await handler.run()

        # _senders is pruned on stream completion (memory-leak fix); assert the
        # shared connection window (bug 1.2) that seeds new senders instead.
        assert handler._conn_window.size >= 65535, (
            f'Expected handler._conn_window.size >= 65535 after WINDOW_UPDATE, '
            f'got {handler._conn_window.size}'
        )

    @staticmethod
    def _draining_app():
        """An app that reads its request body to completion then responds.

        Consume-based inbound flow control credits WINDOW_UPDATE when the
        app pops each body event — an app that ignores ``receive`` earns no
        credit (its balance is flushed to the connection window only when
        the stream is released), so these crediting tests must actually
        consume.
        """
        async def app(scope, receive, send):
            while True:
                ev = await receive()
                if ev['type'] != 'http.request' or not ev.get('more_body', False):
                    break
            await send({'type': 'http.response.start', 'status': 200,
                        'headers': []})
            await send({'type': 'http.response.body', 'body': b''})
        return app

    @staticmethod
    def _wu_increments(handler) -> list[int]:
        # Stream-level WINDOW_UPDATEs (stream_id > 0) reflect per-stream
        # DATA consumption.  Connection-level (stream_id == 0) is
        # asserted separately via :meth:`_wu_increments_conn` — the
        # server emits one of each per consumed DATA frame so both windows
        # stay credited (RFC 9113 §6.9.1).
        return [call.args[0].window_size
                for call in handler.send_frame.call_args_list
                if hasattr(call.args[0], 'FrameType')
                and call.args[0].FrameType() == FrameTypes.WINDOW_UPDATE
                and call.args[0].stream_id > 0]

    @staticmethod
    def _wu_increments_conn(handler) -> list[int]:
        """Connection-level WINDOW_UPDATE increments emitted by *handler*.

        RFC 9113 §6.9.1 — DATA frames consume both the stream-level and
        connection-level receive windows; the server must credit both
        back as it delivers frames to the application, otherwise the
        connection-level window depletes toward zero across requests
        and any subsequent body stalls once cumulative inbound traffic
        reaches the initial 65,535 bytes.
        """
        return [call.args[0].window_size
                for call in handler.send_frame.call_args_list
                if hasattr(call.args[0], 'FrameType')
                and call.args[0].FrameType() == FrameTypes.WINDOW_UPDATE
                and call.args[0].stream_id == 0]

    async def test_single_data_frame_window_update_increment(self):
        """One DATA frame, once consumed by the app, must produce
        WINDOW_UPDATE with increment equal to that frame's payload size
        (RFC 7540 §6.9; credit is replayed at consume-time)."""
        payload = b'hello'
        stream_id = 1
        h_frame = _make_headers_frame(stream_id=stream_id, end_stream=False)
        d_frame = _make_h2_frame(FrameTypes.DATA, DataFrameFlags.END_STREAM,
                                 stream_id, payload)

        handler, _ = _make_h2_actor(self._draining_app())
        handler.receive = AsyncMock(side_effect=[h_frame, d_frame, None])
        await handler.run()

        increments = self._wu_increments(handler)
        assert increments, 'handler must send WINDOW_UPDATE after consuming DATA'
        assert any(inc == len(payload) for inc in increments), (
            f'WINDOW_UPDATE increment must equal payload size ({len(payload)}); '
            f'got increments={increments}'
        )

    async def test_two_data_frames_window_update_sum(self):
        """Two DATA frames, consumed → WINDOW_UPDATE increments must sum to
        total bytes."""
        chunk1 = b'hello'
        chunk2 = b' world'
        total = len(chunk1) + len(chunk2)
        stream_id = 1
        h_frame  = _make_headers_frame(stream_id=stream_id, end_stream=False)
        d_frame1 = _make_h2_frame(FrameTypes.DATA, SettingFrameFlags.INIT,
                                  stream_id, chunk1)
        d_frame2 = _make_h2_frame(FrameTypes.DATA, DataFrameFlags.END_STREAM,
                                  stream_id, chunk2)

        handler, _ = _make_h2_actor(self._draining_app())
        handler.receive = AsyncMock(side_effect=[h_frame, d_frame1, d_frame2, None])
        await handler.run()

        increments = self._wu_increments(handler)
        assert increments, 'handler must send at least one WINDOW_UPDATE'
        assert sum(increments) == total, (
            f'Sum of WINDOW_UPDATE increments must equal total bytes consumed '
            f'({total}); got {sum(increments)} from increments={increments}'
        )

    # ----- RFC 9113 §6.9.1 connection-level credit (regression lock) ------
    #
    # Before Sprint 39, the server only emitted stream-level WINDOW_UPDATE
    # on inbound DATA.  The connection-level window depleted toward zero
    # across requests, and any single body — or cumulative inbound across
    # a keep-alive H2 connection — past 65,535 bytes stalled waiting for
    # credit that never came.  Surfaced during the WS-over-H2 64 KiB
    # interop test; the bug was broader than RFC 8441.

    async def test_single_data_frame_emits_connection_window_update(self):
        """Per RFC 9113 §6.9.1, a DATA frame consumes both the stream
        and connection windows; the server must credit both back once the
        app consumes the frame."""
        payload = b'hello'
        stream_id = 1
        h_frame = _make_headers_frame(stream_id=stream_id, end_stream=False)
        d_frame = _make_h2_frame(FrameTypes.DATA, DataFrameFlags.END_STREAM,
                                 stream_id, payload)

        handler, _ = _make_h2_actor(self._draining_app())
        handler.receive = AsyncMock(side_effect=[h_frame, d_frame, None])
        await handler.run()

        conn_increments = self._wu_increments_conn(handler)
        assert any(inc == len(payload) for inc in conn_increments), (
            f'connection-level WINDOW_UPDATE with increment {len(payload)} '
            f'must follow each consumed DATA frame; '
            f'got conn-level increments={conn_increments}'
        )

    async def test_cumulative_inbound_exceeds_initial_connection_window(self):
        """A body larger than the 65,535-byte default connection-level
        receive window must be delivered in full — the connection-level
        WINDOW_UPDATEs emitted per DATA must sum to the full body size.

        Without the fix, cumulative inbound past 65,535 would deplete
        the connection-level receive window to zero and any subsequent
        DATA on this connection would stall waiting for a
        ``WINDOW_UPDATE(stream_id=0)`` the server never sent.

        Chunk size stays under RFC 9113 §4.2 ``max_frame_size``
        (default 16,384); 5 chunks at 16,000 bytes = 80,000 cumulative,
        which clears the 65,535 threshold.

        Consume-based crediting note: a conformant peer is paced by the
        credit it receives — it never has more than 65,535 un-credited
        bytes in flight (a burst that ignores the closed window trips the
        ENHANCE_YOUR_CALM abuse backstop instead).  The fake peer below
        models that by yielding to the loop between frames so the
        draining app consumes (and the server credits) as frames arrive.
        """
        chunk_size = 16_000  # under max_frame_size
        n_chunks = 5
        stream_id = 1
        h_frame = _make_headers_frame(stream_id=stream_id, end_stream=False)
        data_frames = []
        for i in range(n_chunks):
            is_last = (i == n_chunks - 1)
            flags = (DataFrameFlags.END_STREAM if is_last
                     else SettingFrameFlags.INIT)
            data_frames.append(_make_h2_frame(
                FrameTypes.DATA, flags, stream_id, bytes([0x60 + i]) * chunk_size))
        total = n_chunks * chunk_size
        assert total > 65_535, 'sanity: total must exceed initial conn window'

        handler, _ = _make_h2_actor(self._draining_app())
        frames = [h_frame, *data_frames]

        async def fake_receive():
            # Yield first so the app drains what is already queued — the
            # credit-paced behaviour of a window-respecting peer.
            await asyncio.sleep(0)
            return frames.pop(0) if frames else None

        handler.receive = fake_receive
        await handler.run()

        conn_increments = self._wu_increments_conn(handler)
        # Cumulative connection-level credit returned must cover every
        # byte the peer sent — otherwise the *next* inbound burst on
        # this connection would stall.
        assert sum(conn_increments) >= total, (
            f'connection-level WINDOW_UPDATEs must cumulatively credit '
            f'at least the {total} bytes received; got '
            f'sum={sum(conn_increments)} from increments={conn_increments}'
        )
        # Same byte count must also be credited stream-level (RFC 9113
        # §6.9.1 — both windows are debited and must both be restored).
        stream_increments = self._wu_increments(handler)
        assert sum(stream_increments) >= total, (
            f'stream-level WINDOW_UPDATEs must also cumulatively credit '
            f'at least {total}; got sum={sum(stream_increments)}'
        )

    async def test_zero_window_blocks_app_data_send(self):
        """When the sender's window is 0, _write() must suspend until window_update()."""
        from blackbull.server.sender import HTTP2Sender, AsyncioWriter

        written = bytearray()
        mock_writer = MagicMock()
        mock_writer.write = MagicMock(side_effect=lambda d: written.extend(d))
        mock_writer.drain = AsyncMock()

        factory = FrameFactory()
        sender = HTTP2Sender(AsyncioWriter(mock_writer), factory, stream_id=1)

        sender.connection_window_size = 0
        sender.stream_window_size = 0

        payload = b'x' * 100

        async def send_task():
            await sender._write_data(payload, end_stream=True)

        task = asyncio.create_task(send_task())
        await asyncio.sleep(0)

        assert payload not in bytes(written), (
            'DATA written while window was 0 — flow control not enforced.'
        )

        # Credit both windows: connection-level (simulates WU stream_id=0) and
        # stream-level (simulates WU stream_id=1).
        sender.connection_window_size = len(payload) + 200
        sender.window_update(len(payload) + 200)
        await task

        assert payload in bytes(written), 'DATA not written after window_update()'

    async def test_zero_window_does_not_block_headers_send(self):
        """RFC 7540 §6.9.1 — only DATA frames are flow-controlled.

        HEADERS must be written immediately even when the stream window is 0.
        Regression: the old _write() gated all frame types on flow control,
        blocking HEADERS along with DATA.
        """
        from blackbull.server.sender import HTTP2Sender
        from http import HTTPStatus

        written = bytearray()
        mock_writer = MagicMock()
        mock_writer.write = MagicMock(side_effect=lambda d: written.extend(d))
        mock_writer.drain = AsyncMock()
        sender = HTTP2Sender(AsyncioWriter(mock_writer), FrameFactory(), stream_id=1)
        sender.connection_window_size = 0
        sender.stream_window_size = 0

        body = b'x' * 100
        task = asyncio.create_task(sender(body, HTTPStatus.OK, headers=[]))
        await asyncio.sleep(0)

        seen_headers = False
        i = 0
        while i + 9 <= len(written):
            length = int.from_bytes(written[i:i+3], 'big')
            if written[i+3] == 0x01:  # HEADERS frame type
                seen_headers = True
                break
            i += 9 + length
        assert seen_headers, 'HEADERS frame must be written even when stream window is 0'
        assert body not in bytes(written), 'DATA must remain blocked at zero window'

        # Credit both windows: connection-level (simulates WU stream_id=0) and
        # stream-level (simulates WU stream_id=1).
        sender.connection_window_size = len(body) + 200
        sender.window_update(len(body) + 200)
        await task
        assert body in bytes(written), 'DATA must be written after window_update()'


# ---------------------------------------------------------------------------
# RFC 7540 §5.1.2 — MAX_CONCURRENT_STREAMS
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestHTTP2MaxConcurrentStreams:
    """HTTP2Actor must enforce MAX_CONCURRENT_STREAMS."""

    async def test_streams_within_limit_are_processed(self):
        h1 = _make_headers_frame(stream_id=1, end_stream=True)
        h2 = _make_headers_frame(stream_id=3, end_stream=True)

        handler, app = _make_h2_actor()
        handler.max_concurrent_streams = 10
        handler.receive = AsyncMock(side_effect=[h1, h2, None])
        await handler.run()

        assert app.call_count == 2

    async def test_exceeding_max_streams_sends_rst_stream(self):
        """A request beyond MAX_CONCURRENT_STREAMS must get RST_STREAM(REFUSED_STREAM)."""
        frames = [_make_headers_frame(stream_id=i * 2 - 1, end_stream=True)
                  for i in range(1, 4)]

        handler, app = _make_h2_actor()
        handler.max_concurrent_streams = 2
        handler.receive = AsyncMock(side_effect=frames + [None])
        await handler.run()

        sent_types = [call.args[0].FrameType()
                      for call in handler.send_frame.call_args_list
                      if hasattr(call.args[0], 'FrameType')]

        assert FrameTypes.RST_STREAM in sent_types, (
            'Expected RST_STREAM for the third request that exceeds '
            'MAX_CONCURRENT_STREAMS=2. No RST_STREAM was sent.'
        )

    async def test_inbound_rst_cancels_running_stream_task(self):
        """An inbound RST_STREAM must cancel the running handler for that stream.

        Regression for the gRPC server-streaming 'collapse': a streaming handler
        the client abandons mid-flight otherwise blocks forever in the sender's
        flow-control wait (no further WINDOW_UPDATE ever arrives from the
        departed client), permanently holding its max_concurrent_streams slot.
        Under a high-churn streaming client that leaks every slot until new
        streams are REFUSED_STREAM'd.  Cancelling the handler on RST frees it.
        """
        async def blocking_app(scope, receive, send):
            # Stand-in for a server-streaming call parked on flow control —
            # never completes on its own; only cancellation ends it.
            await asyncio.Event().wait()

        h1 = _make_headers_frame(stream_id=1, end_stream=True,
                                 method=b'POST', path=b'/stream')
        # RST_STREAM(CANCEL) on stream 1 — no flags, 4-byte error code.
        rst = _make_h2_frame(FrameTypes.RST_STREAM, 0, 1, (0x8).to_bytes(4, 'big'))

        handler, _ = _make_h2_actor(app=blocking_app)
        handler.receive = AsyncMock(side_effect=[h1, rst, None])

        # With the fix, run() returns: the RST cancels the blocked task so the
        # TaskGroup can exit.  Without it, the TaskGroup awaits the blocked task
        # forever and this wait_for times out (the regression this guards).
        await asyncio.wait_for(handler.run(), timeout=5)
        # Let the task's done-callback fire (scheduled via call_soon).
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert handler._active_stream_count == 0, \
            'max_concurrent_streams slot leaked after inbound RST_STREAM'
        assert 1 not in handler._stream_tasks, \
            'stream task map not cleaned up after inbound RST_STREAM'

    async def test_refused_multiframe_headers_consumes_continuation(self):
        """Bug 1.14 #2 — refusing a HEADERS with END_HEADERS=0 must not turn
        the peer's legal CONTINUATION into a bogus connection error.

        RFC 9113 §6.10: after HEADERS without END_HEADERS the peer MUST send
        the rest of the block as CONTINUATION frames.  A server that refuses
        the stream at the HEADERS frame and forgets it was mid-block then
        misreads those CONTINUATIONs as 'unexpected CONTINUATION' and kills
        the whole connection with GOAWAY(PROTOCOL_ERROR).  The block must be
        consumed and the stream refused with RST_STREAM(REFUSED_STREAM) only.
        """
        from blackbull.protocol.frame_types import ErrorCodes

        encoder = Encoder()
        block = encoder.encode([(b':method', b'GET'),
                                (b':path', b'/'),
                                (b':scheme', b'https'), (b':authority', b'example.com')])
        mid = len(block) // 2
        h1 = _make_headers_frame(stream_id=1, end_stream=True)
        h3 = _make_h2_frame(FrameTypes.HEADERS, 0, 3, block[:mid])
        c3 = _make_h2_frame(FrameTypes.CONTINUATION,
                            HeaderFrameFlags.END_HEADERS, 3, block[mid:])

        handler, _ = _make_h2_actor()
        handler.max_concurrent_streams = 1
        handler.receive = AsyncMock(side_effect=[h1, h3, c3, None])
        await asyncio.wait_for(handler.run(), timeout=5)

        sent = [c.args[0] for c in handler.send_frame.call_args_list
                if hasattr(c.args[0], 'FrameType')]
        goaways = [f for f in sent if f.FrameType() == FrameTypes.GOAWAY]
        assert not goaways, (
            'refused mid-block stream escalated to a connection error '
            '(GOAWAY) on the peer\'s legal CONTINUATION'
        )
        refused = [f for f in sent
                   if f.FrameType() == FrameTypes.RST_STREAM
                   and f.stream_id == 3
                   and getattr(f, 'error_code', None) == ErrorCodes.REFUSED_STREAM]
        assert len(refused) == 1, (
            f'expected exactly one RST_STREAM(REFUSED_STREAM) for stream 3, '
            f'got {len(refused)}'
        )

    async def test_closed_stream_frees_slot(self):
        """After a stream closes, a new one must be accepted within the limit."""
        h1 = _make_headers_frame(stream_id=1, end_stream=True)
        h3 = _make_headers_frame(stream_id=3, end_stream=True)

        handler, app = _make_h2_actor()
        handler.max_concurrent_streams = 1

        frames = iter([h1, h3, b''])

        async def _yielding_receive():
            # Two yields are required before h3:
            # 1st sleep(0): stream 1's task step runs and schedules done_callback via call_soon
            # 2nd sleep(0): done_callback fires, decrementing _active_stream_count to 0
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            return next(frames)

        handler.receive = _yielding_receive

        await handler.run()

        assert app.call_count == 2, (
            f'Expected 2 app calls (stream 1 closes before stream 3 opens), '
            f'got {app.call_count}'
        )


# ---------------------------------------------------------------------------
# RFC 7540 §6.8 — GOAWAY
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestHTTP2GoAway:
    """GOAWAY frame must carry the last processed stream ID."""

    def _make_goaway_frame(self, last_stream_id: int = 0,
                           error_code: int = 0x0) -> bytes:
        payload = last_stream_id.to_bytes(4, 'big') + error_code.to_bytes(4, 'big')
        return _make_h2_frame(FrameTypes.GOAWAY, SettingFrameFlags.INIT, 0, payload)

    def _last_stream_id_from_goaway_calls(self, handler) -> list[int]:
        ids = []
        for call in handler.send_frame.call_args_list:
            frame = call.args[0]
            if hasattr(frame, 'FrameType') and frame.FrameType() == FrameTypes.GOAWAY:
                # last_stream_id is a separate payload field; frame.stream_id
                # of a GOAWAY MUST be 0 (RFC 9113 §6.8) and is therefore not
                # the value to compare here.
                ids.append(getattr(frame, 'last_stream_id', -1))
        return ids

    async def test_goaway_response_carries_last_stream_id(self):
        """Server GOAWAY must include the ID of the last successfully processed stream."""
        h1 = _make_headers_frame(stream_id=1, end_stream=True)
        h3 = _make_headers_frame(stream_id=3, end_stream=True)
        client_goaway = self._make_goaway_frame(last_stream_id=3, error_code=0x0)

        handler, app = _make_h2_actor()
        handler.receive = AsyncMock(side_effect=[h1, h3, client_goaway, None])
        await handler.run()

        goaway_ids = self._last_stream_id_from_goaway_calls(handler)
        assert any(sid >= 3 for sid in goaway_ids), (
            f'Expected server GOAWAY with last_stream_id >= 3; '
            f'got last_stream_ids={goaway_ids}'
        )

    async def test_protocol_error_sends_goaway_with_error_code(self):
        """An HTTP/2 protocol violation must trigger GOAWAY(PROTOCOL_ERROR)."""
        continuation = _make_h2_frame(FrameTypes.CONTINUATION,
                                      HeaderFrameFlags.END_HEADERS, 1, b'\x00' * 4)

        handler, app = _make_h2_actor()
        handler.receive = AsyncMock(side_effect=[continuation, None])

        try:
            await handler.run()
        except Exception:
            pass

        sent_types = [call.args[0].FrameType()
                      for call in handler.send_frame.call_args_list
                      if hasattr(call.args[0], 'FrameType')]

        assert FrameTypes.GOAWAY in sent_types, (
            'Protocol violation (CONTINUATION without HEADERS) must trigger GOAWAY. '
            f'Frames sent: {sent_types}'
        )

    async def test_no_new_streams_accepted_after_receiving_goaway(self):
        """After receiving GOAWAY, further HEADERS must not reach the app."""
        client_goaway = self._make_goaway_frame(last_stream_id=0, error_code=0x0)
        h_after = _make_headers_frame(stream_id=5, end_stream=True)

        handler, app = _make_h2_actor()
        handler.receive = AsyncMock(side_effect=[client_goaway, h_after, None])
        await handler.run()

        assert app.call_count == 0, (
            f'App must not be called after GOAWAY; got call_count={app.call_count}'
        )


# ---------------------------------------------------------------------------
# RFC 7540 §8.2 — Server push (PUSH_PROMISE)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestHTTP2ServerPush:
    """HTTP2Actor must support server push via 'http.response.push' ASGI event."""

    async def test_push_promise_frame_emitted(self):
        """'http.response.push' must cause a PUSH_PROMISE frame to be sent."""
        h_frame = _make_headers_frame(stream_id=1, end_stream=True)

        async def app(scope, receive, send):
            await send({
                'type': 'http.response.push',
                'path': '/style.css',
                'headers': [(b':method', b'GET'), (b':path', b'/style.css'),
                            (b':scheme', b'https'), (b':authority', b'example.com')],
            })
            await send({'type': 'http.response.start', 'status': 200, 'headers': []})
            await send({'type': 'http.response.body', 'body': b''})

        handler, _ = _make_h2_actor(app=app)
        handler.receive = AsyncMock(side_effect=[h_frame, None])
        await handler.run()

        sent_types = [
            call.args[0].FrameType()
            for call in handler.send_frame.call_args_list
            if hasattr(call.args[0], 'FrameType')
        ]
        assert FrameTypes.PUSH_PROMISE in sent_types, (
            f'Expected PUSH_PROMISE frame among sent frames; got {sent_types}'
        )

    async def test_push_promise_has_even_stream_id(self):
        """The promised stream ID in PUSH_PROMISE must be even (RFC 7540 §5.1.1)."""
        h_frame = _make_headers_frame(stream_id=1, end_stream=True)

        async def app(scope, receive, send):
            await send({
                'type': 'http.response.push',
                'path': '/favicon.ico',
                'headers': [(b':method', b'GET'), (b':path', b'/favicon.ico'),
                            (b':scheme', b'https'), (b':authority', b'example.com')],
            })
            await send({'type': 'http.response.start', 'status': 200, 'headers': []})
            await send({'type': 'http.response.body', 'body': b''})

        handler, _ = _make_h2_actor(app=app)
        handler.receive = AsyncMock(side_effect=[h_frame, None])
        await handler.run()

        for call in handler.send_frame.call_args_list:
            frame = call.args[0]
            if hasattr(frame, 'FrameType') and frame.FrameType() == FrameTypes.PUSH_PROMISE:
                assert hasattr(frame, 'promised_stream_id'), (
                    'PUSH_PROMISE frame must have a promised_stream_id attribute'
                )
                assert frame.promised_stream_id % 2 == 0, (
                    f'Promised stream ID must be even; got {frame.promised_stream_id}'
                )
                return
        pytest.fail('No PUSH_PROMISE frame found among sent frames')

    async def test_unknown_push_event_does_not_crash_handler(self):
        """An unrecognised send event type must be logged and ignored, not crash."""
        h_frame = _make_headers_frame(stream_id=1, end_stream=True)

        async def app(scope, receive, send):
            await send({'type': 'http.response.push', 'path': '/x', 'headers': []})
            await send({'type': 'http.response.start', 'status': 200, 'headers': []})
            await send({'type': 'http.response.body', 'body': b''})

        handler, _ = _make_h2_actor(app=app)
        handler.receive = AsyncMock(side_effect=[h_frame, None])
        await handler.run()

    async def test_push_dispatches_synthetic_request_to_app(self):
        """After http.response.push the app must be called with a GET scope for
        the pushed path on the promised stream."""
        h_frame = _make_headers_frame(stream_id=1, end_stream=True)

        scopes: list[dict] = []

        async def app(scope, receive, send):
            scopes.append(scope)
            if scope.get('path') == '/':
                await send({
                    'type': 'http.response.push',
                    'path': '/pushed.css',
                    'headers': [],
                })
            await send({'type': 'http.response.start', 'status': 200, 'headers': []})
            await send({'type': 'http.response.body', 'body': b''})

        handler, _ = _make_h2_actor(app=app)
        handler.receive = AsyncMock(side_effect=[h_frame, None])
        await handler.run()

        assert len(scopes) == 2, (
            f'app must be called twice (original + push); got {len(scopes)} call(s)'
        )
        push_scope = next((s for s in scopes if s.get('path') == '/pushed.css'), None)
        assert push_scope is not None, (
            "No synthetic scope with path='/pushed.css' was delivered to the app"
        )
        assert push_scope.get('method') == 'GET', (
            f"Pushed scope must have method=GET; got {push_scope.get('method')!r}"
        )

    async def test_scope_has_push_extension(self):
        """HTTP/2 scope must advertise 'http.response.push' in extensions."""
        h_frame = _make_headers_frame(stream_id=1, end_stream=True)

        scopes: list[dict] = []

        async def app(scope, receive, send):
            scopes.append(scope)
            await send({'type': 'http.response.start', 'status': 200, 'headers': []})
            await send({'type': 'http.response.body', 'body': b''})

        handler, _ = _make_h2_actor(app=app)
        handler.receive = AsyncMock(side_effect=[h_frame, None])
        await handler.run()

        assert scopes, 'app must have been called'
        exts = scopes[0].get('extensions', {})
        assert 'http.response.push' in exts, (
            f"scope['extensions'] must contain 'http.response.push'; got {exts!r}"
        )


# ---------------------------------------------------------------------------
# RFC 7540 §5.1 — Stream state machine
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestHTTP2StreamStateMachine:
    """HTTP/2 stream state machine: idle → open → half-closed → closed."""

    def _import_state(self):
        from blackbull.protocol.stream import StreamState
        return StreamState

    async def test_new_stream_is_idle(self):
        StreamState = self._import_state()
        from blackbull.protocol.stream import Stream
        stream = Stream(stream_id=1, parent=None)
        assert stream.state == StreamState.IDLE

    async def test_headers_received_opens_stream(self):
        StreamState = self._import_state()
        from blackbull.protocol.stream import Stream
        stream = Stream(stream_id=1, parent=None)
        stream.on_headers_received(end_stream=False)
        assert stream.state == StreamState.OPEN

    async def test_headers_with_end_stream_half_closes(self):
        StreamState = self._import_state()
        from blackbull.protocol.stream import Stream
        stream = Stream(stream_id=1, parent=None)
        stream.on_headers_received(end_stream=True)
        assert stream.state == StreamState.HALF_CLOSED_REMOTE

    async def test_data_end_stream_half_closes_remote(self):
        """RFC 9113 §5.1 — the peer's END_STREAM closes only *their* half.

        The stream is fully CLOSED only once the server side also ends
        (response done-callback) or an RST_STREAM is exchanged.
        """
        StreamState = self._import_state()
        from blackbull.protocol.stream import Stream
        stream = Stream(stream_id=1, parent=None)
        stream.on_headers_received(end_stream=False)
        stream.on_data_received(end_stream=True)
        assert stream.state == StreamState.HALF_CLOSED_REMOTE

    async def test_window_update_after_client_half_close_is_accepted(self):
        """RFC 9113 §5.1 — half-closed (remote) permits WINDOW_UPDATE.

        A client that has sent END_STREAM may still credit the server's
        in-flight response DATA.  Answering that WINDOW_UPDATE with
        RST_STREAM tears down a live bidi stream mid-response (the gRPC
        ``test_echo_each_message`` RST(5) flake).
        """
        h_frame = _make_headers_frame(stream_id=1, method=b'POST')
        d_frame = _make_h2_frame(
            FrameTypes.DATA, DataFrameFlags.END_STREAM, 1, b'hi')
        wu_frame = _make_h2_frame(
            FrameTypes.WINDOW_UPDATE, 0, 1, (1024).to_bytes(4, 'big'))

        release = asyncio.Event()

        async def app(scope, receive, send):
            # Keep the handler in flight so the stream node is still live
            # (not yet pruned) when the WINDOW_UPDATE arrives.
            await release.wait()

        handler, _ = _make_h2_actor(app=app)
        frames = [h_frame, d_frame, wu_frame]

        async def receive():
            if frames:
                return frames.pop(0)
            release.set()
            return None

        handler.receive = receive
        await handler.run()

        bad_calls = [
            call for call in handler.send_frame.call_args_list
            if hasattr(call.args[0], 'FrameType')
            and call.args[0].FrameType() in (FrameTypes.RST_STREAM,
                                             FrameTypes.GOAWAY)
        ]
        assert not bad_calls, (
            'WINDOW_UPDATE on a half-closed (remote) stream must be '
            f'accepted, not answered with RST/GOAWAY; got {bad_calls}'
        )

    async def test_data_on_closed_stream_triggers_rst_stream(self):
        """Receiving DATA on a closed stream must cause RST_STREAM(STREAM_CLOSED)."""
        h_frame = _make_headers_frame(stream_id=1, end_stream=True)
        d_frame = _make_h2_frame(FrameTypes.DATA, DataFrameFlags.END_STREAM, 1, b'late')

        handler, _ = _make_h2_actor()
        handler.receive = AsyncMock(side_effect=[h_frame, d_frame, None])
        await handler.run()

        rst_calls = [
            call for call in handler.send_frame.call_args_list
            if hasattr(call.args[0], 'FrameType')
            and call.args[0].FrameType() == FrameTypes.RST_STREAM
        ]
        assert rst_calls, (
            'Must send RST_STREAM(STREAM_CLOSED) for DATA received on a closed stream'
        )


# ---------------------------------------------------------------------------
# RFC 7540 §5.3 / §6.3 — PRIORITY frames
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestHTTP2Priority:
    """HTTP2Actor must honour PRIORITY frames."""

    @staticmethod
    def _priority_frame(stream_id: int, depends_on: int = 0,
                        weight: int = 16) -> bytes:
        dep = depends_on.to_bytes(4, 'big')
        payload = dep + bytes([weight - 1])
        return _make_h2_frame(FrameTypes.PRIORITY, 0, stream_id, payload)

    async def test_priority_frame_accepted_without_connection_error(self):
        priority = self._priority_frame(stream_id=3, weight=32)

        handler, _ = _make_h2_actor()
        handler.receive = AsyncMock(side_effect=[priority, None])
        await handler.run()

        goaway_calls = [
            call for call in handler.send_frame.call_args_list
            if hasattr(call.args[0], 'FrameType')
            and call.args[0].FrameType() == FrameTypes.GOAWAY
        ]
        assert not goaway_calls, (
            f'PRIORITY frame must not trigger GOAWAY; got {goaway_calls}'
        )

    async def test_stream_weight_stored_after_priority_frame(self):
        priority = self._priority_frame(stream_id=3, weight=32)

        handler, _ = _make_h2_actor()
        handler.receive = AsyncMock(side_effect=[priority, None])
        await handler.run()

        stream = handler.root_stream.find_child(3)
        assert stream is not None, 'Stream 3 must exist after PRIORITY frame'
        assert stream.weight == 32, (
            f'Expected weight=32 after PRIORITY; got {stream.weight}'
        )


# ---------------------------------------------------------------------------
# RFC 9218 — PRIORITY_UPDATE: parse_priority_field + scope population
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestHTTP2PriorityScope:
    """scope['http2_priority'] must be set on every HTTP/2 request."""

    @staticmethod
    def _make_priority_update_frame(prioritized_stream_id: int,
                                    priority_field: str) -> bytes:
        payload = (prioritized_stream_id.to_bytes(4, 'big')
                   + priority_field.encode())
        return _make_h2_frame(FrameTypes.PRIORITY_UPDATE, 0, 0, payload)

    async def test_parse_priority_field_default(self):
        from blackbull.protocol.frame_types import parse_priority_field
        assert parse_priority_field('') == {'urgency': 3, 'incremental': False}

    async def test_parse_priority_field_urgency(self):
        from blackbull.protocol.frame_types import parse_priority_field
        assert parse_priority_field('u=5') == {'urgency': 5, 'incremental': False}

    async def test_parse_priority_field_urgency_and_incremental(self):
        from blackbull.protocol.frame_types import parse_priority_field
        assert parse_priority_field('u=2, i') == {'urgency': 2, 'incremental': True}

    async def test_parse_priority_field_explicit_incremental_boolean(self):
        # RFC 9651 valueless `i` and explicit `i=?1` / `i=?0` are all valid.
        from blackbull.protocol.frame_types import parse_priority_field
        assert parse_priority_field('i=?1') == {'urgency': 3, 'incremental': True}
        assert parse_priority_field('u=2, i=?0') == {'urgency': 2, 'incremental': False}

    async def test_parse_priority_field_out_of_range_urgency_ignored(self):
        # RFC 9218 §4 — out-of-range values MUST be ignored (not clamped):
        # the default urgency 3 applies.
        from blackbull.protocol.frame_types import parse_priority_field
        assert parse_priority_field('u=9') == {'urgency': 3, 'incremental': False}
        assert parse_priority_field('u=-1') == {'urgency': 3, 'incremental': False}

    async def test_parse_priority_field_mistyped_members_ignored(self):
        # RFC 9218 §4 — values of unexpected types MUST be ignored.
        from blackbull.protocol.frame_types import parse_priority_field
        assert parse_priority_field('u=abc, i=1') == {'urgency': 3, 'incremental': False}
        assert parse_priority_field('u=?1') == {'urgency': 3, 'incremental': False}

    async def test_parse_priority_field_unknown_members_ignored(self):
        from blackbull.protocol.frame_types import parse_priority_field
        assert parse_priority_field('x=5, u=1') == {'urgency': 1, 'incremental': False}

    async def test_parse_priority_field_malformed_falls_back_to_defaults(self):
        # A Priority field that fails strict RFC 9651 parsing is ignored
        # entirely (RFC 9218 §5) — defaults apply.
        from blackbull.protocol.frame_types import parse_priority_field
        assert parse_priority_field('???') == {'urgency': 3, 'incremental': False}
        assert parse_priority_field('u=2, ???') == {'urgency': 3, 'incremental': False}

    async def test_scope_has_default_http2_priority(self):
        h_frame = _make_headers_frame(stream_id=1, end_stream=True)

        scopes = []

        async def app(scope, receive, send):
            scopes.append(scope)

        handler, _ = _make_h2_actor(app=app)
        handler.receive = AsyncMock(side_effect=[h_frame, None])
        await handler.run()

        assert scopes, 'app must have been called'
        assert scopes[0].get('http2_priority') == {'urgency': 3, 'incremental': False}

    async def test_priority_update_before_headers_populates_scope(self):
        pu_frame = self._make_priority_update_frame(
            prioritized_stream_id=1, priority_field='u=1, i')
        h_frame = _make_headers_frame(stream_id=1, end_stream=True)

        scopes = []

        async def app(scope, receive, send):
            scopes.append(scope)

        handler, _ = _make_h2_actor(app=app)
        handler.receive = AsyncMock(side_effect=[pu_frame, h_frame, None])
        await handler.run()

        assert scopes, 'app must have been called'
        assert scopes[0].get('http2_priority') == {'urgency': 1, 'incremental': True}

    async def test_priority_header_fallback_populates_scope(self):
        encoder = Encoder()
        block = encoder.encode([
            (b':method', b'GET'),
            (b':path', b'/'),
            (b':scheme', b'https'),
            (b':authority', b'example.com'),
            (b'priority', b'u=6'),
        ])
        flags = HeaderFrameFlags.END_HEADERS | HeaderFrameFlags.END_STREAM
        h_frame = _make_h2_frame(FrameTypes.HEADERS, flags, 1, block)

        scopes = []

        async def app(scope, receive, send):
            scopes.append(scope)

        handler, _ = _make_h2_actor(app=app)
        handler.receive = AsyncMock(side_effect=[h_frame, None])
        await handler.run()

        assert scopes, 'app must have been called'
        assert scopes[0].get('http2_priority') == {'urgency': 6, 'incremental': False}


# ---------------------------------------------------------------------------
# scope['headers'] must contain all request headers, not just pseudo-headers
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestHTTP2ScopeHeaders:
    """HTTP/2 request headers must appear in scope['headers'] as a Headers object."""

    @staticmethod
    def _make_headers_frame_with_cookie(
        stream_id: int = 1,
        method: bytes = b'GET',
        path: bytes = b'/',
        cookie: bytes = b'session_id=abc123',
        end_stream: bool = True,
    ) -> bytes:
        encoder = Encoder()
        block = encoder.encode([
            (b':method', method),
            (b':path', path),
            (b':scheme', b'https'),
            (b':authority', b'example.com'),
            (b'cookie', cookie),
        ])
        flags: FrameFlags = HeaderFrameFlags.END_HEADERS
        if end_stream:
            flags |= HeaderFrameFlags.END_STREAM
        return _make_h2_frame(FrameTypes.HEADERS, flags, stream_id, block)

    async def test_scope_headers_is_not_plain_empty_list(self):
        h_frame = self._make_headers_frame_with_cookie()
        scopes = []

        async def app(scope, receive, send):
            scopes.append(scope)

        handler, _ = _make_h2_actor(app=app)
        handler.receive = AsyncMock(side_effect=[h_frame, None])
        await handler.run()

        assert scopes, 'app must have been called'
        assert scopes[0]['headers'] != [], (
            "scope['headers'] is an empty list — Cookie and other request headers "
            "are missing from the HTTP/2 scope"
        )

    async def test_scope_headers_contains_cookie(self):
        from blackbull.headers import Headers

        h_frame = self._make_headers_frame_with_cookie(cookie=b'session_id=abc123')
        scopes = []

        async def app(scope, receive, send):
            scopes.append(scope)

        handler, _ = _make_h2_actor(app=app)
        handler.receive = AsyncMock(side_effect=[h_frame, None])
        await handler.run()

        assert scopes, 'app must have been called'
        scope = scopes[0]

        assert isinstance(scope['headers'], Headers), (
            f"scope['headers'] must be a Headers instance; got {type(scope['headers'])!r}"
        )
        cookie_val = scope['headers'].get(b'cookie')
        assert cookie_val == b'session_id=abc123', (
            f"Expected cookie b'session_id=abc123' in scope['headers']; got {cookie_val!r}"
        )


# ---------------------------------------------------------------------------
# Each HTTP/2 stream must have its own isolated receive queue
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestHTTP2PerStreamRecipient:
    """Each HTTP/2 stream must have its own isolated receive queue.

    Bug: a shared HTTP2Recipient caused GET's empty http.request event to be
    consumed by a subsequent POST handler instead of the DATA frame body.
    """

    @staticmethod
    def _make_post_headers_frame(stream_id: int, path: bytes = b'/login') -> bytes:
        encoder = Encoder()
        block = encoder.encode([
            (b':method', b'POST'),
            (b':path', path),
            (b':scheme', b'https'),
            (b':authority', b'example.com'),
        ])
        return _make_h2_frame(FrameTypes.HEADERS,
                              HeaderFrameFlags.END_HEADERS,
                              stream_id, block)

    async def test_post_body_not_contaminated_by_prior_get(self):
        json_body = b'{"username":"alice","method":"sse"}'

        h_get  = _make_headers_frame(stream_id=1, end_stream=True,
                                     method=b'GET', path=b'/')
        h_post = self._make_post_headers_frame(stream_id=3, path=b'/login')
        d_post = _make_h2_frame(FrameTypes.DATA, DataFrameFlags.END_STREAM,
                                3, json_body)

        received_bodies: dict[str, bytes] = {}

        async def app(scope, receive, send):
            if scope.get('method') == 'POST':
                event = await receive()
                received_bodies[scope.get('path', '?')] = event.get('body', b'<none>')

        handler, _ = _make_h2_actor(app=app)
        handler.receive = AsyncMock(side_effect=[h_get, h_post, d_post, None])
        await handler.run()

        assert '/login' in received_bodies, (
            'POST /login handler did not call receive() — was it reached?'
        )
        assert received_bodies['/login'] == json_body, (
            f"POST /login received the wrong body.\n"
            f"  Expected : {json_body!r}\n"
            f"  Got      : {received_bodies['/login']!r}\n"
        )


# ---------------------------------------------------------------------------
# SETTINGS_MAX_HEADER_LIST_SIZE must parse without AttributeError
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestSettingsMaxHeaderListSize:
    """SETTINGS_MAX_HEADER_LIST_SIZE must be parsed without AttributeError."""

    @staticmethod
    def _make_settings_frame(params: dict[int, int]) -> bytes:
        payload = b''
        for identifier, value in params.items():
            payload += identifier.to_bytes(2, 'big') + value.to_bytes(4, 'big')
        return _make_h2_frame(FrameTypes.SETTINGS, SettingFrameFlags.INIT, 0, payload)

    async def test_settings_with_only_max_header_list_size_does_not_crash(self):
        """A SETTINGS frame containing only MAX_HEADER_LIST_SIZE must not crash."""
        settings_frame = self._make_settings_frame({0x6: 16384})
        h_frame = _make_headers_frame(stream_id=1, end_stream=True)

        handler, app = _make_h2_actor()
        handler.receive = AsyncMock(side_effect=[settings_frame, h_frame, None])

        await handler.run()

        assert app.call_count == 1, (
            f'App must be called once (connection must stay open after SETTINGS); '
            f'got call_count={app.call_count}'
        )

    async def test_settings_max_header_list_size_value_is_stored(self):
        """MAX_HEADER_LIST_SIZE value must be stored in the SettingFrame object."""
        factory = FrameFactory()
        payload = (0x6).to_bytes(2, 'big') + (262144).to_bytes(4, 'big')
        wire = _make_h2_frame(FrameTypes.SETTINGS, SettingFrameFlags.INIT, 0, payload)
        frame = factory.load(wire)
        assert hasattr(frame, 'max_header_list_size'), (
            'SettingFrame must store max_header_list_size after parsing 0x6 setting'
        )
        assert frame.max_header_list_size == 262144


# ---------------------------------------------------------------------------
# RFC 7540 §6.5.2 — SETTINGS_HEADER_TABLE_SIZE must not mutate our decoder
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestSettingsHeaderTableSize:
    """SETTINGS_HEADER_TABLE_SIZE from the peer must NOT mutate our HPACK decoder.

    RFC 7540 §6.5.2: the peer's HEADER_TABLE_SIZE value bounds OUR encoder's
    table, not our decoder's.  Mutating the decoder causes hpack to raise
    InvalidTableSizeError on the next HEADERS frame unless the peer's encoder
    also sends a Dynamic Table Size Update instruction — which browsers never do.
    """

    async def test_settings_header_table_size_then_headers_dispatches(self):
        """A SETTINGS(HEADER_TABLE_SIZE=65536) followed by HEADERS must dispatch normally.

        Regression: the old SettingsResponder set handler.factory.header_table_size,
        mutating the decoder and causing InvalidTableSizeError on the next HEADERS
        frame decode.  This was the root cause of the browser-connection failure.
        """
        settings = TestSettingsMaxHeaderListSize._make_settings_frame({0x1: 65536})
        h_frame = _make_headers_frame(stream_id=1, end_stream=True)
        handler, app = _make_h2_actor()
        handler.receive = AsyncMock(side_effect=[settings, h_frame, None])
        await handler.run()
        assert app.call_count == 1, (
            f'HEADERS after SETTINGS(HEADER_TABLE_SIZE) must dispatch; '
            f'got call_count={app.call_count}')


# ---------------------------------------------------------------------------
# TCP fragmentation — readexactly(n) must reassemble split frame headers
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestHTTP2FragmentedRead:
    """HTTP2Actor.receive() must use readexactly so TCP-fragmented frames parse correctly.

    Regression: the old receive() used read(n) which returns up to n bytes.
    On a byte-by-byte reader read(9) returns 1 byte, making the 9-byte frame
    header unparseable.  readexactly(9) correctly waits for all 9 bytes.
    """

    async def test_fragmented_headers_frame_dispatches(self):
        """A HEADERS frame delivered one byte at a time must still dispatch to the app."""
        h_frame_bytes = _make_headers_frame(stream_id=1, end_stream=True)
        reader = _ByteByByteReader(h_frame_bytes)

        app = AsyncMock()
        writer = MagicMock()
        writer.drain = AsyncMock()
        writer.close = MagicMock()

        handler = HTTP2Actor(reader, AsyncioWriter(writer), app, aggregator=None)
        handler.send_frame = AsyncMock()
        await handler.run()

        assert app.call_count == 1, (
            f'App must be called once from byte-by-byte fragmented HEADERS; '
            f'got call_count={app.call_count}')


# ---------------------------------------------------------------------------
# Browser-shaped frames (HEADERS with PRIORITY flag, flags=0x25)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestHTTP2BrowserShapedFrames:
    """HEADERS with PRIORITY flag (flags=0x25) must dispatch identically.

    Firefox and Edge always set END_STREAM | END_HEADERS | PRIORITY on their
    first request HEADERS frame.  curl does not.  This class ensures the test
    corpus includes browser-shaped frames so regressions in PRIORITY-flagged
    HEADERS decoding fail loudly.
    """

    async def test_headers_with_priority_flag_dispatches(self):
        """HEADERS flags=0x25 (END_STREAM|END_HEADERS|PRIORITY) must dispatch normally."""
        h_frame = _make_headers_frame(stream_id=1, end_stream=True, priority=(0, 42))
        handler, app = _make_h2_actor()
        handler.receive = AsyncMock(side_effect=[h_frame, None])
        await handler.run()
        assert app.call_count == 1, (
            f'HEADERS with PRIORITY flag must dispatch to app; got call_count={app.call_count}')

    async def test_headers_with_priority_and_settings_header_table_size(self):
        """Browser-shaped: SETTINGS(HEADER_TABLE_SIZE) + HEADERS(PRIORITY) must dispatch.

        This is the exact frame sequence Firefox/Edge send on first connection.
        """
        settings = TestSettingsMaxHeaderListSize._make_settings_frame({0x1: 65536})
        h_frame = _make_headers_frame(stream_id=1, end_stream=True, priority=(0, 42))
        handler, app = _make_h2_actor()
        handler.receive = AsyncMock(side_effect=[settings, h_frame, None])
        await handler.run()
        assert app.call_count == 1, (
            f'Browser-shaped SETTINGS+HEADERS(PRIORITY) must dispatch; '
            f'got call_count={app.call_count}')


# ---------------------------------------------------------------------------
# CVE-2023-44487 (Rapid Reset) — RST_STREAM rate limit
# ---------------------------------------------------------------------------
#
# Attackers open a stream with HEADERS and immediately RST it, churning
# per-stream allocations (Stream node, sender, recipient, HPACK context)
# without hitting ``max_concurrent_streams`` because each stream is
# opened and reset too quickly for the counter to accumulate.  Bound
# the per-second inbound RST_STREAM rate; over the threshold, close
# the connection with GOAWAY ENHANCE_YOUR_CALM.

@pytest.mark.asyncio
class TestRapidReset:
    """``HTTP2Actor._RST_RATE_LIMIT`` caps inbound RST_STREAM frames
    per ``_RST_RATE_WINDOW`` seconds.  Over the cap, the connection
    receives ``GOAWAY ENHANCE_YOUR_CALM``."""

    @staticmethod
    def _rst_frame(stream_id: int, error_code: int = 0) -> bytes:
        from blackbull.protocol.frame_types import FrameTypes as _FT
        payload = error_code.to_bytes(4, 'big')
        return _make_h2_frame(_FT.RST_STREAM, 0, stream_id, payload)

    @staticmethod
    def _pretend_streams_closed(handler, stream_ids):
        """Pre-populate ``_closed_streams`` so the RST_STREAM frames
        arrive on the 'late frame on closed stream' branch instead of
        IDLE-state PROTOCOL_ERROR — lets the test exercise the
        rate-limit guard without orchestrating real HEADERS+RST
        cycles (the real attack shape; the rate-limit guard is
        placed before state validation so both shapes count)."""
        for sid in stream_ids:
            handler._closed_streams[sid] = False  # closed-via-END_STREAM

    async def test_burst_of_rst_stream_emits_goaway(self):
        """Sending RST_STREAM frames at a rate above ``_RST_RATE_LIMIT``
        in a single second must trigger GOAWAY(ENHANCE_YOUR_CALM)."""
        from blackbull.protocol.frame_types import ErrorCodes as _EC
        from blackbull.server.http2_actor import HTTP2Actor

        burst = HTTP2Actor._RST_RATE_LIMIT + 5
        sids = [1 + 2 * i for i in range(burst)]
        frames = [self._rst_frame(stream_id=s) for s in sids]

        handler, _ = _make_h2_actor()
        self._pretend_streams_closed(handler, sids)
        handler.receive = AsyncMock(side_effect=[*frames, None])
        await handler.run()

        goaways = [c.args[0] for c in handler.send_frame.call_args_list
                   if hasattr(c.args[0], 'FrameType')
                   and c.args[0].FrameType() == FrameTypes.GOAWAY]
        assert goaways, 'rapid-reset burst must trigger GOAWAY'
        codes = [getattr(g, 'error_code', None) for g in goaways]
        assert _EC.ENHANCE_YOUR_CALM in codes, (
            f'GOAWAY must use ENHANCE_YOUR_CALM; got codes={codes}')

    async def test_rate_below_cap_does_not_trip(self):
        """Sending RST_STREAM frames *under* the cap must NOT trigger
        GOAWAY — regression lock against an overly-aggressive limit."""
        from blackbull.server.http2_actor import HTTP2Actor

        below = max(1, HTTP2Actor._RST_RATE_LIMIT // 2)
        sids = [1 + 2 * i for i in range(below)]
        frames = [self._rst_frame(stream_id=s) for s in sids]

        handler, _ = _make_h2_actor()
        self._pretend_streams_closed(handler, sids)
        handler.receive = AsyncMock(side_effect=[*frames, None])
        await handler.run()

        goaways = [c.args[0] for c in handler.send_frame.call_args_list
                   if hasattr(c.args[0], 'FrameType')
                   and c.args[0].FrameType() == FrameTypes.GOAWAY]
        assert goaways == [], (
            f'no GOAWAY expected below the rate cap; got {goaways}')
