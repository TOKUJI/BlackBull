"""Byte-equivalence tests for the frame-assembly-fast-path Tier 2 builders.

``blackbull.server.sender.build_response_headers`` /
``build_trailers`` encode a HEADERS frame straight to wire bytes,
bypassing ``FrameFactory.create()`` and the ``Headers`` object.  The
non-disruption guarantee is that, fed identical inputs through identical
HPACK encoder state, the builder output is **byte-for-byte identical** to
the ``Headers.save()`` object path it replaces.

Each test drives two independent encoders in lockstep — one through the
object path, one through the builder — and asserts equality, including a
multi-frame sequence to prove the shared dynamic table evolves the same
way.
"""
from hpack import Encoder
import pytest

from blackbull.protocol.frame_types import (
    FrameTypes, HeaderFrameFlags, Headers, PseudoHeaders,
)
from blackbull.server.sender import build_response_headers, build_trailers


def _object_path(encoder, stream_id, status, headers, *, end_stream):
    """Reproduce the pre-Tier-2 Headers.save() emission for a response."""
    flags = int(HeaderFrameFlags.END_HEADERS)
    if end_stream:
        flags |= int(HeaderFrameFlags.END_STREAM)
    h = Headers(length=0, type_=FrameTypes.HEADERS.value, flags=flags,
                stream_id=stream_id, encoder=encoder)
    h.pseudo_headers[PseudoHeaders.STATUS] = str(status)
    for k, v in headers:
        h.headers.append((k, v))
    return h.save()


def _object_trailers(encoder, stream_id, headers):
    h = Headers(length=0, type_=FrameTypes.HEADERS.value,
                flags=int(HeaderFrameFlags.END_HEADERS)
                | int(HeaderFrameFlags.END_STREAM),
                stream_id=stream_id, encoder=encoder)
    for k, v in headers:
        h.headers.append((k, v))
    return h.save()


# A date header is supplied explicitly so neither path auto-injects a
# time-dependent one (which would race across the two save() calls).
_DATE = (b"date", b"Sun, 28 Jun 2026 00:00:00 GMT")


@pytest.mark.parametrize("status", [200, 204, 206, 304, 400, 404, 500])
def test_static_status_byte_identical(status):
    obj = _object_path(Encoder(), 1, status, [
        (b"content-type", b"text/plain"), (b"content-length", b"4"), _DATE,
    ], end_stream=False)
    built = build_response_headers(Encoder(), 1, status, [
        (b"content-type", b"text/plain"), (b"content-length", b"4"), _DATE,
    ], end_stream=False)
    assert built == obj


@pytest.mark.parametrize("status", [201, 302, 403, 418, 503])
def test_non_static_status_byte_identical(status):
    """Statuses outside the HPACK static table go through encoder.encode."""
    obj = _object_path(Encoder(), 3, status, [(b"x-foo", b"bar"), _DATE],
                       end_stream=False)
    built = build_response_headers(Encoder(), 3, status, [(b"x-foo", b"bar"), _DATE],
                                   end_stream=False)
    assert built == obj


def test_end_stream_flag_byte_identical():
    obj = _object_path(Encoder(), 5, 200, [_DATE], end_stream=True)
    built = build_response_headers(Encoder(), 5, 200, [_DATE], end_stream=True)
    assert built == obj


def test_date_auto_injected_when_absent():
    """With no date supplied the builder injects one; the frame still decodes
    and carries exactly one date field."""
    from hpack import Decoder
    built = build_response_headers(Encoder(), 7, 200, [(b"x-a", b"1")],
                                   end_stream=False)
    fields = [(bytes(k), bytes(v)) for k, v in Decoder().decode(built[9:], raw=True)]
    assert (b":status", b"200") in fields
    assert sum(1 for k, _ in fields if k == b"date") == 1


def test_trailers_byte_identical():
    obj = _object_trailers(Encoder(), 9, [(b"grpc-status", b"0")])
    built = build_trailers(Encoder(), 9, [(b"grpc-status", b"0")])
    assert built == obj


def test_multi_frame_shared_encoder_stays_equivalent():
    """Dynamic-table state must evolve identically across a sequence."""
    enc_obj = Encoder()
    enc_built = Encoder()
    sequence = [
        (200, [(b"content-type", b"application/json"), _DATE]),
        (200, [(b"content-type", b"application/json"), _DATE]),   # repeat → dyn-table hit
        (404, [(b"x-trace", b"abc"), _DATE]),
        (200, [(b"content-type", b"application/json"), _DATE]),
    ]
    for i, (status, headers) in enumerate(sequence, start=1):
        obj = _object_path(enc_obj, i * 2 + 1, status, headers, end_stream=False)
        built = build_response_headers(enc_built, i * 2 + 1, status, headers,
                                       end_stream=False)
        assert built == obj, f"diverged at frame {i}"
