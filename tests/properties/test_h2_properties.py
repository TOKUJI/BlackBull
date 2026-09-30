"""Hypothesis property tests for H2 field validation and frame parsing.

Covers patterns discovered during fuzz testing and the RFC 9113 audit:
  - Field value character validation (CR, LF, NUL — G13 failures)
  - Frame header parsing robustness (truncated headers, invalid lengths)
  - Flow-control window edge cases

Uses ``@given`` to verify invariants hold for ALL valid/invalid inputs,
not just hand-picked examples.
"""
from __future__ import annotations

import asyncio
import struct
from http import HTTPStatus
from unittest.mock import AsyncMock

import pytest
from hypothesis import given, strategies as st

from blackbull.protocol.field_grammar import (
    FIELD_VALUE_ALLOWED_OCTETS, TCHAR_SET)
from hpack import HPACKError

from blackbull.protocol.frame import FrameFactory
from blackbull.protocol.frame_types import (
    ErrorCodes, FrameBase, FrameFormatError, FrameTypes,
    field_name_is_valid, field_value_has_boundary_whitespace,
    field_value_is_valid)
from blackbull.server.http2_actor import HTTP2Actor
from blackbull.server.sender import AbstractWriter, ConnectionWindow, HTTP2Sender

# Octets legal inside a field name: the RFC 9110 §5.6.2 token alphabet, minus
# the uppercase RFC 9113 §8.2 forbids in an HTTP/2 name.
_safe_name_octets = st.sampled_from(sorted(
    b for b in TCHAR_SET if not 0x41 <= b <= 0x5A))
_safe_name = st.lists(_safe_name_octets, min_size=1, max_size=20).map(bytes)
# Octets legal inside a field value: RFC 9110 §5.5 field-content, the
# complement of which both transports refuse.
_value_forbidden = frozenset(range(0x100)) - frozenset(
    FIELD_VALUE_ALLOWED_OCTETS)
_safe_value_octets = st.sampled_from(
    [b for b in range(0x100) if b not in _value_forbidden])
_safe_value = st.lists(_safe_value_octets, max_size=40).map(bytes)
# The same, minus the two octets RFC 9113 §8.2.1 forbids at either end.
_safe_value_edge = st.sampled_from(
    [b for b in range(0x100)
     if b not in _value_forbidden and b not in (0x20, 0x09)]).map(bytes)
_edgeless_value = st.tuples(
    _safe_value_edge, _safe_value, _safe_value_edge
).map(lambda t: t[0] + t[1] + t[2])

# Strategies
_valid_frame_types = st.sampled_from([0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10])
_any_frame_type = st.integers(min_value=0, max_value=255)
_any_flags = st.integers(min_value=0, max_value=255)
_any_stream_id = st.integers(min_value=0, max_value=0x7FFFFFFF)
_any_payload = st.binary(min_size=0, max_size=16384)

# The codec's own type lookup, rebuilt here from the registry's members.
_TYPE_BY_BYTE = {m.value: m for m in FrameTypes}
# Frames whose payload round-trips as raw octets (the other types parse
# theirs into typed fields — HPACK headers, GOAWAY's last-stream/error ...).
_RAW_PAYLOAD_OCTETS = frozenset(
    t.value for t in (FrameTypes.DATA, FrameTypes.SETTINGS,
                      FrameTypes.WINDOW_UPDATE, FrameTypes.CONTINUATION))
# Types that decode an HPACK field section at parse time.
_HPACK_TYPES = frozenset((FrameTypes.HEADERS.value, FrameTypes.PUSH_PROMISE.value))


class _RecordingWriter(AbstractWriter):
    """Records the frames a sender writes, parsed from the raw octets."""

    def __init__(self):
        self.frames = []

    async def write(self, data: bytes) -> None:
        offset = 0
        while offset < len(data):
            length = int.from_bytes(data[offset:offset + 3], 'big')
            end = offset + 9 + length
            kind, flags = data[offset + 3:offset + 5]
            sid = int.from_bytes(data[offset + 5:offset + 9], 'big')
            self.frames.append((kind, flags, sid, data[offset + 9:end]))
            offset = end

    def data(self) -> bytes:
        return b''.join(payload for kind, _, _, payload in self.frames
                        if kind == 0)


def _make_sender(writer, *, initial_window):
    return HTTP2Sender(writer, FrameFactory(), 1,
                       conn_window=ConnectionWindow(),
                       initial_window=initial_window,
                       flow_control_timeout=0.0)


async def _pump() -> None:
    """Let the sender task run to its next suspension (or completion)."""
    for _ in range(5):
        await asyncio.sleep(0)


# ═══════════════════════════════════════════════════════════════════════
# §1 — Frame header parsing: any 9-byte header must not crash
# ═══════════════════════════════════════════════════════════════════════

class TestFrameHeaderRobustness:
    """FrameFactory must handle arbitrary frame headers without crashing.
    Discovered during fuzz: partial headers (3 bytes) cause behavioral
    differences between BlackBull and nginx."""

    @given(
        length=st.integers(min_value=0, max_value=0xFFFFFF),
        type_byte=_any_frame_type,
        flags=_any_flags,
        stream_id=_any_stream_id,
    )
    def test_any_frame_header_builds_without_crash(self, length, type_byte,
                                                     flags, stream_id):
        """Building a raw H2 frame header from arbitrary values must not
        raise any Python exception.

        The generated length is a header field — real payloads are bounded by
        SETTINGS_MAX_FRAME_SIZE, so the writer is driven with the field rather
        than 16 MiB of body — and the written header must parse back to the
        same fields through the product codec.
        """
        factory = FrameFactory()
        header = (length.to_bytes(3, 'big')
                  + bytes([type_byte, flags])
                  + stream_id.to_bytes(4, 'big'))
        assert len(header) == 9

        # The product writer serializes the header fields (FrameBase.save).
        assert FrameBase(length, bytes([type_byte]), flags,
                         stream_id).save() == header

        # Registered types also build through the factory itself.  Flag and
        # payload combinations the frame format rejects (DATA PADDED with no
        # padding octets, say) answer with the documented FrameFormatError
        # verdict — a protocol refusal, not a crash.
        frame_type = _TYPE_BY_BYTE.get(bytes([type_byte]))
        if frame_type is not None:
            try:
                built = factory.create(frame_type, flags, stream_id, data=b'')
            except FrameFormatError:
                pass
            else:
                assert (built.flags, built.stream_id) == (flags, stream_id)

        # The product codec parses the written header back without crashing.
        try:
            parsed = factory.load(header)
        except FrameFormatError:
            # The same documented refusal on the parse side (a PADDED DATA
            # header with no padding octets to read, say).
            return
        assert parsed.flags == flags
        assert parsed.stream_id == stream_id
        assert parsed.FrameType() == frame_type
        if frame_type is None:
            # Unknown frames (RFC 9113 §5.5) carry their declared length.
            assert parsed.length == length

    @given(
        partial_header=st.binary(min_size=0, max_size=8),
    )
    def test_partial_header_handled_gracefully(self, partial_header):
        """Partial frame headers (0-8 bytes) must be handled gracefully
        by any parser that reads them — no crashes."""
        from tests.conformance.http2._harness import _BufferReader

        # The actor's frame-header reader: a short read is the protocol's
        # incomplete-read outcome (receive() -> b'' -> the connection is
        # over), never IndexError/ValueError.
        actor = HTTP2Actor(_BufferReader(partial_header),
                           _RecordingWriter(), AsyncMock(), None)
        assert asyncio.run(actor.receive()) == b''

        # The codec seam answers a short buffer with its own documented
        # verdict — a plain incomplete-data Exception, not a parse crash.
        # (Pinning the bare Exception is deliberate for now; BLA-517 tracks
        # giving it a named error instead.)
        with pytest.raises(Exception) as excinfo:
            FrameFactory().load(partial_header)
        assert type(excinfo.value) is Exception

    @given(
        frame=st.binary(min_size=9, max_size=9 + 16384),
    )
    def test_full_frame_never_crashes_parser(self, frame):
        """Any well-formed 9-byte header + up to 16KB payload must not
        crash a frame parser that reads length-prefixed data.

        Truncated frames take the codec's documented paths; well-formed
        frames yield the declared length/type/flags/stream_id/payload.
        """
        factory = FrameFactory()
        length = int.from_bytes(frame[:3], 'big')
        type_byte = frame[3:4]
        flags = frame[4]
        stream_id = int.from_bytes(frame[5:9], 'big')
        try:
            parsed = factory.load(frame)
        except FrameFormatError as exc:
            # The parser's documented protocol verdict — the frame-shape
            # rules (§6.1/§6.4/§6.6/§6.7: RST_STREAM is 4 octets, PING is 8,
            # padding stays inside the frame) — carrying the protocol error
            # code its section names.  A refusal, not a crash.
            assert exc.error_code in (ErrorCodes.PROTOCOL_ERROR,
                                      ErrorCodes.FRAME_SIZE_ERROR)
            return
        except HPACKError:
            # The documented verdict for an undecodable field section (the
            # actor maps it to COMPRESSION_ERROR): HEADERS and PUSH_PROMISE
            # decode their payload at parse time.
            assert type_byte in _HPACK_TYPES
            return
        assert parsed.flags == flags
        assert parsed.stream_id == stream_id & 0x7FFFFFFF
        assert parsed.FrameType() == _TYPE_BY_BYTE.get(type_byte)
        if len(frame) >= 9 + length:
            # Well-formed: the frame carries exactly what was declared.
            assert parsed.length == length
            if type_byte in _RAW_PAYLOAD_OCTETS:
                assert parsed.payload == frame[9:9 + length]
        else:
            # Truncated: the codec builds from the bytes that arrived (the
            # actor's framing never presents these — mid-frame EOF ends the
            # connection — so arrival-bounded state is the documented one).
            assert parsed.length == (length if parsed.FrameType() is None
                                    else len(frame) - 9)


# ═══════════════════════════════════════════════════════════════════════
# §2 — Field value character validation (G13 patterns)
# ═══════════════════════════════════════════════════════════════════════

class TestFieldValidationProperties:
    """RFC 9113 §8.2.1: Field values MUST NOT contain CR, LF, NUL.
    Field names MUST NOT contain colon or uppercase characters.
    These are the G13 failures found in the RFC 9113 audit."""

    @given(value=_safe_value)
    def test_field_value_without_prohibited_chars_is_valid(self, value):
        """Field values without CR/LF/NUL must be considered valid."""
        assert field_value_is_valid(value)

    @given(prefix=_safe_value, suffix=_safe_value,
           bad=st.sampled_from([0x00, 0x0A, 0x0D]))
    def test_field_value_with_prohibited_char_must_be_rejected(
            self, prefix, suffix, bad):
        """Field values containing CR, LF, or NUL MUST be rejected as
        malformed (RFC 9113 §8.2.1)."""
        value = prefix + bytes([bad]) + suffix
        assert not field_value_is_valid(value)

    @given(value=_edgeless_value)
    def test_a_field_value_without_edge_whitespace_is_not_a_violation(
            self, value):
        """SP and HTAB are legal inside a field value, and these values have
        neither at either end."""
        assert not field_value_has_boundary_whitespace(value)

    @given(value=_safe_value, edge=st.sampled_from([0x20, 0x09]),
           leading=st.booleans())
    def test_a_field_value_bounded_by_whitespace_is_a_violation(
            self, value, edge, leading):
        """A field value that starts or ends with SP or HTAB violates the
        MUST, wherever else its bytes came from (RFC 9113 §8.2.1)."""
        padded = bytes([edge]) + value if leading else value + bytes([edge])
        assert field_value_has_boundary_whitespace(padded)

    @given(prefix=_safe_name, suffix=_safe_name,
           upper=st.integers(min_value=0x41, max_value=0x5A))
    def test_uppercase_field_name_must_be_rejected(self, prefix, suffix, upper):
        """Field names with uppercase characters MUST be rejected
        (RFC 9113 §8.2.1: names in 0x41-0x5A range prohibited)."""
        name = prefix + bytes([upper]) + suffix
        assert not field_name_is_valid(name)

    @given(prefix=_safe_name, suffix=_safe_name)
    def test_colon_in_field_name_must_be_rejected(self, prefix, suffix):
        """Field names MUST NOT include a colon other than the leading
        pseudo-header marker (RFC 9113 §8.2.1).  ``prefix`` is non-empty so
        the injected colon is never the leading octet."""
        name = prefix + b':' + suffix
        assert not field_name_is_valid(name)

    @given(name=_safe_name)
    def test_valid_lowercase_field_name_is_accepted(self, name):
        """A name of only legal octets must be accepted (pseudo-headers,
        which start with a single colon, included)."""
        assert field_name_is_valid(name)
        assert field_name_is_valid(b':' + name)


# ═══════════════════════════════════════════════════════════════════════
# §3 — Flow-control window invariants
# ═══════════════════════════════════════════════════════════════════════

class TestFlowControlInvariants:
    """RFC 9113 §6.9: Flow-control window invariants discovered during
    fuzz-driven bug hunting."""

    @given(
        initial=st.integers(min_value=65535, max_value=65535),
        consumed=st.integers(min_value=0, max_value=131070),
        credited=st.integers(min_value=0, max_value=0x7FFFFFFF),
    )
    def test_window_update_accumulates_unbounded_bla514(self, initial, consumed, credited):
        """The current (non-conformant) behaviour, named for what it is.

        PRODUCT GAP — tracked in BLA-514: sender.window_update adds the
        peer's increments unchecked and has no FLOW_CONTROL_ERROR path.
        """
        sender = _make_sender(_RecordingWriter(),
                              initial_window=initial - consumed)
        sender.window_update(credited)
        assert sender.stream_window_size == initial - consumed + credited

    @given(
        initial=st.integers(min_value=65535, max_value=65535),
        consumed=st.integers(min_value=0, max_value=131070),
        credited=st.integers(min_value=0, max_value=0x7FFFFFFF),
    )
    @pytest.mark.xfail(strict=True, reason='BLA-514: no FLOW_CONTROL_ERROR path')
    def test_window_never_exceeds_max(self, initial, consumed, credited):
        """RFC 9113 §6.9.1 — the flow-control window must never exceed
        2^31-1: an increment that would push it past the bound is rejected
        with FLOW_CONTROL_ERROR.  This is the conformant assertion; it xfails
        until BLA-514 lands (strict — the day the product implements the
        path this turns into a failure and the marker must go)."""
        sender = _make_sender(_RecordingWriter(),
                              initial_window=initial - consumed)
        try:
            sender.window_update(credited)
        except Exception as exc:
            assert getattr(exc, 'error_code', None) is ErrorCodes.FLOW_CONTROL_ERROR
            return
        assert sender.stream_window_size <= 0x7FFFFFFF, (
            f'window {sender.stream_window_size} exceeds 2^31-1 (RFC 9113 §6.9.1)'
        )

    @given(
        frame_size=st.integers(min_value=1, max_value=65535),
        window_size=st.integers(min_value=0, max_value=65535),
    )
    def test_sender_never_exceeds_window(self, frame_size, window_size):
        """A sender MUST NOT send frames exceeding the available window."""

        async def drive():
            writer = _RecordingWriter()
            sender = _make_sender(writer, initial_window=window_size)
            task = asyncio.ensure_future(
                sender(b'x' * frame_size, HTTPStatus.OK))
            await _pump()
            # Every DATA frame on the wire is at most the window that was
            # left when it was written (RFC 9113 §6.9).
            remaining = window_size
            for kind, _, _, payload in writer.frames:
                if kind == 0 and payload:
                    assert len(payload) <= remaining, (
                        f'DATA frame of {len(payload)} exceeds the '
                        f'{remaining} bytes of window left'
                    )
                    remaining -= len(payload)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        asyncio.run(drive())

    @given(
        window_after=st.integers(min_value=-65535, max_value=-1),
    )
    def test_negative_window_must_be_tracked(self, window_after):
        """RFC 9113 §6.9.2: Negative flow-control windows MUST be tracked.
        Sending MUST NOT resume until WINDOW_UPDATE makes it positive."""
        assert window_after < 0
        body = b'held-back'

        async def drive():
            writer = _RecordingWriter()
            sender = _make_sender(writer, initial_window=window_after)
            task = asyncio.ensure_future(sender(body, HTTPStatus.OK))
            await _pump()
            # The negative window is tracked: no flow-controlled frame goes
            # out while it is negative.
            assert writer.data() == b'', (
                'flow-controlled writes must be held while the window is negative'
            )
            sender.window_update(-window_after + len(body))
            await _pump()
            assert writer.data() == body, (
                'a WINDOW_UPDATE making the window positive must release the body'
            )
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        asyncio.run(drive())


# ═══════════════════════════════════════════════════════════════════════
# §4 — WINDOW_UPDATE invariants
# ═══════════════════════════════════════════════════════════════════════

class TestWindowUpdateInvariants:
    """WINDOW_UPDATE frame invariants per RFC 9113 §6.9."""

    @given(increment=st.integers(min_value=1, max_value=0x7FFFFFFF))
    def test_valid_wu_increment_is_accepted(self, increment):
        """WINDOW_UPDATE with increment 1..2^31-1 is valid."""
        assert 1 <= increment <= 0x7FFFFFFF
        factory = FrameFactory()
        # The wire form of the credit, through the product codec ...
        wire = factory.window_update(1, increment).save()
        parsed = factory.load(wire)
        assert parsed.window_size == increment
        # ... applied to the sender: accepted, and the window grows by the
        # increment with no protocol error.
        sender = _make_sender(_RecordingWriter(), initial_window=0)
        sender.window_update(parsed.window_size)
        assert sender.stream_window_size == increment
