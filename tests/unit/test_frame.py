"""
Tests for frame parsing and HTTP/2 frame dispatch (blackbull/frame.py,
blackbull/server/server.py, blackbull/server/response.py)

P1 item 7 — Header name normalization (RFC 7540 §8.1.2)
---------------------------------------------------------
HTTP/2 header names MUST be lowercase.  A non-conformant client may send
mixed-case header names.  ``Headers.parse_payload()`` must normalize every
name to lowercase after HPACK decode.  Header *values* must be preserved
as-is.

P1 — DATA frame regression
---------------------------
``HTTP2Handler.run()`` lost its ``case FrameTypes.DATA:`` branch during the
CONTINUATION refactoring.  DATA frames now fall through to ``case _:`` and
then into ``ResponderFactory.create()``, which raises ``KeyError`` because there
is no ``DataResponder`` handler.

P1 — GOAWAY frame
-----------------
``ResponderFactory`` has no ``GoAwayResponder`` handler.  Receiving a client
GOAWAY (e.g. during connection teardown) raises ``KeyError``.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock
from hpack import Encoder

from blackbull.protocol.frame import FrameFactory
from blackbull.protocol.frame_types import (FrameTypes, FrameFlags,
                                            HeaderFrameFlags, DataFrameFlags,
                                            SettingFrameFlags, Headers, PseudoHeaders)
from blackbull.server.http2_actor import HTTP2Actor
from blackbull.server.sender import AsyncioWriter


# ---------------------------------------------------------------------------
# Wire-format helper (mirrors test_h2_continuation.py)
# ---------------------------------------------------------------------------

def _make_h2_frame(type_byte: FrameTypes, flags: FrameFlags | int,
                   stream_id: int, payload: bytes) -> bytes:
    """Build a raw 9-byte HTTP/2 frame header followed by *payload*."""
    length = len(payload)
    return (length.to_bytes(3, 'big')
            + type_byte
            + bytes([flags])
            + stream_id.to_bytes(4, 'big')
            + payload)


def _make_handler(app):
    """Create an ``HTTP2Actor`` with a fake writer and mocked ``send_frame``."""
    writer = MagicMock()
    writer.drain = AsyncMock()
    handler = HTTP2Actor(None, AsyncioWriter(writer), app, aggregator=None)
    handler.send_frame = AsyncMock()
    return handler


# ---------------------------------------------------------------------------
# P1 item 7 — Header name normalization (RFC 7540 §8.1.2)
# ---------------------------------------------------------------------------

class TestHeaderNameNormalization:
    """``Headers.parse_payload()`` must lowercase all header names.

    RFC 7540 §8.1.2:
        All header field names MUST be converted to lowercase prior to their
        encoding in HTTP/2.

    P1 bug: ``parse_payload()`` calls ``k.decode()`` which preserves whatever
    casing the HPACK decoder returns.  A non-conformant peer may send
    mixed-case names; they must be stored as lowercase regardless.
    """

    def _frame_with_raw_block(self, raw_block: bytes) -> Headers:
        """Build a Headers frame whose raw_block is set directly (bypasses HPACK encode)."""
        factory = FrameFactory()
        # Create an empty HEADERS frame (length=0, END_HEADERS set) then override raw_block
        raw = _make_h2_frame(FrameTypes.HEADERS, HeaderFrameFlags.END_HEADERS, 1, b'')
        frame = factory.load(raw)
        # Now inject the raw HPACK bytes and re-parse
        frame.raw_block = raw_block
        frame.pseudo_headers.clear()
        frame.headers.clear()
        frame.parse_payload()
        return frame

    def test_lowercase_header_name_stored_as_is(self):
        """Normally-lowercase header name must be preserved."""
        factory = FrameFactory()
        encoder = Encoder()
        block = encoder.encode([(b'content-type', b'text/plain'),
                                 (b':method', b'GET'),
                                 (b':path', b'/'),
                                 (b':scheme', b'https')])
        raw = _make_h2_frame(FrameTypes.HEADERS, HeaderFrameFlags.END_HEADERS, 1, block)
        frame = factory.load(raw)
        names = [k for k, _ in frame.headers]
        assert b'content-type' in names

    def test_mixed_case_header_name_is_malformed(self):
        """Mixed-case header field name must be flagged malformed.

        RFC 9113 §8.2.1: header field names MUST be lowercase; a message
        with an uppercase name MUST be treated as malformed (PROTOCOL_ERROR
        at the actor level).
        """
        raw_block = (bytes([0x00, 0x0c]) + b'Content-Type'
                     + bytes([0x0a]) + b'text/plain')
        frame = self._frame_with_raw_block(raw_block)
        assert frame.malformed
        assert 'Content-Type' in frame.malformed_reason

    def test_all_uppercase_header_name_is_malformed(self):
        """All-uppercase header field name must be flagged malformed."""
        raw_block = (bytes([0x00, 0x04]) + b'HOST'
                     + bytes([0x09]) + b'localhost')
        frame = self._frame_with_raw_block(raw_block)
        assert frame.malformed
        assert 'HOST' in frame.malformed_reason

    def test_pseudo_header_with_wrong_case_is_malformed(self):
        """:Method (mixed case) must be flagged malformed.

        Pseudo-header names share the lowercase requirement.  Receiving
        ``:Method`` is a PROTOCOL_ERROR, not silently normalized.
        """
        raw_block = (bytes([0x00, 0x07]) + b':Method'
                     + bytes([0x03]) + b'GET')
        frame = self._frame_with_raw_block(raw_block)
        assert frame.malformed
        assert ':Method' in frame.malformed_reason

    def test_header_value_is_not_lowercased(self):
        """Header *values* must be preserved exactly — only names are restricted.

        Name is lowercase ``content-type`` so the frame passes validation;
        the value contains mixed case and punctuation that must be preserved.
        """
        raw_block = (bytes([0x00, 0x0c]) + b'content-type'
                     + bytes([0x11]) + b'Text/Plain; q=1.0')
        frame = self._frame_with_raw_block(raw_block)
        assert not frame.malformed
        values = [v for _, v in frame.headers]
        assert b'Text/Plain; q=1.0' in values


# ---------------------------------------------------------------------------
# P1 — DATA frame regression
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestDataFrameHandling:
    """``HTTP2Handler.run()`` must handle DATA frames without raising.

    P1 regression: the ``case FrameTypes.DATA:`` branch was removed during
    the CONTINUATION refactoring.  DATA frames now fall to ``case _:`` and
    then ``ResponderFactory.create(frame)`` raises ``KeyError`` because there
    is no ``DataResponder`` class.
    """

    def _make_headers_frame(self, stream_id: int = 1, end_stream: bool = False) -> bytes:
        encoder = Encoder()
        block = encoder.encode([(b':method', b'POST'),
                                 (b':path', b'/upload'),
                                 (b':scheme', b'https')])
        flags: FrameFlags = HeaderFrameFlags.END_HEADERS
        if end_stream:
            flags |= HeaderFrameFlags.END_STREAM
        return _make_h2_frame(FrameTypes.HEADERS, flags, stream_id, block)

    def _make_data_frame(self, payload: bytes, stream_id: int = 1,
                         end_stream: bool = True) -> bytes:
        flags = DataFrameFlags.END_STREAM if end_stream else SettingFrameFlags.INIT
        return _make_h2_frame(FrameTypes.DATA, flags, stream_id, payload)

    async def test_data_frame_does_not_raise(self):
        """A DATA frame arriving after HEADERS must not raise ``KeyError``."""
        h_frame = self._make_headers_frame(end_stream=False)
        d_frame = self._make_data_frame(b'hello', end_stream=True)

        app = AsyncMock()
        handler = _make_handler(app)
        handler.receive = AsyncMock(side_effect=[h_frame, d_frame, None])

        await handler.run()  # must not raise

    async def test_data_frame_with_end_stream_calls_app(self):
        """HEADERS then DATA (END_STREAM) must result in the ASGI app being called."""
        h_frame = self._make_headers_frame(end_stream=False)
        d_frame = self._make_data_frame(b'body', end_stream=True)

        app = AsyncMock()
        handler = _make_handler(app)
        handler.receive = AsyncMock(side_effect=[h_frame, d_frame, None])

        await handler.run()

        assert app.call_count == 1

    async def test_data_frame_without_end_stream_app_called_once(self):
        """HEADERS + two DATA frames must result in exactly one app call.

        With the concurrent design the app is launched as a task right after
        END_HEADERS (not after END_STREAM), so call_count is 1 once the server
        loop finishes — regardless of how many DATA frames arrived.
        """
        h_frame = self._make_headers_frame(end_stream=False)
        d_frame = self._make_data_frame(b'chunk1', end_stream=False)
        d_final = self._make_data_frame(b'chunk2', end_stream=True)

        app = AsyncMock()
        handler = _make_handler(app)
        handler.receive = AsyncMock(side_effect=[h_frame, d_frame, d_final, None])

        await handler.run()

        assert app.call_count == 1

    async def test_headers_with_end_stream_calls_app_without_data(self):
        """HEADERS+END_STREAM (no body) must call the app directly — regression guard."""
        h_frame = self._make_headers_frame(end_stream=True)

        app = AsyncMock()
        handler = _make_handler(app)
        handler.receive = AsyncMock(side_effect=[h_frame, None])

        await handler.run()

        assert app.call_count == 1


# ---------------------------------------------------------------------------
# P1 — GOAWAY frame
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestGoAwayHandling:
    """``HTTP2Handler.run()`` must handle GOAWAY frames without raising.

    P1 bug: ``ResponderFactory`` has no ``GoAwayResponder`` handler, so receiving
    a GOAWAY from the client causes ``ResponderFactory.create(frame)`` to raise
    ``KeyError``.
    """

    def _make_goaway_frame(self, last_stream_id: int = 0,
                           error_code: int = 0x0) -> bytes:
        """Build a GOAWAY frame (type 0x07, always on stream 0).

        Payload: 4-byte last_stream_id + 4-byte error_code.
        """
        payload = last_stream_id.to_bytes(4, 'big') + error_code.to_bytes(4, 'big')
        return _make_h2_frame(FrameTypes.GOAWAY, SettingFrameFlags.INIT, 0, payload)

    async def test_goaway_does_not_raise(self):
        """A GOAWAY frame must not raise ``KeyError``."""
        goaway = self._make_goaway_frame(last_stream_id=0, error_code=0x0)

        app = AsyncMock()
        handler = _make_handler(app)
        handler.receive = AsyncMock(side_effect=[goaway, None])

        await handler.run()  # must not raise

    async def test_goaway_no_error_does_not_call_app(self):
        """GOAWAY with NO_ERROR (0x0) is a graceful shutdown — app must not be called."""
        goaway = self._make_goaway_frame(last_stream_id=0, error_code=0x0)

        app = AsyncMock()
        handler = _make_handler(app)
        handler.receive = AsyncMock(side_effect=[goaway, None])

        await handler.run()

        assert app.call_count == 0

    async def test_goaway_stops_processing_further_frames(self):
        """After GOAWAY, no further frames should be dispatched to the app."""
        encoder = Encoder()
        block = encoder.encode([(b':method', b'GET'), (b':path', b'/'),
                                 (b':scheme', b'https')])
        h_frame = _make_h2_frame(FrameTypes.HEADERS,
                                 HeaderFrameFlags.END_HEADERS | HeaderFrameFlags.END_STREAM, 1, block)
        goaway = self._make_goaway_frame(last_stream_id=0, error_code=0x0)

        app = AsyncMock()
        handler = _make_handler(app)
        # GOAWAY arrives before a HEADERS request
        handler.receive = AsyncMock(side_effect=[goaway, h_frame, None])

        await handler.run()

        # App must not be called after GOAWAY terminated the connection
        assert app.call_count == 0


# ---------------------------------------------------------------------------
# Legacy commented-out tests (kept for reference)
# ---------------------------------------------------------------------------

# def test_settingframe():
#     type_ = b'\x04'
#     flags = 0
#     sid = 0
#     payload = b''
#     length = len(payload).to_bytes(3, byteorder='big')
#
#     SettingFrame(length, type_, flags, sid, data=payload)
#     assert 0 == 1


# def test_priorityframe():
#     type_ = b'\x02'
#     flags = 0
#     sid = 0
#
#     exclusive = 0x80000000
#     dependencies = (1).to_bytes(4, byteorder='big')
#     weight = (100).to_bytes(1, byteorder='big')
#     payload = dependencies + weight
#
#     length = len(payload).to_bytes(3, byteorder='big')
#
#     Priority(length, type_, flags, sid, data=payload)
#
#     assert 0 == 1


# ---------------------------------------------------------------------------
# Frame serialization — save() must include the payload bytes
# ---------------------------------------------------------------------------

class TestFrameSavePayload:
    """Frame.save() must include the payload bytes, not just the 9-byte header.

    Bug: WindowUpdate.save(), RstStream.save(), and GoAway.save() all called
    super().save() but returned without appending the actual payload bytes.
    The receiver read past the header expecting `length` more bytes, got the
    beginning of the next frame instead, and the entire HTTP/2 stream parser
    desynced.
    """

    def test_window_update_save_includes_increment(self):
        """WindowUpdate.save() must produce a 13-byte frame (9 header + 4 payload)."""
        from blackbull.protocol.frame_types import ErrorCodes
        frame = FrameFactory().window_update(stream_id=3, increment=35)
        wire = frame.save()
        assert len(wire) == 13, (
            f'WindowUpdate wire frame must be 13 bytes (9-byte header + '
            f'4-byte increment); got {len(wire)} bytes: {wire.hex()}'
        )
        assert wire[:3] == b'\x00\x00\x04', (
            f'Length field must be 4; got {wire[:3].hex()}'
        )
        assert int.from_bytes(wire[9:13], 'big') == 35, (
            f'Increment in wire payload must be 35; '
            f'got {int.from_bytes(wire[9:13], "big")}'
        )

    def test_rst_stream_save_includes_error_code(self):
        """RstStream.save() must produce a 13-byte frame (9 header + 4 error code)."""
        from blackbull.protocol.frame_types import ErrorCodes
        frame = FrameFactory().rst_stream(stream_id=3,
                                          error_code=ErrorCodes.REFUSED_STREAM)
        wire = frame.save()
        assert len(wire) == 13, (
            f'RstStream wire frame must be 13 bytes; got {len(wire)}: {wire.hex()}'
        )
        assert int.from_bytes(wire[9:13], 'big') == int(ErrorCodes.REFUSED_STREAM), (
            f'Error code in wire payload must be REFUSED_STREAM '
            f'({int(ErrorCodes.REFUSED_STREAM)}); '
            f'got {int.from_bytes(wire[9:13], "big")}'
        )

    def test_goaway_save_includes_last_stream_id_and_error_code(self):
        """GoAway.save() must produce a 17-byte frame (9 header + 8 payload)."""
        frame = FrameFactory().goaway(last_stream_id=3, error_code=0)
        wire = frame.save()
        assert len(wire) == 17, (
            f'GoAway wire frame must be 17 bytes (9 header + 4 last-stream-id '
            f'+ 4 error-code); got {len(wire)}: {wire.hex()}'
        )
        assert int.from_bytes(wire[9:13], 'big') == 3, (
            f'Last-stream-id in GoAway payload must be 3; '
            f'got {int.from_bytes(wire[9:13], "big")}'
        )
        assert int.from_bytes(wire[13:17], 'big') == 0, (
            f'Error code in GoAway payload must be 0; '
            f'got {int.from_bytes(wire[13:17], "big")}'
        )
