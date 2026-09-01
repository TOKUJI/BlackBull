"""The chunked response reader: correct framing, and bounded by size.

Three things met in one fifteen-line function, which is why they are answered
together rather than in three passes over it:

* the chunk-size numeral was parsed with ``int(..., 16)``, which accepts far
  more than RFC 9112 §7.1's ``1*HEXDIG`` — including a negative, which reached
  ``readexactly()``;
* the trailer section was read as exactly one line, so a response with real
  trailers left the rest buffered and the **next** keep-alive response began
  parsing at a trailer field line;
* nothing bounded the size line, the chunk, or the total, so every octet the
  client held was a number the peer chose.

The total is on the buffering entry point only.  ``stream()``'s whole promise
is that a large response need not fit in memory, and a cap on the shared
iterator would cap the path that asked not to be capped.
"""
from __future__ import annotations

import pytest

from blackbull.client.exceptions import ProtocolError, ResponseTooLarge
from blackbull.client.http1 import HTTP1ResponseRecipient
from blackbull.server.recipient import AbstractReader


class _CannedReader(AbstractReader):
    def __init__(self, payload: bytes) -> None:
        self._buf = payload
        self._pos = 0

    async def read(self, n: int = -1) -> bytes:
        if n < 0:
            out = self._buf[self._pos:]
            self._pos = len(self._buf)
            return out
        out = self._buf[self._pos:self._pos + n]
        self._pos += len(out)
        return out


def _chunked(*chunks: bytes, trailers: bytes = b'') -> bytes:
    body = b''.join(b'%x\r\n%s\r\n' % (len(c), c) for c in chunks)
    return (b'HTTP/1.1 200 OK\r\ntransfer-encoding: chunked\r\n\r\n'
            + body + b'0\r\n' + trailers + b'\r\n')


class TestChunkSizeGrammar:
    """RFC 9112 §7.1: ``chunk-size = 1*HEXDIG``."""

    @pytest.mark.parametrize('numeral', [
        b'-5',        # reached readexactly(-5)
        b'0x5',
        b'1_0',
        b'  5  ',
        b'+5',
        b'',
    ])
    @pytest.mark.asyncio
    async def test_a_non_hexdig_numeral_is_refused(self, numeral):
        wire = (b'HTTP/1.1 200 OK\r\ntransfer-encoding: chunked\r\n\r\n'
                + numeral + b'\r\nhello\r\n0\r\n\r\n')
        with pytest.raises(ProtocolError):
            await HTTP1ResponseRecipient().receive(_CannedReader(wire))

    @pytest.mark.asyncio
    async def test_a_long_but_legal_numeral_is_accepted(self):
        """§7.1 asks recipients to *anticipate* large numerals, not to cap
        them, and ``last-chunk = 1*("0")`` puts no ceiling on the zeros."""
        wire = (b'HTTP/1.1 200 OK\r\ntransfer-encoding: chunked\r\n\r\n'
                + b'0' * 17 + b'\r\n\r\n')
        res = await HTTP1ResponseRecipient().receive(_CannedReader(wire))
        assert res.status == 200 and res.body == b''

    @pytest.mark.asyncio
    async def test_a_chunk_extension_is_still_accepted(self):
        wire = (b'HTTP/1.1 200 OK\r\ntransfer-encoding: chunked\r\n\r\n'
                b'5;name=value\r\nhello\r\n0\r\n\r\n')
        res = await HTTP1ResponseRecipient().receive(_CannedReader(wire))
        assert res.body == b'hello'


class TestTrailerSection:
    @pytest.mark.asyncio
    async def test_trailers_do_not_desync_the_next_response(self):
        """Two pipelined responses; the first carries a two-field trailer
        section.  Reading one line of it left the rest for the next status
        line."""
        first = _chunked(b'hi', trailers=b'x-a: 1\r\nx-b: 2\r\n')
        second = b'HTTP/1.1 204 No Content\r\n\r\n'
        reader = _CannedReader(first + second)
        recipient = HTTP1ResponseRecipient()

        one = await recipient.receive(reader)
        assert one.body == b'hi'
        two = await recipient.receive(reader)
        assert two.status == 204, 'the second response began inside the trailers'

    @pytest.mark.asyncio
    async def test_an_empty_trailer_section_still_works(self):
        res = await HTTP1ResponseRecipient().receive(_CannedReader(_chunked(b'hi')))
        assert res.body == b'hi'


class TestBodyBounds:
    @pytest.mark.asyncio
    async def test_a_chunked_body_over_the_total_is_refused(self, monkeypatch):
        monkeypatch.setenv('BB_CLIENT_BODY_MAX_TOTAL', '32')
        wire = _chunked(b'a' * 20, b'b' * 20)
        with pytest.raises(ResponseTooLarge):
            await HTTP1ResponseRecipient().receive(_CannedReader(wire))

    @pytest.mark.asyncio
    async def test_a_declared_body_over_the_total_is_refused(self, monkeypatch):
        monkeypatch.setenv('BB_CLIENT_BODY_MAX_TOTAL', '4')
        wire = b'HTTP/1.1 200 OK\r\ncontent-length: 10\r\n\r\n' + b'x' * 10
        with pytest.raises(ResponseTooLarge):
            await HTTP1ResponseRecipient().receive(_CannedReader(wire))

    @pytest.mark.asyncio
    async def test_streaming_is_not_capped_by_the_buffering_total(self, monkeypatch):
        """``stream()`` promises a large response need not fit in memory."""
        monkeypatch.setenv('BB_CLIENT_BODY_MAX_TOTAL', '32')
        wire = _chunked(b'a' * 20, b'b' * 20)
        out = b''.join([c async for c in
                        HTTP1ResponseRecipient().stream(_CannedReader(wire))])
        assert out == b'a' * 20 + b'b' * 20

    @pytest.mark.asyncio
    async def test_an_oversized_chunk_size_line_is_refused(self, monkeypatch):
        monkeypatch.setenv('BB_CLIENT_HEAD_MAX_LINE', '32')
        wire = (b'HTTP/1.1 200 OK\r\ntransfer-encoding: chunked\r\n\r\n'
                b'5;ext=' + b'a' * 200 + b'\r\nhello\r\n0\r\n\r\n')
        with pytest.raises(ResponseTooLarge):
            await HTTP1ResponseRecipient().receive(_CannedReader(wire))

    @pytest.mark.asyncio
    async def test_a_large_chunk_is_delivered_in_slices(self):
        """The unit column on the streaming path: one peer-declared chunk must
        not become one allocation."""
        big = b'z' * (200 * 1024)
        pieces = [c async for c in
                  HTTP1ResponseRecipient().stream(_CannedReader(_chunked(big)))]
        assert b''.join(pieces) == big
        assert len(pieces) > 1, 'the whole chunk materialised in one read'


class TestChunkTermination:
    @pytest.mark.asyncio
    async def test_chunk_data_must_be_followed_by_exactly_crlf(self):
        """Reading *until* CRLF would swallow spill up to the next one — the
        vector the server refuses as SMUG-CHUNK-SPILL."""
        wire = (b'HTTP/1.1 200 OK\r\ntransfer-encoding: chunked\r\n\r\n'
                b'5\r\nhelloXX\r\n0\r\n\r\n')
        with pytest.raises(ProtocolError):
            await HTTP1ResponseRecipient().receive(_CannedReader(wire))
