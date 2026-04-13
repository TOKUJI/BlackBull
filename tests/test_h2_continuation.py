"""
Tests for HTTP/2 CONTINUATION frame support (P1 item 6)
========================================================

RFC 7540 §6.10 — CONTINUATION
------------------------------
A CONTINUATION frame is used to continue a sequence of header block fragments
(§4.3).  Any number of CONTINUATION frames can be sent, as long as the
preceding frame is on the same stream and is a HEADERS, PUSH_PROMISE, or
CONTINUATION frame without the END_HEADERS flag set.

The server must:

1. Accept CONTINUATION frames as a valid frame type (currently causes
   ``KeyError`` in ``FrameFactory`` because no ``Continuation`` class is
   registered).

2. When a HEADERS frame arrives *without* END_HEADERS (flag 0x4 absent),
   keep reading subsequent CONTINUATION frames on the same stream.

3. Concatenate the ``data`` payloads of all fragments before passing the
   combined header block to the HPACK decoder.

4. Call the ASGI application only after the full header block has been
   assembled (END_HEADERS seen on a CONTINUATION frame).

P1 bug
------
``FrameFactory._factory`` is populated from ``FrameBase.__subclasses__()`` at
construction time.  Because no ``Continuation`` class exists, ``factory.load()``
raises ``KeyError`` for type ``0x09``.  Even if the class existed,
``HTTP2Handler.run()`` has no accumulation logic and would call the ASGI app
prematurely — after only the HEADERS fragment — with an incomplete scope.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock

from hpack import Encoder

from blackbull.frame import (FrameFactory, FrameTypes, FrameFlags,
                              HeaderFrameFlags, SettingFrameFlags)
from blackbull.server.server import HTTP2Handler


# ---------------------------------------------------------------------------
# Wire-format helper
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


# ---------------------------------------------------------------------------
# HEADERS frame flag parsing
# ---------------------------------------------------------------------------

class TestHeadersFrameFlags:
    """HEADERS frame must correctly expose the END_HEADERS flag."""

    def test_headers_with_end_headers_flag_reports_nonzero(self):
        """HEADERS frame with END_HEADERS (0x04) must have ``end_headers != 0``."""
        factory = FrameFactory()
        raw = _make_h2_frame(FrameTypes.HEADERS, HeaderFrameFlags.END_HEADERS, 1, b'')
        frame = factory.load(raw)
        assert frame.end_headers != 0

    def test_headers_without_end_headers_flag_reports_zero(self):
        """HEADERS frame without END_HEADERS must have ``end_headers == 0``."""
        factory = FrameFactory()
        raw = _make_h2_frame(FrameTypes.HEADERS, SettingFrameFlags.INIT, 1, b'')
        frame = factory.load(raw)
        assert frame.end_headers == 0

    def test_end_stream_flag_independent_of_end_headers(self):
        """END_STREAM (0x01) and END_HEADERS (0x04) are independent bits."""
        factory = FrameFactory()
        # Both flags set
        raw = _make_h2_frame(FrameTypes.HEADERS,
                             HeaderFrameFlags.END_STREAM | HeaderFrameFlags.END_HEADERS, 1, b'')
        frame = factory.load(raw)
        assert frame.end_stream != 0
        assert frame.end_headers != 0

    def test_end_stream_set_without_end_headers(self):
        """END_STREAM without END_HEADERS is legal (HEADERS without all headers yet)."""
        factory = FrameFactory()
        raw = _make_h2_frame(FrameTypes.HEADERS, HeaderFrameFlags.END_STREAM, 1, b'')
        frame = factory.load(raw)
        assert frame.end_stream != 0
        assert frame.end_headers == 0


# ---------------------------------------------------------------------------
# CONTINUATION frame parsing (FrameFactory level)
# ---------------------------------------------------------------------------

class TestContinuationFrameParsing:
    """``FrameFactory`` must recognise and parse CONTINUATION frames.

    P1 bug: ``FrameFactory._factory`` is built from ``FrameBase.__subclasses__()``.
    No ``Continuation`` subclass exists, so ``factory.load()`` raises ``KeyError``
    for any frame whose type byte is ``0x09``.
    """

    def test_continuation_type_is_defined_in_enum(self):
        """``FrameTypes.CONTINUATION`` must exist and equal ``b'\\x09'``."""
        assert FrameTypes.CONTINUATION == b'\x09'

    def test_factory_load_continuation_without_end_headers_does_not_raise(self):
        """``FrameFactory.load()`` must parse a CONTINUATION frame (no END_HEADERS).

        Currently raises ``KeyError`` because no handler class is registered for type 0x09.
        """
        factory = FrameFactory()
        raw = _make_h2_frame(FrameTypes.CONTINUATION, SettingFrameFlags.INIT, 1, b'\x00')
        frame = factory.load(raw)              # P1 bug: KeyError here
        assert frame.FrameType() == FrameTypes.CONTINUATION

    def test_factory_load_continuation_with_end_headers_does_not_raise(self):
        """``FrameFactory.load()`` must parse a CONTINUATION frame with END_HEADERS."""
        factory = FrameFactory()
        raw = _make_h2_frame(FrameTypes.CONTINUATION, HeaderFrameFlags.END_HEADERS, 1, b'\x00')
        frame = factory.load(raw)              # P1 bug: KeyError here
        assert frame.FrameType() == FrameTypes.CONTINUATION

    def test_continuation_frame_exposes_end_headers_flag(self):
        """A parsed CONTINUATION frame must expose its END_HEADERS flag."""
        factory = FrameFactory()
        raw = _make_h2_frame(FrameTypes.CONTINUATION, HeaderFrameFlags.END_HEADERS, 1, b'')
        frame = factory.load(raw)
        assert frame.end_headers != 0

    def test_intermediate_continuation_frame_has_end_headers_zero(self):
        """Intermediate CONTINUATION frame (no END_HEADERS) must have ``end_headers == 0``."""
        factory = FrameFactory()
        raw = _make_h2_frame(FrameTypes.CONTINUATION, SettingFrameFlags.INIT, 1, b'')
        frame = factory.load(raw)
        assert frame.end_headers == 0

    def test_continuation_frame_carries_stream_id(self):
        """CONTINUATION frame must be associated with the same stream as HEADERS."""
        factory = FrameFactory()
        raw = _make_h2_frame(FrameTypes.CONTINUATION, HeaderFrameFlags.END_HEADERS, 3, b'')
        frame = factory.load(raw)
        assert frame.stream_id == 3


# ---------------------------------------------------------------------------
# HTTP2Handler.run() — accumulation across HEADERS + CONTINUATION frames
# ---------------------------------------------------------------------------

def _make_handler(app):
    """Create an ``HTTP2Handler`` with a fake writer and a mocked ``send_frame``."""
    writer = MagicMock()
    writer.drain = AsyncMock()
    handler = HTTP2Handler(app, reader=None, writer=writer)
    # Skip the SETTINGS frame write that happens at the start of run()
    handler.send_frame = AsyncMock()
    return handler


@pytest.mark.asyncio
class TestContinuationHandling:
    """``HTTP2Handler.run()`` must accumulate header block fragments until
    END_HEADERS and call the ASGI app once with the fully assembled scope.

    These tests document the desired behaviour; they currently fail because
    the accumulation logic does not exist.
    """

    async def test_headers_with_end_headers_calls_app_once(self):
        """Normal HEADERS frame (END_HEADERS set) must call the app — regression guard."""
        encoder = Encoder()
        block = encoder.encode([
            (b':method', b'GET'),
            (b':path', b'/'),
            (b':scheme', b'https'),
        ])
        h_frame = _make_h2_frame(FrameTypes.HEADERS,
                                 HeaderFrameFlags.END_HEADERS | HeaderFrameFlags.END_STREAM, 1, block)

        app = AsyncMock()
        handler = _make_handler(app)
        handler.receive = AsyncMock(side_effect=[h_frame, None])  # None stops the loop

        await handler.run()

        assert app.call_count == 1

    async def test_headers_plus_continuation_calls_app_once_with_full_scope(self):
        """HEADERS (no END_HEADERS) + CONTINUATION (END_HEADERS) must yield one app call
        with the fully decoded scope.

        P1 bug: ``factory.load()`` raises ``KeyError`` on the CONTINUATION frame;
        even if it didn't, ``run()`` would have called the app early (after HEADERS only)
        with a scope built from only the first HPACK fragment.
        """
        encoder = Encoder()
        full_block = encoder.encode([
            (b':method', b'GET'),
            (b':path', b'/api/resource'),
            (b':scheme', b'https'),
        ])
        # Split at an arbitrary byte boundary — the concatenated block is still valid HPACK
        split = len(full_block) // 2 + 1
        part1, part2 = full_block[:split], full_block[split:]

        h_frame = _make_h2_frame(FrameTypes.HEADERS, SettingFrameFlags.INIT, 1, part1)
        c_frame = _make_h2_frame(FrameTypes.CONTINUATION, HeaderFrameFlags.END_HEADERS, 1, part2)

        app = AsyncMock()
        handler = _make_handler(app)
        handler.receive = AsyncMock(side_effect=[h_frame, c_frame, None])

        await handler.run()

        assert app.call_count == 1
        scope = app.call_args[0][0]
        assert scope['method'] == 'GET'
        assert scope['path'] == '/api/resource'

    async def test_app_not_called_after_headers_only(self):
        """The app must NOT be called after the HEADERS-only frame; it must wait for
        the CONTINUATION frame with END_HEADERS.

        Verifies that ``run()`` does not prematurely invoke the ASGI application.
        """
        encoder = Encoder()
        full_block = encoder.encode([
            (b':method', b'POST'),
            (b':path', b'/submit'),
            (b':scheme', b'https'),
        ])
        split = len(full_block) // 2 + 1
        part1, part2 = full_block[:split], full_block[split:]

        h_frame = _make_h2_frame(FrameTypes.HEADERS, SettingFrameFlags.INIT, 1, part1)
        c_frame = _make_h2_frame(FrameTypes.CONTINUATION, HeaderFrameFlags.END_HEADERS, 1, part2)

        app = AsyncMock()
        handler = _make_handler(app)

        # Intercept receive() to check call_count at the right moment
        frames = [h_frame, c_frame, None]
        call_count_after_headers = None

        original_se = iter(frames)
        call_index = 0

        async def tracked_receive():
            nonlocal call_index, call_count_after_headers
            data = next(original_se)
            call_index += 1
            if call_index == 1:
                # Just returned the HEADERS frame — app must not have been called yet
                call_count_after_headers = app.call_count
            return data

        handler.receive = tracked_receive
        await handler.run()

        assert call_count_after_headers == 0   # app not called after HEADERS-only
        assert app.call_count == 1             # called exactly once, after CONTINUATION

    async def test_multiple_continuation_frames_produce_correct_scope(self):
        """HEADERS + two CONTINUATION frames (only the last has END_HEADERS) must
        concatenate all three fragments before decoding.
        """
        encoder = Encoder()
        full_block = encoder.encode([
            (b':method', b'PUT'),
            (b':path', b'/data'),
            (b':scheme', b'https'),
        ])
        n = len(full_block)
        # Three roughly equal parts
        p1 = full_block[:n // 3]
        p2 = full_block[n // 3: 2 * n // 3]
        p3 = full_block[2 * n // 3:]

        h_frame  = _make_h2_frame(FrameTypes.HEADERS,       SettingFrameFlags.INIT,          1, p1)
        c1_frame = _make_h2_frame(FrameTypes.CONTINUATION, SettingFrameFlags.INIT,          1, p2)
        c2_frame = _make_h2_frame(FrameTypes.CONTINUATION, HeaderFrameFlags.END_HEADERS,    1, p3)

        app = AsyncMock()
        handler = _make_handler(app)
        handler.receive = AsyncMock(side_effect=[h_frame, c1_frame, c2_frame, None])

        await handler.run()

        assert app.call_count == 1
        scope = app.call_args[0][0]
        assert scope['method'] == 'PUT'
        assert scope['path'] == '/data'

    async def test_two_independent_requests_each_with_continuation(self):
        """Two separate streams, each split over HEADERS+CONTINUATION, must yield
        two separate app calls with the correct scopes for each request.
        """
        encoder = Encoder()

        block1 = encoder.encode([
            (b':method', b'GET'),
            (b':path', b'/first'),
            (b':scheme', b'https'),
        ])
        block2 = encoder.encode([
            (b':method', b'POST'),
            (b':path', b'/second'),
            (b':scheme', b'https'),
        ])

        mid1 = len(block1) // 2 + 1
        mid2 = len(block2) // 2 + 1

        # Stream 1 frames
        h1 = _make_h2_frame(FrameTypes.HEADERS,       SettingFrameFlags.INIT,       1, block1[:mid1])
        c1 = _make_h2_frame(FrameTypes.CONTINUATION,  HeaderFrameFlags.END_HEADERS, 1, block1[mid1:])
        # Stream 3 frames (HTTP/2 client streams are odd-numbered)
        h3 = _make_h2_frame(FrameTypes.HEADERS,       SettingFrameFlags.INIT,       3, block2[:mid2])
        c3 = _make_h2_frame(FrameTypes.CONTINUATION,  HeaderFrameFlags.END_HEADERS, 3, block2[mid2:])

        app = AsyncMock()
        handler = _make_handler(app)
        handler.receive = AsyncMock(side_effect=[h1, c1, h3, c3, None])

        await handler.run()

        assert app.call_count == 2
        scopes = [call[0][0] for call in app.call_args_list]
        paths = {s['path'] for s in scopes}
        assert paths == {'/first', '/second'}
