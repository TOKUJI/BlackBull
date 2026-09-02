"""HTTP/1.1 client defects — response framing and streaming.

Both are the connecting-role twin of a rule the *server* already
enforces.  `_validate_message_framing` in `http1_actor.py` collapses
comma-combined and repeated `Content-Length` into one set and refuses a
conflict; the client trusted the first value it found.  And the server
reads a body in bounded chunks; the client's `stream()` read a
`Content-Length` body with one `readexactly`, which is the opposite of
what its own docstring promises.
"""
from __future__ import annotations

import asyncio

import pytest

from blackbull.client.exceptions import ProtocolError
from blackbull.client.http1 import HTTP1ResponseRecipient
from blackbull.headers import Headers
from blackbull.server.recipient import AbstractReader

pytestmark = pytest.mark.asyncio


class _Reader(AbstractReader):
    """Feeds bytes; raises on an over-read so a wrong length is visible."""

    def __init__(self, data: bytes):
        self._buf = bytearray(data)

    @property
    def remaining(self) -> int:
        return len(self._buf)

    async def readexactly(self, n: int) -> bytes:
        if len(self._buf) < n:
            raise asyncio.IncompleteReadError(bytes(self._buf), n)
        chunk = bytes(self._buf[:n])
        del self._buf[:n]
        return chunk

    async def read(self, n: int) -> bytes:
        chunk = bytes(self._buf[:n])
        del self._buf[:n]
        return chunk

    async def readuntil(self, sep: bytes) -> bytes:
        idx = self._buf.find(sep)
        if idx == -1:
            raise asyncio.IncompleteReadError(bytes(self._buf), None)
        chunk = bytes(self._buf[:idx + len(sep)])
        del self._buf[:idx + len(sep)]
        return chunk


def _client() -> HTTP1ResponseRecipient:
    """The recipient owns response framing; the client just drives it."""
    return HTTP1ResponseRecipient()


# ===========================================================================
# C-M2 — a response that declares two different lengths is not a response
# ===========================================================================

class TestConflictingContentLength:
    async def test_two_different_lengths_are_refused(self):
        """CL.CL desync: believe the wrong one and the surplus becomes the
        next keep-alive response's status line.

        The server refuses this on the request side already
        (`_validate_message_framing`); a client that does not refuse it on
        the response side is the same defect facing the other way.
        """
        c = _client()
        headers = Headers([(b'content-length', b'5'),
                           (b'content-length', b'10')])
        reader = _Reader(b'HELLOSURPLUS!')

        with pytest.raises(ProtocolError):
            await c._read_body(reader, headers, status=200)

    async def test_the_comma_combined_form_is_refused_too(self):
        """One header line, two values — the same conflict, different spelling."""
        c = _client()
        headers = Headers([(b'content-length', b'5, 10')])
        reader = _Reader(b'HELLOSURPLUS!')

        with pytest.raises(ProtocolError):
            await c._read_body(reader, headers, status=200)

    async def test_repeated_but_equal_lengths_are_accepted(self):
        """RFC 9110 §8.6 permits the repeat when the values agree.

        Refusing it would break a response nothing is wrong with — and the
        server applies exactly this rule to requests, including the leading
        -zero normalisation.
        """
        c = _client()
        headers = Headers([(b'content-length', b'5'),
                           (b'content-length', b'005')])
        reader = _Reader(b'HELLO')

        assert await c._read_body(reader, headers, status=200) == b'HELLO'

    async def test_a_single_length_still_works(self):
        c = _client()
        headers = Headers([(b'content-length', b'5')])
        assert await c._read_body(_Reader(b'HELLO'), headers,
                          status=200) == b'HELLO'


# ===========================================================================
# C-M3 — stream() must stream
# ===========================================================================

class TestStreamingIsIncremental:
    async def test_a_content_length_body_arrives_in_chunks(self):
        """The docstring says "yield body chunks lazily"; one
        ``readexactly(n)`` yields the whole body as a single chunk, so a
        caller streaming a large response holds all of it — the memory
        bound streaming exists to provide is absent on exactly the path
        that needs it.
        """
        c = _client()
        body = b'x' * 200_000
        headers = Headers([(b'content-length', str(len(body)).encode())])

        chunks = [chunk async for chunk in
                  c._stream_body(_Reader(body), headers, status=200)]

        assert b''.join(chunks) == body
        assert len(chunks) > 1, (
            f'a {len(body)}-byte body arrived as {len(chunks)} chunk(s) — '
            f'stream() buffered the whole response')

    async def test_a_short_body_is_still_one_chunk(self):
        c = _client()
        headers = Headers([(b'content-length', b'5')])
        chunks = [chunk async for chunk in
                  c._stream_body(_Reader(b'HELLO'), headers, status=200)]
        assert chunks == [b'HELLO']

    async def test_streaming_refuses_a_conflicting_length_too(self):
        """The two defects meet here: streaming must not trust value one
        either."""
        c = _client()
        headers = Headers([(b'content-length', b'5'),
                           (b'content-length', b'10')])
        with pytest.raises(ProtocolError):
            [chunk async for chunk in c._stream_body(_Reader(b'HELLOSURPLUS!'),
                                                     headers, status=200)]
