"""Sprint 38 Task A — HTTP/2 response trailers (``http.response.trailers``) emit-path tests.

Per the sprint plan, the HTTP/2 sender at
``blackbull/server/sender.py`` lacked a case for
``ASGIEvent.HTTP_RESPONSE_TRAILERS`` — it logged "unhandled event
type".  Sprint 38 adds the emit path: a HEADERS frame carrying
regular (non-pseudo) header fields with the END_STREAM flag set,
sent after the final DATA frame which carries ``END_STREAM`` clear.

These tests verify:

* The sender emits a HEADERS frame for a trailers event.
* That frame carries the END_STREAM flag.
* That frame contains *no* pseudo-headers (trailers are
  informational, not response metadata — RFC 9113 §8.1).
* Empty trailers (``headers: []``) still produce a valid
  HEADERS-with-END_STREAM frame.
* Multiple trailer fields are serialised correctly.
* A trailers-only response (no preceding DATA body) is handled.
* The preceding DATA frame must NOT have END_STREAM when trailers
  follow (sender contract — the caller is responsible for setting
  ``more_body: True`` on the preceding body event; we verify the
  sender does not strip or alter that intent).

Tests drive ``HTTP2Sender`` directly using an in-process fake
writer — the same pattern as ``test_server_response.py`` — so no
live sockets are needed.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock

from blackbull.asgi import ASGIEvent
from blackbull.server.sender import HTTP2Sender, AsyncioWriter
from blackbull.protocol.frame import FrameFactory
from blackbull.protocol.frame_types import (
    FrameTypes, HeaderFrameFlags, DataFrameFlags,
)


# ---------------------------------------------------------------------------
# Wire helpers — decode H2 frames written to the fake writer
# ---------------------------------------------------------------------------

def _parse_h2_frame(all_bytes: bytes, offset: int = 0) -> tuple[bytes, int, int, int, bytes] | None:
    """Parse one HTTP/2 frame from *all_bytes* at *offset*.

    Returns ``(type_byte, flags, stream_id, length, payload)`` or ``None``
    if fewer than 9 bytes remain.
    """
    if offset + 9 > len(all_bytes):
        return None
    length = int.from_bytes(all_bytes[offset:offset + 3], 'big')
    type_byte = all_bytes[offset + 3:offset + 4]
    flags = all_bytes[offset + 4]
    stream_id = int.from_bytes(all_bytes[offset + 5:offset + 9], 'big') & 0x7fffffff
    payload = all_bytes[offset + 9:offset + 9 + length]
    return type_byte, flags, stream_id, length, payload


def _all_frames(written: bytes) -> list[tuple[bytes, int, int, int, bytes]]:
    """Return every H2 frame parsed from the byte buffer."""
    frames = []
    offset = 0
    while True:
        parsed = _parse_h2_frame(written, offset)
        if parsed is None:
            break
        frames.append(parsed)
        offset += 9 + parsed[3]
    return frames


def _decode_h2_headers(payload: bytes, factory: FrameFactory) -> list[tuple[str, str]]:
    """Decode an HPACK-encoded header block into a list of (name, value) str pairs."""
    return [(k.decode() if isinstance(k, bytes) else k,
             v.decode() if isinstance(v, bytes) else v)
            for k, v in factory.decoder.decode(payload)]


def _make_sender() -> tuple[HTTP2Sender, bytearray, FrameFactory]:
    """Create an ``HTTP2Sender`` with a bytes-capturing fake writer.

    Returns ``(sender, written_buffer, factory)``.
    """
    written = bytearray()
    mock_writer = MagicMock()
    mock_writer.write = MagicMock(side_effect=lambda d: written.extend(d))
    mock_writer.drain = AsyncMock()
    factory = FrameFactory()
    sender = HTTP2Sender(AsyncioWriter(mock_writer), factory, stream_id=1)
    return sender, written, factory


# ---------------------------------------------------------------------------
# §1 — Headers frame shape for trailers
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestH2TrailersFrameShape:
    """The trailers emit path must produce a HEADERS frame (type=0x01) with
    END_STREAM set and no pseudo-headers."""

    async def test_trailers_event_emits_headers_frame(self):
        """``http.response.trailers`` with one trailer field must produce exactly
        one HEADERS frame on the wire."""
        sender, written, factory = _make_sender()
        await sender({
            'type': ASGIEvent.HTTP_RESPONSE_TRAILERS,
            'headers': [(b'x-checksum', b'abc123')],
        })

        frames = _all_frames(bytes(written))
        assert len(frames) == 1, f'expected 1 frame, got {len(frames)}'
        assert frames[0][0] == FrameTypes.HEADERS, (
            f'expected HEADERS frame, got type {frames[0][0]!r}')

    async def test_trailers_frame_has_end_stream_flag(self):
        """The trailers HEADERS frame MUST carry the END_STREAM flag.

        Rationale: trailers are the final event in an HTTP response;
        END_STREAM signals to the peer that this stream is done.
        """
        sender, written, factory = _make_sender()
        await sender({
            'type': ASGIEvent.HTTP_RESPONSE_TRAILERS,
            'headers': [(b'x-processing-time', b'42ms')],
        })

        _, flags, _, _, _ = _all_frames(bytes(written))[0]
        assert flags & HeaderFrameFlags.END_STREAM, (
            f'END_STREAM flag must be set; got flags={flags:#x}')

    async def test_trailers_frame_has_end_headers_flag(self):
        """The trailers HEADERS frame MUST carry END_HEADERS so the peer
        knows the header block is complete in a single frame (no CONTINUATION)."""
        sender, written, factory = _make_sender()
        await sender({
            'type': ASGIEvent.HTTP_RESPONSE_TRAILERS,
            'headers': [(b'x-id', b'1')],
        })

        _, flags, _, _, _ = _all_frames(bytes(written))[0]
        assert flags & HeaderFrameFlags.END_HEADERS, (
            f'END_HEADERS flag must be set; got flags={flags:#x}')

    async def test_trailers_frame_has_stream_id_1(self):
        """The frame must be sent on the correct stream — stream_id=1
        in our single-stream sender fixture."""
        sender, written, factory = _make_sender()
        await sender({
            'type': ASGIEvent.HTTP_RESPONSE_TRAILERS,
            'headers': [(b'x-stream', b'verify')],
        })

        _, _, stream_id, _, _ = _all_frames(bytes(written))[0]
        assert stream_id == 1, f'expected stream_id=1, got {stream_id}'


# ---------------------------------------------------------------------------
# §2 — No pseudo-headers in trailers
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestH2TrailersNoPseudoHeaders:
    """RFC 9113 §8.1: pseudo-header fields are only valid in the initial
    HEADERS frame that starts a request or response.  They MUST NOT appear
    in trailers."""

    async def test_no_pseudo_headers_in_trailers_frame(self):
        """The trailers HEADERS frame must contain zero pseudo-headers
        (no ``:status``, no ``:method``, no ``:path``, etc.).

        This is the key RFC 9113 §8.1 constraint that distinguishes
        response-triggering HEADERS from trailer HEADERS.
        """
        sender, written, factory = _make_sender()
        await sender({
            'type': ASGIEvent.HTTP_RESPONSE_TRAILERS,
            'headers': [(b'x-checksum', b'abc123')],
        })

        _, _, _, _, payload = _all_frames(bytes(written))[0]
        decoded = _decode_h2_headers(payload, factory)
        pseudo = [k for k, _ in decoded if k.startswith(':')]
        assert len(pseudo) == 0, (
            f'trailers must contain no pseudo-headers; found {pseudo!r}')

    async def test_trailers_with_multiple_fields_no_pseudo(self):
        """Multiple trailer fields, none of them pseudo-headers."""
        sender, written, factory = _make_sender()
        await sender({
            'type': ASGIEvent.HTTP_RESPONSE_TRAILERS,
            'headers': [
                (b'x-checksum', b'abc123'),
                (b'x-processing-time', b'42ms'),
                (b'x-cache', b'MISS'),
            ],
        })

        _, _, _, _, payload = _all_frames(bytes(written))[0]
        decoded = _decode_h2_headers(payload, factory)
        names = {k for k, _ in decoded}
        assert ':status' not in names
        assert ':method' not in names
        assert names == {'x-checksum', 'x-processing-time', 'x-cache'}, (
            f'unexpected header names: {names!r}')


# ---------------------------------------------------------------------------
# §3 — Empty trailers
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestH2TrailersEmpty:
    """An ``http.response.trailers`` event with no headers is valid ASGI 3.0
    — it signals "I'm done sending the response" without adding metadata.
    The sender must still emit a HEADERS+END_STREAM frame."""

    async def test_empty_trailers_headers_list(self):
        """``headers: []`` must produce a valid (empty) HEADERS+END_STREAM frame."""
        sender, written, factory = _make_sender()
        await sender({
            'type': ASGIEvent.HTTP_RESPONSE_TRAILERS,
            'headers': [],
        })

        frames = _all_frames(bytes(written))
        assert len(frames) == 1
        type_byte, flags, sid, _, payload = frames[0]
        assert type_byte == FrameTypes.HEADERS
        assert flags & HeaderFrameFlags.END_STREAM
        assert flags & HeaderFrameFlags.END_HEADERS
        assert sid == 1
        decoded = _decode_h2_headers(payload, factory)
        assert len(decoded) == 0, f'expected no headers, got {decoded!r}'

    async def test_trailers_event_without_headers_key(self):
        """An event dict lacking a ``headers`` key should be treated as
        having an empty header list — graceful degradation."""
        sender, written, factory = _make_sender()
        await sender({
            'type': ASGIEvent.HTTP_RESPONSE_TRAILERS,
            # no 'headers' key
        })

        frames = _all_frames(bytes(written))
        assert len(frames) >= 1
        # Must be a HEADERS frame — not a log-line fallback
        assert frames[0][0] == FrameTypes.HEADERS


# ---------------------------------------------------------------------------
# §4 — Interaction with the body-before-trailers sequence
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestH2TrailersAfterBody:
    """In the most common trailers scenario, the app sends:

    1. ``http.response.start``  → HEADERS (no END_STREAM)
    2. ``http.response.body`` with ``more_body: True`` → DATA (no END_STREAM)
    3. ``http.response.trailers`` → HEADERS (END_STREAM)

    The sender must not strip the ``more_body`` intent from step 2.
    """

    async def test_body_then_trailers_produces_data_then_headers(self):
        """Full three-event sequence must produce DATA (no END_STREAM)
        followed by HEADERS (END_STREAM)."""
        sender, written, factory = _make_sender()

        await sender({'type': ASGIEvent.HTTP_RESPONSE_START, 'status': 200, 'headers': []})
        await sender({'type': ASGIEvent.HTTP_RESPONSE_BODY, 'body': b'hello', 'more_body': True})
        await sender({'type': ASGIEvent.HTTP_RESPONSE_TRAILERS,
                      'headers': [(b'x-checksum', b'abc123')]})

        frames = _all_frames(bytes(written))
        # Expected: HEADERS (response start) → DATA → HEADERS (trailers)
        type_bytes = [f[0] for f in frames]
        assert type_bytes == [FrameTypes.HEADERS, FrameTypes.DATA, FrameTypes.HEADERS], (
            f'expected HEADERS→DATA→HEADERS sequence; got {type_bytes!r}')

    async def test_preceding_data_does_not_have_end_stream(self):
        """When ``more_body: True`` is passed on the body event, the
        preceding DATA frame must NOT carry END_STREAM — otherwise the
        peer would close the stream before reading trailers."""
        sender, written, factory = _make_sender()

        await sender({'type': ASGIEvent.HTTP_RESPONSE_START, 'status': 200, 'headers': []})
        await sender({'type': ASGIEvent.HTTP_RESPONSE_BODY, 'body': b'hello', 'more_body': True})
        await sender({'type': ASGIEvent.HTTP_RESPONSE_TRAILERS,
                      'headers': [(b'x-id', b'1')]})

        frames = _all_frames(bytes(written))
        data_frame = frames[1]
        _, data_flags, _, _, _ = data_frame
        assert not (data_flags & DataFrameFlags.END_STREAM), (
            'preceding DATA must not have END_STREAM when trailers follow')

    async def test_trailers_after_data_frames_end_stream(self):
        """The trailers HEADERS frame must be the LAST frame in the
        sequence and carry END_STREAM."""
        sender, written, factory = _make_sender()

        await sender({'type': ASGIEvent.HTTP_RESPONSE_START, 'status': 200, 'headers': []})
        await sender({'type': ASGIEvent.HTTP_RESPONSE_BODY, 'body': b'hello', 'more_body': True})
        await sender({'type': ASGIEvent.HTTP_RESPONSE_TRAILERS,
                      'headers': [(b'x-checksum', b'abc123')]})

        frames = _all_frames(bytes(written))
        trailers_frame = frames[-1]
        _, trailers_flags, _, _, _ = trailers_frame
        assert trailers_flags & HeaderFrameFlags.END_STREAM, (
            'trailers HEADERS must carry END_STREAM')


# ---------------------------------------------------------------------------
# §5 — Trailer field encoding correctness
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestH2TrailersFieldEncoding:
    """Trailer names and values must survive the round-trip through HPACK
    encoding → decoding without corruption."""

    async def test_trailer_name_case_preserved(self):
        """Trailer field names are lowercase per RFC 9113 §8.2.1 but the
        sender must not alter the case the app provides."""
        sender, written, factory = _make_sender()
        await sender({
            'type': ASGIEvent.HTTP_RESPONSE_TRAILERS,
            'headers': [(b'x-request-id', b'42')],
        })

        _, _, _, _, payload = _all_frames(bytes(written))[0]
        decoded = _decode_h2_headers(payload, factory)
        name = decoded[0][0]
        assert name == 'x-request-id', f'expected x-request-id, got {name!r}'

    async def test_trailer_value_round_trips(self):
        """A trailer value containing ASCII text must survive HPACK round-trip."""
        sender, written, factory = _make_sender()
        await sender({
            'type': ASGIEvent.HTTP_RESPONSE_TRAILERS,
            'headers': [(b'x-timing', b'p50=12ms; p99=340ms')],
        })

        _, _, _, _, payload = _all_frames(bytes(written))[0]
        decoded = _decode_h2_headers(payload, factory)
        value = decoded[0][1]
        assert value == 'p50=12ms; p99=340ms', f'value round-trip failed: {value!r}'

    async def test_multiple_trailer_fields_independent(self):
        """Each trailer field is a distinct HPACK entry."""
        sender, written, factory = _make_sender()
        await sender({
            'type': ASGIEvent.HTTP_RESPONSE_TRAILERS,
            'headers': [
                (b'x-a', b'1'),
                (b'x-b', b'2'),
                (b'x-c', b'3'),
            ],
        })

        _, _, _, _, payload = _all_frames(bytes(written))[0]
        decoded = _decode_h2_headers(payload, factory)
        assert len(decoded) == 3
        assert decoded[0] == ('x-a', '1')
        assert decoded[1] == ('x-b', '2')
        assert decoded[2] == ('x-c', '3')


# ---------------------------------------------------------------------------
# §6 — Trailer-only response (no preceding body)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestH2TrailersOnlyResponse:
    """A trailers-only response (response start + trailers, no body event)
    should still produce valid frames.  While unusual in practice, the
    sender must not crash or emit garbage."""

    async def test_start_then_trailers_no_body(self):
        """``http.response.start`` → ``http.response.trailers`` without
        an intervening body event must emit HEADERS (start) + HEADERS
        (trailers, END_STREAM)."""
        sender, written, factory = _make_sender()

        await sender({'type': ASGIEvent.HTTP_RESPONSE_START, 'status': 200, 'headers': []})
        await sender({'type': ASGIEvent.HTTP_RESPONSE_TRAILERS,
                      'headers': [(b'x-empty', b'true')]})

        frames = _all_frames(bytes(written))
        assert len(frames) == 2
        assert frames[0][0] == FrameTypes.HEADERS  # response start
        assert frames[1][0] == FrameTypes.HEADERS  # trailers
        assert frames[1][1] & HeaderFrameFlags.END_STREAM


# ---------------------------------------------------------------------------
# §7 — Sender contract: caller still controls END_STREAM on DATA
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestH2TrailersSenderContract:
    """The sender must honour the ``more_body`` flag on DATA frames.
    If the app sends ``more_body: False`` on the last DATA frame
    (no trailers), the sender must set END_STREAM on that DATA frame
    and a subsequent trailers event should be a protocol error or
    the sender must handle it gracefully (no double END_STREAM).

    These tests verify the sender does not *prevent* correct usage
    by the caller."""

    async def test_body_without_trailers_closes_stream_normally(self):
        """``more_body: False`` on the last DATA frame must set END_STREAM
        — the normal (non-trailers) path still works."""
        sender, written, factory = _make_sender()

        await sender({'type': ASGIEvent.HTTP_RESPONSE_START, 'status': 200, 'headers': []})
        await sender({'type': ASGIEvent.HTTP_RESPONSE_BODY, 'body': b'done', 'more_body': False})

        frames = _all_frames(bytes(written))
        data_frame = frames[1]
        _, data_flags, _, _, _ = data_frame
        assert data_flags & DataFrameFlags.END_STREAM, (
            'last DATA without trailers must carry END_STREAM')


# ---------------------------------------------------------------------------
# §8 — Cross-check with HTTP/1.1 trailers symmetry
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestH2TrailersCrossProtocolSymmetry:
    """Sprint 38 Task A is driven by the asymmetry where HTTP/1.1 trailers
    are fully wired but HTTP/2 trailers logged "unhandled event type".
    These tests verify the H2 path now handles the same event types that
    the H1 path accepts, matching the field name shape."""

    async def test_trailers_with_same_headers_shape_as_h1(self):
        """The header list shape ``[(b'name', b'value'), ...]`` is the
        same across both H1 and H2 paths — verify H2 accepts it."""
        sender, written, factory = _make_sender()
        # Same shape the H1 sender receives in its trailers case:
        headers = [(b'x-checksum', b'sha256-abc'), (b'x-count', b'3')]
        await sender({'type': ASGIEvent.HTTP_RESPONSE_TRAILERS, 'headers': headers})

        _, _, _, _, payload = _all_frames(bytes(written))[0]
        decoded = _decode_h2_headers(payload, factory)
        names = {k for k, _ in decoded}
        assert names == {'x-checksum', 'x-count'}, f'unexpected names: {names!r}'


# ---------------------------------------------------------------------------
# §9 — Previously-logged "unhandled event type" is gone
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestH2TrailersNoLongerUnhandled:
    """Before Sprint 38 Task A, the H2 sender fell through to the default
    ``else`` branch and logged ``HTTP2Sender: unhandled event type
    'http.response.trailers'``.  After the fix, the event must be
    dispatched to the trailers emit path — no log warning emitted."""

    async def test_trailers_no_longer_logs_unhandled_warning(self):
        """Using the full sender path, sending a trailers event must NOT
        produce the 'unhandled event type' log record."""
        import logging
        from io import StringIO

        # Capture log output at the blackbull logger level
        stream = StringIO()
        handler = logging.StreamHandler(stream)
        handler.setLevel(logging.WARNING)
        logger = logging.getLogger('blackbull.server.sender')
        logger.addHandler(handler)
        old_level = logger.level
        logger.setLevel(logging.WARNING)

        try:
            sender, written, factory = _make_sender()
            await sender({
                'type': ASGIEvent.HTTP_RESPONSE_TRAILERS,
                'headers': [(b'x-test', b'1')],
            })

            log_output = stream.getvalue()
            assert 'unhandled event type' not in log_output.lower(), (
                f"'unhandled event type' still logged: {log_output!r}")
        finally:
            logger.removeHandler(handler)
            logger.setLevel(old_level)

    async def test_trailers_event_is_not_an_error(self):
        """The trailers event must complete without raising an exception."""
        sender, written, factory = _make_sender()
        # Must not raise
        await sender({
            'type': ASGIEvent.HTTP_RESPONSE_TRAILERS,
            'headers': [(b'x-ok', b'1')],
        })
        # Must have produced output (not silently swallowed)
        assert len(written) > 0, 'trailers event produced no wire output'


# ---------------------------------------------------------------------------
# §10 — END_STREAM defensive guard symmetry across both __call__ branches
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestH2EndStreamGuardBytesPath:
    """RFC 9113 §8.1 — frames after END_STREAM are a protocol error.
    The guard added in Sprint 38 covered the dict-event branch; this
    section locks in the symmetric coverage on the bytes branch so a
    misbehaving ASGI app that bytes-sends after stream end gets a
    logged warning instead of a wire violation."""

    async def test_bytes_send_after_end_stream_is_dropped(self):
        import logging
        from io import StringIO

        sender, written, factory = _make_sender()
        # First, end the stream via the dict path (trailers).
        await sender({
            'type': ASGIEvent.HTTP_RESPONSE_TRAILERS,
            'headers': [(b'x-test', b'1')],
        })
        assert sender._end_stream_sent is True
        bytes_after_first = len(written)

        stream = StringIO()
        handler = logging.StreamHandler(stream)
        handler.setLevel(logging.WARNING)
        logger = logging.getLogger('blackbull.server.sender')
        logger.addHandler(handler)
        old_level = logger.level
        logger.setLevel(logging.WARNING)
        try:
            # Now misbehave: bytes write after the stream has ended.
            await sender(b'late body')
            assert len(written) == bytes_after_first, (
                'bytes write after END_STREAM produced wire bytes')
            assert 'END_STREAM already sent' in stream.getvalue()
        finally:
            logger.removeHandler(handler)
            logger.setLevel(old_level)

    async def test_reset_per_request_state_clears_end_stream_flag(self):
        sender, _written, _factory = _make_sender()
        sender._end_stream_sent = True
        sender.reset_per_request_state()
        assert sender._end_stream_sent is False
