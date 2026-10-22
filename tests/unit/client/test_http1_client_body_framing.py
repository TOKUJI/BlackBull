"""The response body reader: aligned to the next response and bounded by size.

The total is on the buffering entry point only: ``stream()`` promises that a
large response need not fit in memory.  Chunk grammar is a row of
tests/conformance/http1/test_framing_both_roles.py.
"""
from __future__ import annotations

import pytest

from blackbull.client.exceptions import ResponseTooLarge
from blackbull.client.http1 import HTTP1ResponseRecipient
from tests.harness import fed


def _reader(wire: bytes):
    return fed(wire).reader


def _chunked(*chunks: bytes, trailers: bytes = b'') -> bytes:
    body = b''.join(b'%x\r\n%s\r\n' % (len(c), c) for c in chunks)
    return (b'HTTP/1.1 200 OK\r\ntransfer-encoding: chunked\r\n\r\n'
            + body + b'0\r\n' + trailers + b'\r\n')


class TestBodylessStatuses:
    @pytest.mark.asyncio
    async def test_a_205_body_is_consumed_so_the_next_response_stays_aligned(
            self):
        """RFC 9110 §15.3.6 forbids a server to generate content in a 205,
        but RFC 9112 §6.3 still frames one. Calling it bodyless would leave
        those octets in the buffer for the next response to be misparsed
        as — the desync this reader exists to avoid."""
        reader = _reader(
            b'HTTP/1.1 205 Reset Content\r\ncontent-length: 3\r\n\r\nabc'
            b'HTTP/1.1 200 OK\r\ncontent-length: 2\r\n\r\nhi')
        first = await HTTP1ResponseRecipient().receive(reader)
        assert (first.status, first.body) == (205, b'abc')
        second = await HTTP1ResponseRecipient().receive(reader)
        assert (second.status, second.body) == (200, b'hi')


class TestTrailerSection:
    @pytest.mark.asyncio
    async def test_trailers_do_not_desync_the_next_response(self):
        """Two pipelined responses; the first carries a two-field trailer
        section.  Reading one line of it left the rest for the next status
        line."""
        first = _chunked(b'hi', trailers=b'x-a: 1\r\nx-b: 2\r\n')
        second = b'HTTP/1.1 204 No Content\r\n\r\n'
        reader = _reader(first + second)
        recipient = HTTP1ResponseRecipient()

        one = await recipient.receive(reader)
        assert one.body == b'hi'
        two = await recipient.receive(reader)
        assert two.status == 204, 'the second response began inside the trailers'

    @pytest.mark.asyncio
    async def test_an_empty_trailer_section_still_works(self):
        res = await HTTP1ResponseRecipient().receive(_reader(_chunked(b'hi')))
        assert res.body == b'hi'


class TestBodyBounds:
    @pytest.mark.asyncio
    async def test_a_chunked_body_over_the_total_is_refused(self, monkeypatch):
        monkeypatch.setenv('BB_CLIENT_BODY_MAX_TOTAL', '32')
        wire = _chunked(b'a' * 20, b'b' * 20)
        with pytest.raises(ResponseTooLarge):
            await HTTP1ResponseRecipient().receive(_reader(wire))

    @pytest.mark.asyncio
    async def test_a_declared_body_over_the_total_is_refused(self, monkeypatch):
        monkeypatch.setenv('BB_CLIENT_BODY_MAX_TOTAL', '4')
        wire = b'HTTP/1.1 200 OK\r\ncontent-length: 10\r\n\r\n' + b'x' * 10
        with pytest.raises(ResponseTooLarge):
            await HTTP1ResponseRecipient().receive(_reader(wire))

    @pytest.mark.asyncio
    async def test_streaming_is_not_capped_by_the_buffering_total(self, monkeypatch):
        """``stream()`` promises a large response need not fit in memory."""
        monkeypatch.setenv('BB_CLIENT_BODY_MAX_TOTAL', '32')
        wire = _chunked(b'a' * 20, b'b' * 20)
        out = b''.join([c async for c in
                        HTTP1ResponseRecipient().stream(_reader(wire))])
        assert out == b'a' * 20 + b'b' * 20

    @pytest.mark.asyncio
    async def test_an_oversized_chunk_size_line_is_refused(self, monkeypatch):
        monkeypatch.setenv('BB_CLIENT_HEAD_MAX_LINE', '32')
        wire = (b'HTTP/1.1 200 OK\r\ntransfer-encoding: chunked\r\n\r\n'
                b'5;ext=' + b'a' * 200 + b'\r\nhello\r\n0\r\n\r\n')
        with pytest.raises(ResponseTooLarge):
            await HTTP1ResponseRecipient().receive(_reader(wire))

    @pytest.mark.asyncio
    async def test_a_large_chunk_is_delivered_in_slices(self):
        """The unit column on the streaming path: one peer-declared chunk must
        not become one allocation."""
        big = b'z' * (200 * 1024)
        pieces = [c async for c in
                  HTTP1ResponseRecipient().stream(_reader(_chunked(big)))]
        assert b''.join(pieces) == big
        assert len(pieces) > 1, 'the whole chunk materialised in one read'


