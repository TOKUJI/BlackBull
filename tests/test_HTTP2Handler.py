"""
Tests for HTTP2Handler (blackbull/server/server.py)
====================================================

P2 items covered:
  - Flow control: window-size gating + WINDOW_UPDATE after consuming DATA
  - MAX_CONCURRENT_STREAMS: RST_STREAM(REFUSED_STREAM) when limit exceeded
  - GOAWAY: last processed stream ID in server GOAWAY; GOAWAY on protocol error
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock
from hpack import Encoder

from blackbull.server.server import HTTP2Handler
from blackbull.frame import (FrameFactory, FrameTypes, FrameFlags,
                              HeaderFrameFlags, DataFrameFlags,
                              SettingFrameFlags)


# ---------------------------------------------------------------------------
# Wire-format helpers
# ---------------------------------------------------------------------------

def _make_h2_frame(type_byte: FrameTypes, flags: FrameFlags | int,
                   stream_id: int, payload: bytes) -> bytes:
    """Build a raw 9-byte HTTP/2 frame header + payload."""
    length = len(payload)
    return (length.to_bytes(3, 'big')
            + type_byte
            + bytes([flags])
            + stream_id.to_bytes(4, 'big')
            + payload)


def _make_headers_frame(stream_id: int = 1, end_stream: bool = False,
                        method: bytes = b'GET', path: bytes = b'/') -> bytes:
    encoder = Encoder()
    block = encoder.encode([(b':method', method),
                             (b':path', path),
                             (b':scheme', b'https')])
    flags: FrameFlags = HeaderFrameFlags.END_HEADERS
    if end_stream:
        flags |= HeaderFrameFlags.END_STREAM
    return _make_h2_frame(FrameTypes.HEADERS, flags, stream_id, block)


def _make_h2_handler(app=None):
    """Create an HTTP2Handler with a fake writer and mocked send_frame."""
    if app is None:
        app = AsyncMock()
    writer = MagicMock()
    writer.drain = AsyncMock()
    writer.close = MagicMock()
    handler = HTTP2Handler(app, reader=None, writer=writer)
    handler.send_frame = AsyncMock()
    return handler, app


# ---------------------------------------------------------------------------
# P2 — HTTP/2 flow control
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestHTTP2FlowControl:
    """HTTP2Handler must gate DATA sends behind the peer's flow-control window.

    P2 items:
    - Check remaining window size before sending DATA; hold if window <= 0.
    - Issue a WINDOW_UPDATE frame after the ASGI app consumes received DATA.

    The initial window size is set by the client's SETTINGS frame
    (SETTINGS_INITIAL_WINDOW_SIZE, identifier 0x4).
    """

    def _make_window_update_frame(self, increment: int,
                                  stream_id: int = 0) -> bytes:
        payload = increment.to_bytes(4, 'big')
        return _make_h2_frame(FrameTypes.WINDOW_UPDATE, SettingFrameFlags.INIT,
                              stream_id, payload)

    async def test_window_update_received_is_tracked(self):
        """A connection-level WINDOW_UPDATE from the client must increase the
        connection window on all cached stream senders.

        The window is now owned by each HTTP2Sender instance rather than by
        the handler, so we verify via the sender's connection_window_size.
        """
        h_frame = _make_headers_frame(stream_id=1, end_stream=True)
        wu_frame = self._make_window_update_frame(65535, stream_id=0)
        settings = _make_h2_frame(FrameTypes.SETTINGS, SettingFrameFlags.INIT, 0, b'')

        handler, app = _make_h2_handler()
        handler.receive = AsyncMock(side_effect=[settings, h_frame, wu_frame, None])

        await handler.run()

        # After the HEADERS frame, a sender for stream 1 is cached.
        # The connection-level WINDOW_UPDATE must have incremented its window.
        sender = handler._senders.get(1)
        assert sender is not None, 'Sender for stream 1 must be cached after HEADERS'
        assert sender.connection_window_size >= 65535, (
            f'Expected connection_window_size >= 65535 after WINDOW_UPDATE, '
            f'got {sender.connection_window_size}'
        )

    @staticmethod
    def _wu_increments(handler) -> list[int]:
        """Extract window_size from every WINDOW_UPDATE frame sent by the handler."""
        return [call.args[0].window_size
                for call in handler.send_frame.call_args_list
                if hasattr(call.args[0], 'FrameType')
                and call.args[0].FrameType() == FrameTypes.WINDOW_UPDATE]

    async def test_single_data_frame_window_update_increment(self):
        """Receiving one DATA frame must produce WINDOW_UPDATE with increment
        equal to that frame's payload size.

        RFC 7540 §6.9: the receiver must send WINDOW_UPDATE after consuming data
        so the sender's window is restored exactly.
        """
        payload = b'hello'
        stream_id = 1
        h_frame = _make_headers_frame(stream_id=stream_id, end_stream=False)
        d_frame = _make_h2_frame(FrameTypes.DATA, DataFrameFlags.END_STREAM,
                                 stream_id, payload)

        handler, _ = _make_h2_handler()
        handler.receive = AsyncMock(side_effect=[h_frame, d_frame, None])
        await handler.run()

        increments = self._wu_increments(handler)
        assert increments, 'handler must send WINDOW_UPDATE after consuming DATA'
        assert any(inc == len(payload) for inc in increments), (
            f'WINDOW_UPDATE increment must equal payload size ({len(payload)}); '
            f'got increments={increments}'
        )

    async def test_two_data_frames_window_update_sum(self):
        """Receiving two DATA frames must produce WINDOW_UPDATE increments that
        sum to the total bytes consumed across both frames.

        RFC 7540 §6.9: the receiver may credit per-frame or accumulate; either
        way the total increment must restore the full consumed window.
        An implementation that only credits the last DATA frame would pass the
        single-frame test above but fail here.
        """
        chunk1 = b'hello'
        chunk2 = b' world'
        total = len(chunk1) + len(chunk2)
        stream_id = 1
        h_frame  = _make_headers_frame(stream_id=stream_id, end_stream=False)
        d_frame1 = _make_h2_frame(FrameTypes.DATA, SettingFrameFlags.INIT,
                                  stream_id, chunk1)   # no END_STREAM
        d_frame2 = _make_h2_frame(FrameTypes.DATA, DataFrameFlags.END_STREAM,
                                  stream_id, chunk2)

        handler, _ = _make_h2_handler()
        handler.receive = AsyncMock(side_effect=[h_frame, d_frame1, d_frame2, None])
        await handler.run()

        increments = self._wu_increments(handler)
        assert increments, 'handler must send at least one WINDOW_UPDATE'
        assert sum(increments) == total, (
            f'Sum of WINDOW_UPDATE increments must equal total bytes consumed '
            f'({total}); got {sum(increments)} from increments={increments}'
        )

    async def test_zero_window_blocks_app_data_send(self):
        """When the sender's window is 0, _write() must suspend until window_update().

        Tests the sender directly rather than through the full handler loop to
        avoid task scheduling uncertainty.
        """
        from blackbull.server.sender import HTTP2Sender, AsyncioWriter

        written = bytearray()
        mock_writer = MagicMock()
        mock_writer.write = MagicMock(side_effect=lambda d: written.extend(d))
        mock_writer.drain = AsyncMock()

        factory = FrameFactory()
        sender = HTTP2Sender(AsyncioWriter(mock_writer), factory, stream_identifier=1)

        # Drain the window to zero.
        sender.connection_window_size = 0
        sender.stream_window_size[1] = 0
        sender._window_open.clear()

        payload = b'x' * 100

        async def send_task():
            frame = factory.create(FrameTypes.DATA, DataFrameFlags.END_STREAM, 1, data=payload)
            await sender._write(frame.save())

        task = asyncio.create_task(send_task())
        # Yield control so send_task can start and block on _window_open.
        await asyncio.sleep(0)

        # Nothing should be written yet — sender is blocked.
        assert payload not in bytes(written), (
            'DATA written while window was 0 — flow control not enforced.'
        )

        # Open the window.
        sender.window_update(len(payload) + 200)
        await task  # now completes

        assert payload in bytes(written), 'DATA not written after window_update()'


# ---------------------------------------------------------------------------
# P2 — HTTP/2 MAX_CONCURRENT_STREAMS
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestHTTP2MaxConcurrentStreams:
    """handler must enforce MAX_CONCURRENT_STREAMS.

    P2 item: when the number of open streams reaches the server's advertised
    MAX_CONCURRENT_STREAMS, new HEADERS frames must be rejected with
    RST_STREAM(REFUSED_STREAM) instead of being processed.

    RFC 7540 §5.1.2: endpoints MUST NOT exceed the limit; the receiver may
    treat it as a stream error of type REFUSED_STREAM.
    """

    async def test_streams_within_limit_are_processed(self):
        """Requests within MAX_CONCURRENT_STREAMS must reach the app."""
        h1 = _make_headers_frame(stream_id=1, end_stream=True)
        h2 = _make_headers_frame(stream_id=3, end_stream=True)

        handler, app = _make_h2_handler()
        handler.max_concurrent_streams = 10
        handler.receive = AsyncMock(side_effect=[h1, h2, None])
        await handler.run()

        assert app.call_count == 2

    async def test_exceeding_max_streams_sends_rst_stream(self):
        """A request beyond MAX_CONCURRENT_STREAMS must get RST_STREAM(REFUSED_STREAM).

        P2 bug: no limit is enforced — all requests are queued regardless.
        """
        frames = [_make_headers_frame(stream_id=i * 2 - 1, end_stream=True)
                  for i in range(1, 4)]  # 3 concurrent stream requests

        handler, app = _make_h2_handler()
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

    async def test_closed_stream_frees_slot(self):
        """After a stream closes, a new one must be accepted within the limit."""
        h1 = _make_headers_frame(stream_id=1, end_stream=True)
        h3 = _make_headers_frame(stream_id=3, end_stream=True)

        handler, app = _make_h2_handler()
        handler.max_concurrent_streams = 1
        handler.receive = AsyncMock(side_effect=[h1, h3, None])

        await handler.run()

        assert app.call_count == 2, (
            f'Expected 2 app calls (stream 1 closes before stream 3 opens), '
            f'got {app.call_count}'
        )


# ---------------------------------------------------------------------------
# P2 — HTTP/2 GOAWAY — last processed stream ID + protocol-error GOAWAY
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestHTTP2GoAwayLastStreamId:
    """GOAWAY frame sent by the server must include the last processed stream ID.

    P2 item: RFC 7540 §6.8 — the GOAWAY payload contains a 4-byte
    Last-Stream-ID that tells the peer which requests were processed.
    The current implementation always sends last_stream_id=0 (SettingFlags.INIT),
    losing information about already-processed streams.

    Additionally, the server must send GOAWAY (with PROTOCOL_ERROR) when it
    detects an HTTP/2 protocol violation before closing the connection.
    """

    def _make_goaway_frame(self, last_stream_id: int = 0,
                           error_code: int = 0x0) -> bytes:
        payload = last_stream_id.to_bytes(4, 'big') + error_code.to_bytes(4, 'big')
        return _make_h2_frame(FrameTypes.GOAWAY, SettingFrameFlags.INIT, 0, payload)

    def _last_stream_id_from_goaway_calls(self, handler) -> list[int]:
        ids = []
        for call in handler.send_frame.call_args_list:
            frame = call.args[0]
            if hasattr(frame, 'FrameType') and frame.FrameType() == FrameTypes.GOAWAY:
                ids.append(getattr(frame, 'stream_id', -1))
        return ids

    async def test_goaway_response_carries_last_stream_id(self):
        """Server GOAWAY must include the ID of the last successfully processed stream.

        P2 bug: current code sends SettingFlags.INIT (0x0) as stream_id, always
        reporting 0 regardless of which streams were handled.
        """
        h1 = _make_headers_frame(stream_id=1, end_stream=True)
        h3 = _make_headers_frame(stream_id=3, end_stream=True)
        client_goaway = self._make_goaway_frame(last_stream_id=3, error_code=0x0)

        handler, app = _make_h2_handler()
        handler.receive = AsyncMock(side_effect=[h1, h3, client_goaway, None])
        await handler.run()

        goaway_ids = self._last_stream_id_from_goaway_calls(handler)
        assert any(sid >= 3 for sid in goaway_ids), (
            f'Expected server GOAWAY with last_stream_id >= 3 (streams 1 and 3 '
            f'were processed); got last_stream_ids={goaway_ids}'
        )

    async def test_protocol_error_sends_goaway_with_error_code(self):
        """An HTTP/2 protocol violation must trigger GOAWAY(PROTOCOL_ERROR).

        RFC 7540 §5.4.1: a connection error must be treated by sending a GOAWAY
        frame with the appropriate error code before closing the TCP connection.

        Example violation: CONTINUATION frame without a preceding HEADERS frame.
        """
        continuation = _make_h2_frame(FrameTypes.CONTINUATION,
                                      HeaderFrameFlags.END_HEADERS, 1, b'\x00' * 4)

        handler, app = _make_h2_handler()
        handler.receive = AsyncMock(side_effect=[continuation, None])

        try:
            await handler.run()
        except Exception:
            pass  # handler may raise; we only care about the GOAWAY sent

        sent_types = [call.args[0].FrameType()
                      for call in handler.send_frame.call_args_list
                      if hasattr(call.args[0], 'FrameType')]

        assert FrameTypes.GOAWAY in sent_types, (
            'Protocol violation (CONTINUATION without HEADERS) must trigger GOAWAY. '
            f'Frames sent: {sent_types}'
        )

    async def test_no_new_streams_accepted_after_receiving_goaway(self):
        """After receiving GOAWAY, further HEADERS from client must not reach the app.

        RFC 7540 §6.8: once GOAWAY is received, the endpoint MUST NOT process
        any new streams.
        """
        client_goaway = self._make_goaway_frame(last_stream_id=0, error_code=0x0)
        h_after = _make_headers_frame(stream_id=5, end_stream=True)

        handler, app = _make_h2_handler()
        handler.receive = AsyncMock(side_effect=[client_goaway, h_after, None])
        await handler.run()

        assert app.call_count == 0, (
            f'App must not be called after GOAWAY; got call_count={app.call_count}'
        )


# ---------------------------------------------------------------------------
# P3 — HTTP/2 server push (PUSH_PROMISE)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestHTTP2ServerPush:
    """HTTP2Handler must support server push via the ASGI 'http.response.push'
    event (HTTP/2 PUSH_PROMISE frame, RFC 7540 §8.2).

    When the ASGI app sends:
        {'type': 'http.response.push', 'path': '/style.css', 'headers': [...]}
    the server must emit a PUSH_PROMISE frame that references an even-numbered
    promised stream ID (server-initiated streams are always even per RFC 7540 §5.1.1).
    """

    async def test_push_promise_frame_emitted(self):
        """'http.response.push' must cause a PUSH_PROMISE frame to be sent."""
        h_frame = _make_headers_frame(stream_id=1, end_stream=True)

        async def app(scope, receive, send):
            await send({
                'type': 'http.response.push',
                'path': '/style.css',
                'headers': [(b':method', b'GET'), (b':path', b'/style.css'),
                            (b':scheme', b'https')],
            })
            await send({'type': 'http.response.start', 'status': 200, 'headers': []})
            await send({'type': 'http.response.body', 'body': b''})

        handler, _ = _make_h2_handler(app=app)
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
                            (b':scheme', b'https')],
            })
            await send({'type': 'http.response.start', 'status': 200, 'headers': []})
            await send({'type': 'http.response.body', 'body': b''})

        handler, _ = _make_h2_handler(app=app)
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
            # This should be gracefully ignored (not crash the handler)
            await send({'type': 'http.response.push', 'path': '/x', 'headers': []})
            await send({'type': 'http.response.start', 'status': 200, 'headers': []})
            await send({'type': 'http.response.body', 'body': b''})

        handler, _ = _make_h2_handler(app=app)
        handler.receive = AsyncMock(side_effect=[h_frame, None])
        # Must not raise
        await handler.run()


# ---------------------------------------------------------------------------
# P3 — HTTP/2 stream state machine
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestHTTP2StreamStateMachine:
    """HTTP/2 stream state machine: idle → open → half-closed → closed
    (RFC 7540 §5.1).

    Expected API: blackbull.stream.Stream exposes a ``state`` attribute
    (a ``StreamState`` enum) and methods that transition it on protocol events.
    """

    def _import_state(self):
        from blackbull.stream import StreamState
        return StreamState

    async def test_new_stream_is_idle(self):
        """A freshly created stream must be in the IDLE state."""
        StreamState = self._import_state()
        from blackbull.stream import Stream
        stream = Stream(identifier=1, parent=None)
        assert stream.state == StreamState.IDLE, (
            f'New stream must be IDLE; got {stream.state}'
        )

    async def test_headers_received_opens_stream(self):
        """Receiving a HEADERS frame must transition the stream to OPEN."""
        StreamState = self._import_state()
        from blackbull.stream import Stream
        stream = Stream(identifier=1, parent=None)
        stream.on_headers_received(end_stream=False)
        assert stream.state == StreamState.OPEN, (
            f'Stream must be OPEN after HEADERS; got {stream.state}'
        )

    async def test_headers_with_end_stream_half_closes(self):
        """HEADERS + END_STREAM must transition to HALF_CLOSED_REMOTE."""
        StreamState = self._import_state()
        from blackbull.stream import Stream
        stream = Stream(identifier=1, parent=None)
        stream.on_headers_received(end_stream=True)
        assert stream.state == StreamState.HALF_CLOSED_REMOTE, (
            f'Expected HALF_CLOSED_REMOTE after HEADERS+END_STREAM; got {stream.state}'
        )

    async def test_data_end_stream_closes_stream(self):
        """DATA + END_STREAM on an open stream must transition to CLOSED."""
        StreamState = self._import_state()
        from blackbull.stream import Stream
        stream = Stream(identifier=1, parent=None)
        stream.on_headers_received(end_stream=False)
        stream.on_data_received(end_stream=True)
        assert stream.state == StreamState.CLOSED, (
            f'Expected CLOSED after DATA+END_STREAM; got {stream.state}'
        )

    async def test_data_on_closed_stream_triggers_rst_stream(self):
        """Receiving DATA on a closed stream must cause RST_STREAM(STREAM_CLOSED).

        RFC 7540 §6.1: if a DATA frame is received whose stream is in the
        'closed' state, the endpoint MUST respond with a stream error of type
        STREAM_CLOSED.
        """
        from blackbull.frame import ErrorCodes
        # Send HEADERS+END_STREAM (closes remote side), then another DATA
        h_frame = _make_headers_frame(stream_id=1, end_stream=True)
        d_frame = _make_h2_frame(FrameTypes.DATA, DataFrameFlags.END_STREAM, 1, b'late')

        handler, _ = _make_h2_handler()
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
# P3 — HTTP/2 priority scheduling
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestHTTP2Priority:
    """HTTP2Handler must honour PRIORITY frames (RFC 7540 §5.3, §6.3).

    A PRIORITY frame sets the stream's dependency and weight.  When multiple
    streams are ready, the handler must allocate resources proportional to each
    stream's weight.  Higher-weight streams must be processed before lower-weight
    ones when both are ready simultaneously.
    """

    @staticmethod
    def _priority_frame(stream_id: int, depends_on: int = 0,
                        weight: int = 16) -> bytes:
        """Build a raw PRIORITY frame (type=0x02, RFC 7540 §6.3)."""
        dep = depends_on.to_bytes(4, 'big')
        payload = dep + bytes([weight - 1])   # wire weight = weight - 1
        return _make_h2_frame(FrameTypes.PRIORITY, 0, stream_id, payload)

    async def test_priority_frame_accepted_without_connection_error(self):
        """A PRIORITY frame must not produce a GOAWAY or exception."""
        priority = self._priority_frame(stream_id=3, weight=32)

        handler, _ = _make_h2_handler()
        handler.receive = AsyncMock(side_effect=[priority, None])
        await handler.run()   # must not raise

        goaway_calls = [
            call for call in handler.send_frame.call_args_list
            if hasattr(call.args[0], 'FrameType')
            and call.args[0].FrameType() == FrameTypes.GOAWAY
        ]
        assert not goaway_calls, (
            f'PRIORITY frame must not trigger GOAWAY; got {goaway_calls}'
        )

    async def test_stream_weight_stored_after_priority_frame(self):
        """A PRIORITY frame must update the stream's weight in the tree."""
        priority = self._priority_frame(stream_id=3, weight=32)

        handler, _ = _make_h2_handler()
        handler.receive = AsyncMock(side_effect=[priority, None])
        await handler.run()

        stream = handler.root_stream.find_child(3)
        assert stream is not None, 'Stream 3 must exist after PRIORITY frame'
        assert stream.weight == 32, (
            f'Expected weight=32 after PRIORITY; got {stream.weight}'
        )

    async def test_higher_weight_stream_served_before_lower(self):
        """Given two concurrent streams, the higher-weight one must be
        dispatched to the app first (RFC 7540 §5.3.2).

        Stream 3 gets weight=32; stream 1 uses the default weight=16.
        Both HEADERS arrive simultaneously; stream 3 must be called first.
        """
        priority = self._priority_frame(stream_id=3, weight=32)
        h1 = _make_headers_frame(stream_id=1, end_stream=True)
        h3 = _make_headers_frame(stream_id=3, end_stream=True)

        call_order = []

        async def app(scope, receive, send):
            # Record which stream_id reached the app and when
            call_order.append(scope.get('stream_id') or scope.get('path'))

        handler, _ = _make_h2_handler(app=app)
        handler.receive = AsyncMock(side_effect=[priority, h1, h3, None])
        await handler.run()

        assert len(call_order) == 2, f'Expected 2 app calls; got {call_order}'
        assert call_order[0] != call_order[1], 'Both streams must reach the app'
        # Stream 3 (weight 32) should precede stream 1 (weight 16)
        idx_stream3 = next(
            (i for i, v in enumerate(call_order) if v in (3, '/') ), None
        )
        assert idx_stream3 == 0, (
            f'Higher-weight stream 3 must be served first; call order: {call_order}'
        )
