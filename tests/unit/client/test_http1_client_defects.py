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


import pytest

from blackbull.client.exceptions import ProtocolError
from blackbull.client.http1 import HTTP1ResponseRecipient

from tests.harness import fed

pytestmark = pytest.mark.asyncio


def _Reader(payload: bytes = b''):
    return fed(payload).reader


def _recipient() -> HTTP1ResponseRecipient:
    """The recipient owns response framing; the client just drives it."""
    return HTTP1ResponseRecipient()


def _response(fields: bytes, body: bytes = b'') -> bytes:
    return b'HTTP/1.1 200 OK\r\n' + fields + b'\r\n' + body


# ===========================================================================
# C-M2 — a response that declares two different lengths is not a response
# ===========================================================================

class TestStreamingIsIncremental:
    async def test_a_content_length_body_arrives_in_chunks(self):
        """The docstring says "yield body chunks lazily"; one
        ``readexactly(n)`` yields the whole body as a single chunk, so a
        caller streaming a large response holds all of it — the memory
        bound streaming exists to provide is absent on exactly the path
        that needs it.
        """
        body = b'x' * 200_000
        reader = _Reader(_response(
            b'content-length: %d\r\n' % len(body), body))

        chunks = [chunk async for chunk in _recipient().stream(reader)]

        assert b''.join(chunks) == body
        assert len(chunks) > 1, (
            f'a {len(body)}-byte body arrived as {len(chunks)} chunk(s) — '
            f'stream() buffered the whole response')

    async def test_a_short_body_is_still_one_chunk(self):
        reader = _Reader(_response(b'content-length: 5\r\n', b'HELLO'))

        chunks = [chunk async for chunk in _recipient().stream(reader)]

        assert chunks == [b'HELLO']

    async def test_streaming_refuses_a_conflicting_length_too(self):
        """The two defects meet here: streaming must not trust value one
        either."""
        reader = _Reader(_response(
            b'content-length: 5\r\ncontent-length: 10\r\n', b'HELLOSURPLUS!'))

        with pytest.raises(ProtocolError):
            [chunk async for chunk in _recipient().stream(reader)]
