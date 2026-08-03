"""Tests for HTTP1Sender consuming the native response message (Sprint 92).

Pins the wire-equivalence invariant: a NativeResponse-driven response
produces byte-identical output to the existing ASGI-dict path, and the
unified object's presence semantics (``is not None``) drive a single sender
path with no consumer detection.
"""
import pytest

from blackbull.server.sender import HTTP1Sender, AbstractWriter
from blackbull.native import NativeResponse
from blackbull.asgi import ASGIEvent

pytestmark = pytest.mark.asyncio


class BytesWriter(AbstractWriter):
    def __init__(self):
        self.data = b''

    async def write(self, data: bytes) -> None:
        self.data += data


def _sender():
    w = BytesWriter()
    return HTTP1Sender(w), w


# ---------------------------------------------------------------------------
# Wire equivalence: NativeResponse path == dict path
# ---------------------------------------------------------------------------

class TestWireEquivalence:
    async def test_complete_response_matches_dict_path(self):
        # dict path
        s1, w1 = _sender()
        await s1({'type': ASGIEvent.HTTP_RESPONSE_START, 'status': 200,
                  'headers': [(b'content-type', b'text/plain')]})
        await s1({'type': ASGIEvent.HTTP_RESPONSE_BODY, 'body': b'Hello',
                  'more_body': False})
        # native path — one object, one send
        s2, w2 = _sender()
        await s2(NativeResponse(status=200,
                                header=[(b'content-type', b'text/plain')],
                                body=b'Hello'))
        assert w2.data == w1.data

    async def test_streaming_matches_dict_path(self):
        s1, w1 = _sender()
        await s1({'type': ASGIEvent.HTTP_RESPONSE_START, 'status': 200,
                  'headers': [(b'content-type', b'text/plain')]})
        await s1({'type': ASGIEvent.HTTP_RESPONSE_BODY, 'body': b'chunk1',
                  'more_body': True})
        await s1({'type': ASGIEvent.HTTP_RESPONSE_BODY, 'body': b'chunk2',
                  'more_body': False})

        s2, w2 = _sender()
        await s2(NativeResponse(status=200,
                                header=[(b'content-type', b'text/plain')]))
        await s2(NativeResponse(body=b'chunk1', more_body=True))
        await s2(NativeResponse(body=b'chunk2'))
        assert w2.data == w1.data

    async def test_empty_body_matches_dict_path(self):
        s1, w1 = _sender()
        await s1({'type': ASGIEvent.HTTP_RESPONSE_START, 'status': 204,
                  'headers': []})
        await s1({'type': ASGIEvent.HTTP_RESPONSE_BODY, 'body': b'',
                  'more_body': False})

        s2, w2 = _sender()
        await s2(NativeResponse(status=204, header=[], body=b''))
        assert w2.data == w1.data


# ---------------------------------------------------------------------------
# Presence-driven single path
# ---------------------------------------------------------------------------

class TestNativeResponsePath:
    async def test_complete_single_send(self):
        s, w = _sender()
        await s(NativeResponse(status=200,
                               header=[(b'content-type', b'text/plain')],
                               body=b'Hi'))
        # sender auto-emits Date (RFC 9110 §6.6.1), so assert structure,
        # not exact bytes.
        assert w.data.startswith(b'HTTP/1.1 200 OK\r\n')
        assert b'content-type: text/plain\r\n' in w.data
        assert b'content-length: 2\r\n' in w.data
        assert w.data.endswith(b'\r\n\r\nHi')
        assert s._completed is True

    async def test_header_only_is_buffered(self):
        s, w = _sender()
        await s(NativeResponse(status=200,
                               header=[(b'content-type', b'text/plain')]))
        assert w.data == b''          # buffered, nothing on the wire
        assert s._buffered_status is not None

    async def test_header_then_body(self):
        s, w = _sender()
        await s(NativeResponse(status=200,
                               header=[(b'content-type', b'text/plain')]))
        await s(NativeResponse(body=b'Hi'))
        assert w.data.startswith(b'HTTP/1.1 200 OK\r\n')
        assert b'content-type: text/plain\r\n' in w.data
        assert b'content-length: 2\r\n' in w.data
        assert w.data.endswith(b'\r\n\r\nHi')
        assert s._completed is True

    async def test_streaming_chunks(self):
        s, w = _sender()
        await s(NativeResponse(status=200, header=[(b'content-type', b'text/plain')]))
        await s(NativeResponse(body=b'c1', more_body=True))
        await s(NativeResponse(body=b'c2'))
        # streaming: chunked transfer encoding (headers are lowercase-normalised)
        assert b'transfer-encoding: chunked' in w.data
        assert w.data.endswith(b'2\r\nc1\r\n2\r\nc2\r\n0\r\n\r\n')

    async def test_trailers(self):
        # trailers require chunked framing: the body must be non-terminal
        s, w = _sender()
        await s(NativeResponse(status=200, header=[(b'content-type', b'text/plain')]))
        await s(NativeResponse(body=b'Hi', more_body=True))
        await s(NativeResponse(trailers=[(b'x-t', b'v')]))
        assert b'transfer-encoding: chunked' in w.data
        assert b'x-t: v' in w.data
        assert w.data.endswith(b'0\r\nx-t: v\r\n\r\n')

    async def test_completed_drops_later_sends(self):
        s, w = _sender()
        await s(NativeResponse(status=200, body=b'Hi'))
        before = w.data
        await s(NativeResponse(body=b'second'))
        assert w.data == before      # second response dropped

    async def test_head_mode_no_body(self):
        s, w = _sender()
        s._head_mode = True
        await s(NativeResponse(status=200,
                               header=[(b'content-length', b'2')]))
        await s(NativeResponse(body=b'Hi'))
        # HEAD: headers but no body bytes
        assert b'HTTP/1.1 200 OK' in w.data
        assert b'Hi' not in w.data
