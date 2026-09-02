"""The chunk-size element, by RFC 9112's grammar and the server's reading of it.

Two MUSTs, in opposite directions, both missed the first time.

``rstrip(_CRLF)`` strips *every* trailing CR/LF, so a bare CR inside the
element was silently deleted and the line parsed as though it were clean.
RFC 9112 §2.2 gives a recipient two choices for a bare CR — treat the element
as invalid, or replace it with SP — and a replaced SP leaves ``5 ``, which is
not ``1*HEXDIG``.  Refusal is the only conforming outcome either way.

The other direction: ``chunk-ext = *( BWS ";" BWS chunk-ext-name … )``, and
RFC 9110 §5.6.3 says a recipient ``MUST parse for such bad whitespace and
remove it``.  The client refused ``5 ;x=y`` — wire its own server accepts.

The tests assert against the server's parser as the oracle wherever both sides
are meant to agree, because "the client is stricter than our own server on a
grammar production" is the shape of the bug.
"""
from __future__ import annotations

import pytest

from blackbull.client.exceptions import ProtocolError
from blackbull.client.http1 import HTTP1ResponseRecipient
from blackbull.server.recipient import AbstractReader
from blackbull.server.recipient import _parse_chunk_size as _server_parse

_HEAD = b'HTTP/1.1 200 OK\r\ntransfer-encoding: chunked\r\n\r\n'


class _Canned(AbstractReader):
    def __init__(self, payload: bytes) -> None:
        self._buf, self._pos = payload, 0

    async def read(self, n: int = -1) -> bytes:
        out = self._buf[self._pos:] if n < 0 else self._buf[self._pos:self._pos + n]
        self._pos += len(out)
        return out


async def _body(size_line: bytes) -> bytes:
    wire = _HEAD + size_line + b'AAAAA\r\n0\r\n\r\n'
    res = await HTTP1ResponseRecipient().receive(_Canned(wire))
    return res.body


def _server_accepts(size_line: bytes) -> bool:
    try:
        _server_parse(size_line)
        return True
    except Exception:
        return False


class TestRefused:
    @pytest.mark.parametrize('size_line, why', [
        (b'5\r\r\n',   'bare CR in the element (RFC 9112 2.2)'),
        (b'5\n\r\n',   'embedded LF'),
        (b'5\r\n\r\n', 'a second CRLF inside what was read as one line'),
        (b'5 \r\n',    'trailing whitespace with no chunk-ext — BWS needs a ";"'),
        (b'-5\r\n',    'a sign is not HEXDIG, and reached readexactly()'),
        (b'0x5\r\n',   'an 0x prefix is not HEXDIG'),
        (b'1_0\r\n',   'an underscore separator is not HEXDIG'),
        (b'\r\n',      'empty — chunk-size is 1*HEXDIG'),
    ])
    @pytest.mark.asyncio
    async def test_the_client_refuses(self, size_line, why):
        with pytest.raises(ProtocolError):
            await _body(size_line)

    @pytest.mark.parametrize('size_line', [
        b'5\r\r\n', b'5\n\r\n', b'5 \r\n', b'-5\r\n', b'0x5\r\n', b'1_0\r\n',
    ])
    def test_the_server_refuses_the_same(self, size_line):
        assert not _server_accepts(size_line), 'oracle disagrees'


class TestAccepted:
    @pytest.mark.parametrize('size_line', [
        b'5\r\n',
        b'5;name=value\r\n',
        b'5 ;name=value\r\n',      # BWS before ';' — 9110 5.6.3 MUST remove
        b'5\t;name=value\r\n',
        b'0000000000000005\r\n',   # no digit ceiling; 7.1 says *anticipate*
    ])
    @pytest.mark.asyncio
    async def test_the_client_accepts(self, size_line):
        assert await _body(size_line) == b'AAAAA'

    @pytest.mark.parametrize('size_line', [
        b'5\r\n', b'5;name=value\r\n', b'5 ;name=value\r\n',
        b'5\t;name=value\r\n', b'0000000000000005\r\n',
    ])
    def test_the_server_accepts_the_same(self, size_line):
        assert _server_accepts(size_line), 'oracle disagrees'


class TestTruncation:
    @pytest.mark.asyncio
    async def test_a_size_line_cut_off_at_eof_is_not_a_chunk_size(self):
        """``b'5'`` with no CRLF parsed as 5 under the unbounded default
        ``readuntil``, which returns what it has when the peer goes away."""
        with pytest.raises(Exception):
            wire = _HEAD + b'5'
            await HTTP1ResponseRecipient().receive(_Canned(wire))
