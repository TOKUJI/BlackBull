"""HTTP2Sender consuming the native response message (Sprint 93).

Pins the wire-equivalence invariant for H2 (the Sprint 92 H1 invariant,
extended): a NativeResponse-driven response produces byte-identical frames to
the ASGI-dict path, and the unified object's presence semantics (``is not
None``) drive the sender with no per-protocol consumer detection.  Also pins
the H2-specific END_STREAM rules: trailers after a terminal body are dropped
(the same post-terminal drop the dict lane's entry guard performs), and a
single object may legitimately carry header + non-terminal body + trailers
(HEADERS + DATA + trailing HEADERS coalesce into one write).
"""
import pytest
from unittest.mock import AsyncMock, MagicMock

from blackbull.native import NativeResponse
from blackbull.protocol.frame import FrameFactory
from blackbull.protocol.frame_types import FrameTypes
from blackbull.server.sender import HTTP2Sender, AsyncioWriter
from blackbull.asgi import ASGIEvent

pytestmark = pytest.mark.asyncio


def _make_sender(max_frame_size: int | None = None):
    written = bytearray()
    mock_writer = MagicMock()
    mock_writer.write = MagicMock(side_effect=lambda d: written.extend(d))
    mock_writer.drain = AsyncMock()
    factory = FrameFactory()
    sender = HTTP2Sender(AsyncioWriter(mock_writer), factory, stream_id=1)
    if max_frame_size is not None:
        sender.max_frame_size = max_frame_size
    return sender, written, factory, mock_writer


def _decode_headers(written: bytes, factory: FrameFactory) -> list[tuple[bytes, bytes]]:
    """Decode the leading HEADERS frame (HPACK) from ``written``."""
    length = int.from_bytes(written[0:3], 'big')
    assert written[3:4] == FrameTypes.HEADERS.value
    return factory.decoder.decode(written[9:9 + length])


def _collect_frames(written: bytearray, factory: FrameFactory) -> list:
    """Parse every frame (HEADERS + DATA) from the accumulated wire bytes."""
    frames = []
    mv = memoryview(written)
    offset = 0
    while offset + 9 <= len(mv):
        length = int.from_bytes(mv[offset:offset + 3], 'big')
        if offset + 9 + length > len(mv):
            break
        frames.append(factory.load(bytes(mv[offset:offset + 9 + length])))
        offset += 9 + length
    return frames


# ---------------------------------------------------------------------------
# Wire equivalence: NativeResponse path == dict path (H2)
# ---------------------------------------------------------------------------

class TestWireEquivalence:
    async def test_complete_response_matches_dict_path(self):
        s1, w1, f1, _ = _make_sender()
        await s1({'type': ASGIEvent.HTTP_RESPONSE_START, 'status': 200,
                  'headers': [(b'content-type', b'text/plain')]})
        await s1({'type': ASGIEvent.HTTP_RESPONSE_BODY, 'body': b'Hello',
                  'more_body': False})

        s2, w2, f2, _ = _make_sender()
        await s2(NativeResponse(status=200,
                                header=[(b'content-type', b'text/plain')],
                                body=b'Hello'))
        assert bytes(w2) == bytes(w1)

    async def test_streaming_matches_dict_path(self):
        s1, w1, f1, _ = _make_sender()
        await s1({'type': ASGIEvent.HTTP_RESPONSE_START, 'status': 200,
                  'headers': [(b'content-type', b'text/plain')]})
        await s1({'type': ASGIEvent.HTTP_RESPONSE_BODY, 'body': b'c1',
                  'more_body': True})
        await s1({'type': ASGIEvent.HTTP_RESPONSE_BODY, 'body': b'c2',
                  'more_body': False})

        s2, w2, f2, _ = _make_sender()
        await s2(NativeResponse(status=200,
                                header=[(b'content-type', b'text/plain')]))
        await s2(NativeResponse(body=b'c1', more_body=True))
        await s2(NativeResponse(body=b'c2'))
        assert bytes(w2) == bytes(w1)

    async def test_trailers_matches_dict_path(self):
        # Full-form start(trailers=True) → non-terminal body → trailers.
        s1, w1, f1, _ = _make_sender()
        await s1({'type': ASGIEvent.HTTP_RESPONSE_START, 'status': 200,
                  'headers': [(b'content-type', b'text/plain')], 'trailers': True})
        await s1({'type': ASGIEvent.HTTP_RESPONSE_BODY, 'body': b'Hi',
                  'more_body': True})
        await s1({'type': ASGIEvent.HTTP_RESPONSE_TRAILERS,
                  'headers': [(b'x-t', b'v')]})

        s2, w2, f2, _ = _make_sender()
        await s2(NativeResponse(status=200,
                                header=[(b'content-type', b'text/plain')],
                                expects_trailers=True))
        await s2(NativeResponse(body=b'Hi', more_body=True))
        await s2(NativeResponse(trailers=[(b'x-t', b'v')]))
        assert bytes(w2) == bytes(w1)
        assert s2._end_stream_sent is True

    async def test_terminal_body_then_trailers_matches_dict_path(self):
        # start(trailers=True) → body(more_body=False) → trailers: on H2 the
        # ``expects_trailers`` deferral withholds END_STREAM from the terminal
        # DATA, so the trailers event writes the trailing HEADERS as the
        # END_STREAM carrier (lossless compat — unlike H1's content-length
        # drop).  The native path mirrors the dict path byte-for-byte.
        s1, w1, f1, _ = _make_sender()
        await s1({'type': ASGIEvent.HTTP_RESPONSE_START, 'status': 200,
                  'headers': [(b'content-type', b'text/plain')], 'trailers': True})
        await s1({'type': ASGIEvent.HTTP_RESPONSE_BODY, 'body': b'Hi',
                  'more_body': False})
        await s1({'type': ASGIEvent.HTTP_RESPONSE_TRAILERS,
                  'headers': [(b'x-t', b'v')]})

        s2, w2, f2, _ = _make_sender()
        await s2(NativeResponse(status=200,
                                header=[(b'content-type', b'text/plain')],
                                expects_trailers=True))
        await s2(NativeResponse(body=b'Hi'))
        await s2(NativeResponse(trailers=[(b'x-t', b'v')]))
        assert bytes(w2) == bytes(w1)
        # HEADERS + DATA (END_STREAM withheld) + trailing HEADERS (END_STREAM)
        frames = _collect_frames(w2, f2)
        assert len(frames) == 3, f'expected HEADERS + DATA + trailers; got {len(frames)}'
        assert not frames[1].end_stream
        assert frames[2].end_stream
        assert s2._end_stream_sent is True


# ---------------------------------------------------------------------------
# Single-object presence-driven path (H2)
# ---------------------------------------------------------------------------

class TestNativeResponsePath:
    async def test_header_only_is_buffered(self):
        s, w, f, _ = _make_sender()
        await s(NativeResponse(status=200,
                               header=[(b'content-type', b'text/plain')]))
        assert not w, 'header-only object must buffer, nothing on the wire'
        assert s._buffered_status is not None

    async def test_complete_single_send(self):
        s, w, f, mock = _make_sender()
        await s(NativeResponse(status=200,
                               header=[(b'content-type', b'text/plain')],
                               body=b'Hi'))
        # HEADERS + DATA coalesced into one write; DATA carries END_STREAM.
        assert mock.write.call_count == 1
        frames = _collect_frames(w, f)
        assert len(frames) == 2
        names = [k.lower() if isinstance(k, (bytes, bytearray))
                 else k.lower().encode() for k, _ in _decode_headers(w, f)]
        assert b':status' in names and b'content-type' in names
        assert bytes(frames[1].payload) == b'Hi'
        assert frames[1].end_stream
        assert s._end_stream_sent is True

    async def test_single_object_nonterminal_body_trailers(self):
        # One object with header + non-terminal body + trailers and
        # ``expects_trailers=True``: HEADERS + DATA + trailing HEADERS
        # coalesce into one write (the unary pattern).
        s, w, f, mock = _make_sender()
        await s(NativeResponse(status=200,
                               header=[(b'content-type', b'text/plain')],
                               body=b'Hi', more_body=True,
                               trailers=[(b'x-t', b'v')],
                               expects_trailers=True))
        assert mock.write.call_count == 1, 'unary native response must coalesce'
        assert s._buffered_body is None
        # three frames: HEADERS, DATA (no END_STREAM), trailing HEADERS
        frames = _collect_frames(w, f)
        assert len(frames) == 3, f'expected HEADERS + DATA + trailers; got {len(frames)}'
        assert not frames[1].end_stream, 'DATA must not END_STREAM (trailers carry it)'
        assert frames[2].end_stream, 'trailing HEADERS must carry END_STREAM'
        assert s._end_stream_sent is True
        # a stale auto-flush from the consumed chunk must not re-write
        await pytest_turn()
        assert mock.write.call_count == 1

    async def test_single_object_nonterminal_body_trailers_no_expect_flag(self):
        # Same object WITHOUT ``expects_trailers``: the trailers-coalescing
        # fast path does not apply, so the body writes immediately (HEADERS +
        # DATA, no END_STREAM) and the trailers write a standalone trailing
        # HEADERS — still wire-correct H2 trailers, just two writes.
        s, w, f, mock = _make_sender()
        await s(NativeResponse(status=200,
                               header=[(b'content-type', b'text/plain')],
                               body=b'Hi', more_body=True,
                               trailers=[(b'x-t', b'v')]))
        assert mock.write.call_count == 2
        frames = _collect_frames(w, f)
        assert len(frames) == 3
        assert not frames[1].end_stream
        assert frames[2].end_stream
        assert s._end_stream_sent is True

    async def test_single_object_terminal_body_trailers_drop(self):
        # Review-M1-style guard on H2: one object with header + terminal body
        # + trailers must not write frames after END_STREAM (RFC 9113 §8.1).
        s, w, f, _ = _make_sender()
        await s(NativeResponse(status=200,
                               header=[(b'content-type', b'text/plain')],
                               body=b'Hi',
                               trailers=[(b'x-t', b'v')]))
        frames = _collect_frames(w, f)
        assert len(frames) == 2, f'expected HEADERS + DATA only; got {len(frames)}'
        assert frames[1].end_stream
        assert s._end_stream_sent is True

    async def test_completed_drops_later_sends(self):
        s, w, f, _ = _make_sender()
        await s(NativeResponse(status=200, body=b'Hi'))
        before = bytes(w)
        await s(NativeResponse(body=b'second'))
        assert bytes(w) == before, 'later sends after END_STREAM must be dropped'


async def pytest_turn():
    """Yield one event-loop iteration (lets a stale auto-flush fire)."""
    import asyncio
    await asyncio.sleep(0)
