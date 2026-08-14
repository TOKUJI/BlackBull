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


@pytest.fixture(autouse=True)
def _frozen_http_date(monkeypatch):
    """Hold the ``date`` header still for the length of a test.

    Every wire-equivalence test here drives two senders and compares their
    bytes.  The senders inject ``date`` at whole-second resolution, so a test
    whose two halves straddle a second boundary compares responses that
    genuinely differ — a real failure of an invariant nobody claimed.  Freezing
    the clock is what makes "byte-identical" mean the thing under test.
    """
    monkeypatch.setattr('blackbull.server.sender._http_date',
                        lambda: b'Thu, 01 Jan 1970 00:00:00 GMT')


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

    async def test_two_chunk_terminal_then_trailers_holds_end_stream(self):
        # Review M2: a SECOND (multi-frame) terminal chunk with trailers
        # pending must not carry END_STREAM — END_STREAM belongs on the
        # trailing HEADERS (frames after END_STREAM are RFC 9113 §8.1
        # protocol errors).  Dict and native lanes share the helper; this
        # pins the ≥2-chunk case the single-chunk test missed.
        s, w, f, _ = _make_sender()
        await s({'type': ASGIEvent.HTTP_RESPONSE_START, 'status': 200,
                 'headers': [], 'trailers': True})
        await s({'type': ASGIEvent.HTTP_RESPONSE_BODY, 'body': b'c1',
                 'more_body': True})
        await s({'type': ASGIEvent.HTTP_RESPONSE_BODY, 'body': b'c2',
                 'more_body': False})
        await s({'type': ASGIEvent.HTTP_RESPONSE_TRAILERS,
                 'headers': [(b'x-t', b'v')]})

        frames = _collect_frames(w, f)
        data = [fr for fr in frames if fr.FRAME_TYPE == FrameTypes.DATA]
        assert len(data) == 2
        assert not any(fr.end_stream for fr in data), \
            'no DATA may END_STREAM while trailers are pending (RFC 9113 §8.1)'
        assert frames[-1].FRAME_TYPE == FrameTypes.HEADERS
        assert frames[-1].end_stream, 'trailing HEADERS must carry END_STREAM'
        assert s._end_stream_sent is True

    async def test_two_chunk_terminal_then_trailers_native(self):
        # Same M2 case driven natively (one object per chunk).
        s, w, f, _ = _make_sender()
        await s(NativeResponse(status=200, header=[], expects_trailers=True))
        await s(NativeResponse(body=b'c1', more_body=True))
        await s(NativeResponse(body=b'c2'))
        await s(NativeResponse(trailers=[(b'x-t', b'v')]))

        frames = _collect_frames(w, f)
        data = [fr for fr in frames if fr.FRAME_TYPE == FrameTypes.DATA]
        assert not any(fr.end_stream for fr in data), \
            'no DATA may END_STREAM while trailers are pending (RFC 9113 §8.1)'
        assert frames[-1].end_stream
        assert s._end_stream_sent is True


class TestAccessLogCapture:
    """Review M1: the HTTP2Sender captures the access-log record inline in
    its native / dict / bytes arms — the old dict-shaped
    ``_make_capturing_send`` wrapper never saw a NativeResponse after the
    H2 native seam, so status/response_bytes regressed to '-'/0."""

    def _record(self):
        from blackbull.server.access_log import AccessLogRecord
        return AccessLogRecord(
            client_ip='1.2.3.4', method='GET', path='/', http_version='2')

    async def test_native_captures_status_and_bytes(self):
        s, w, f, _ = _make_sender()
        record = self._record()
        s._log_record = record
        await s(NativeResponse(status=201,
                               header=[(b'content-type', b'text/plain')],
                               body=b'Hello'))
        assert record.status == 201
        assert record.response_bytes == 5

    async def test_native_streaming_captures_all_chunks(self):
        s, w, f, _ = _make_sender()
        record = self._record()
        s._log_record = record
        await s(NativeResponse(status=200, header=[(b'content-type', b'text/plain')]))
        await s(NativeResponse(body=b'c1', more_body=True))
        await s(NativeResponse(body=b'c2'))
        assert record.status == 200
        assert record.response_bytes == 4

    async def test_dict_captures_status_and_bytes(self):
        s, w, f, _ = _make_sender()
        record = self._record()
        s._log_record = record
        await s({'type': ASGIEvent.HTTP_RESPONSE_START, 'status': 204,
                 'headers': []})
        await s({'type': ASGIEvent.HTTP_RESPONSE_BODY, 'body': b'',
                 'more_body': False})
        assert record.status == 204

    async def test_bytes_path_captures(self):
        s, w, f, _ = _make_sender()
        record = self._record()
        s._log_record = record
        await s(b'hi')
        assert record.status == 200
        assert record.response_bytes == 2

    async def test_phase_marks_present_on_native(self):
        # start_arm_in/out and body_arm_in/out are marked on the native path
        # (same names as the H1 inline capture).
        import blackbull.server.access_log as alog
        old = alog.PHASE_TRACE
        alog.PHASE_TRACE = True
        try:
            s, w, f, _ = _make_sender()
            record = self._record()
            s._log_record = record
            await s(NativeResponse(status=200,
                                   header=[(b'content-type', b'text/plain')],
                                   body=b'Hi'))
            assert 'start_arm_in' in record.phases
            assert 'start_arm_out' in record.phases
            assert 'body_arm_in' in record.phases
            assert 'body_arm_out' in record.phases
        finally:
            alog.PHASE_TRACE = old


async def pytest_turn():
    """Yield one event-loop iteration (lets a stale auto-flush fire)."""
    import asyncio
    await asyncio.sleep(0)
