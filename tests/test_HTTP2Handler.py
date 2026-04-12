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
from blackbull.frame import FrameTypes, SettingFlags


# ---------------------------------------------------------------------------
# Wire-format helpers
# ---------------------------------------------------------------------------

def _make_h2_frame(type_byte: bytes, flags: int, stream_id: int,
                   payload: bytes) -> bytes:
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
    flags = 0x04  # END_HEADERS
    if end_stream:
        flags |= 0x01
    return _make_h2_frame(b'\x01', flags, stream_id, block)


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
        return _make_h2_frame(b'\x08', 0x00, stream_id, payload)

    async def test_window_update_received_is_tracked(self):
        """A WINDOW_UPDATE from the client must increase the tracked window size.

        The handler must store the updated window and allow DATA up to that limit.
        """
        wu_frame = self._make_window_update_frame(65535, stream_id=0)
        settings = _make_h2_frame(b'\x04', 0x00, 0, b'')  # empty SETTINGS

        handler, app = _make_h2_handler()
        handler.receive = AsyncMock(side_effect=[settings, wu_frame, None])

        await handler.run()

        assert handler.client_window_size >= 65535, (
            f'Expected window_size >= 65535 after WINDOW_UPDATE, '
            f'got {handler.client_window_size}'
        )

    async def test_data_frame_triggers_window_update_to_client(self):
        """After consuming received DATA, handler must issue WINDOW_UPDATE.

        RFC 7540 §6.9: a receiver must send WINDOW_UPDATE after consuming data
        to prevent the sender's window from permanently shrinking.
        """
        h_frame = _make_headers_frame(stream_id=1, end_stream=False)
        d_frame = _make_h2_frame(b'\x00', 0x01, 1, b'hello')  # DATA + END_STREAM

        handler, app = _make_h2_handler()
        handler.receive = AsyncMock(side_effect=[h_frame, d_frame, None])
        await handler.run()

        sent_types = [call.args[0].FrameType()
                      for call in handler.send_frame.call_args_list
                      if hasattr(call.args[0], 'FrameType')]

        assert FrameTypes.WINDOW_UPDATE in sent_types, (
            'handler must send WINDOW_UPDATE after consuming received DATA. '
            f'Frames sent: {sent_types}'
        )

    async def test_zero_window_blocks_app_data_send(self):
        """When the peer window is 0, the app's DATA response must be held.

        Checks that no DATA payload appears on the wire while the window is zero.
        """
        h_frame = _make_headers_frame(stream_id=1, end_stream=True)
        wu_frame = self._make_window_update_frame(65535, stream_id=0)

        writer = MagicMock()
        writer.drain = AsyncMock()
        writer.close = MagicMock()
        writer.written = bytearray()
        writer.write = MagicMock(side_effect=lambda d: writer.written.extend(d))

        data_written_before_wu = None

        async def app(scope, receive, send):
            nonlocal data_written_before_wu
            data_written_before_wu = bytes(writer.written)
            await send({'type': 'http.response.start', 'status': 200, 'headers': []})
            await send({'type': 'http.response.body', 'body': b'x' * 1000})

        handler = HTTP2Handler(app, reader=None, writer=writer)
        handler.send_frame = AsyncMock()
        handler.client_window_size = 0  # force window to zero

        handler.receive = AsyncMock(side_effect=[h_frame, wu_frame, None])
        await handler.run()

        assert b'x' * 10 not in data_written_before_wu, (
            'DATA was sent to wire while window was 0 — flow control not enforced.'
        )


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
        return _make_h2_frame(b'\x07', 0x00, 0, payload)

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
        continuation = _make_h2_frame(b'\x09', 0x04, 1, b'\x00' * 4)

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
