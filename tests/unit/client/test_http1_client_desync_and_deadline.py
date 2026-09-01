"""A refused response must not leave the connection readable, and a body read
must not wait forever.

Both are failures the bounds work introduced or left standing.

*Desync*: every refusal — an over-budget head, an over-budget body, a
malformed status line — leaves the rest of the message on the wire.  Reusing
the connection then parses that remainder as the *next* response, so a peer
whose body is itself a well-formed response gets one delivered for a request
the server answered differently.  It is the same shape the trailer fix closed,
reached from the refusal paths instead.

*Deadline*: the head has a timeout; the body had none at all, so a peer that
declared ten octets, sent three and stayed connected held the client forever.
A per-read deadline is what the server calls ``BB_BODY_TIMEOUT``, and it stops
a peer that **stops**.  It does not stop one that trickles — that is the rate
floor, which is not here yet.
"""
from __future__ import annotations

import asyncio

import pytest

from blackbull.client.exceptions import (ClientError, ConnectionError,
                                         ResponseTooLarge)
from blackbull.client.http1 import HTTP1Client, HTTP1ResponseRecipient
from blackbull.server.recipient import AbstractReader, AbstractWriter

#: A response body that is itself a complete, attacker-chosen response.
_INJECTED = b'HTTP/1.1 200 OK\r\nx-evil: yes\r\ncontent-length: 5\r\n\r\nPWNED'


class _Canned(AbstractReader):
    def __init__(self, payload: bytes) -> None:
        self._buf, self._pos = payload, 0

    async def read(self, n: int = -1) -> bytes:
        out = self._buf[self._pos:] if n < 0 else self._buf[self._pos:self._pos + n]
        self._pos += len(out)
        return out


class _Silent(AbstractReader):
    """Delivers a prefix, then stays connected and says nothing."""

    def __init__(self, payload: bytes) -> None:
        self._buf, self._pos = payload, 0

    async def read(self, n: int = -1) -> bytes:
        if self._pos < len(self._buf):
            out = self._buf[self._pos:] if n < 0 else self._buf[self._pos:self._pos + n]
            self._pos += len(out)
            return out
        await asyncio.Event().wait()


class _NullWriter(AbstractWriter):
    async def write(self, data: bytes) -> None:
        pass


async def _time_to_raise(coro, guard: float = 2.0) -> float:
    """Seconds until *coro* raises ``TimeoutError``, under an outer guard."""
    loop = asyncio.get_running_loop()
    started = loop.time()
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(coro, guard)
    return loop.time() - started


def _with_injected_body(head: bytes) -> bytes:
    return (head + b'content-length: %d\r\n\r\n' % len(_INJECTED)
            + _INJECTED + b'HTTP/1.1 204 No Content\r\n\r\n')


class TestRefusalPoisonsTheConnection:
    @pytest.mark.parametrize('env,value,head', [
        ('BB_CLIENT_HEAD_MAX_LINE', '64',
         b'HTTP/1.1 200 OK\r\nx-big: ' + b'v' * 200 + b'\r\n'),
        ('BB_CLIENT_HEAD_MAX_TOTAL', '48',
         b'HTTP/1.1 200 OK\r\nx-a: 1\r\nx-b: 2\r\nx-c: 3\r\nx-d: 4\r\nx-e: 5\r\n'),
    ])
    @pytest.mark.asyncio
    async def test_the_recipient_reports_broken_framing(self, monkeypatch,
                                                        env, value, head):
        monkeypatch.setenv(env, value)
        recipient = HTTP1ResponseRecipient()
        with pytest.raises(ResponseTooLarge):
            await recipient.receive(_Canned(_with_injected_body(head)))
        assert recipient.framing_broken, 'the stream position is unknown'

    @pytest.mark.asyncio
    async def test_a_malformed_status_line_also_breaks_framing(self):
        recipient = HTTP1ResponseRecipient()
        with pytest.raises(ClientError):
            await recipient.receive(_Canned(b'GARBAGE\r\n\r\n'))
        assert recipient.framing_broken

    @pytest.mark.asyncio
    async def test_the_client_refuses_to_read_past_a_refusal(self, monkeypatch):
        """The attack: the refused response's body is a response of its own."""
        monkeypatch.setenv('BB_CLIENT_HEAD_MAX_LINE', '64')
        client = HTTP1Client('localhost', 1)
        client._reader = _Canned(_with_injected_body(
            b'HTTP/1.1 200 OK\r\nx-big: ' + b'v' * 200 + b'\r\n'))
        client._writer = _NullWriter()

        with pytest.raises(ResponseTooLarge):
            await client.request('GET', '/')
        with pytest.raises(ConnectionError):
            await client.request('GET', '/second')


class TestBodyDeadline:
    @pytest.mark.parametrize('wire', [
        b'HTTP/1.1 200 OK\r\ncontent-length: 10\r\n\r\nabc',
        b'HTTP/1.1 200 OK\r\ntransfer-encoding: chunked\r\n\r\na\r\nabc',
        b'HTTP/1.1 200 OK\r\ntransfer-encoding: chunked\r\n\r\n',
    ], ids=['declared', 'chunked', 'no-terminator'])
    @pytest.mark.asyncio
    async def test_a_peer_that_stops_mid_body_is_abandoned(self, monkeypatch, wire):
        monkeypatch.setenv('BB_CLIENT_BODY_TIMEOUT', '0.05')
        elapsed = await _time_to_raise(
            HTTP1ResponseRecipient().receive(_Silent(wire)))
        # The outer guard raises the same type, so the type alone proves
        # nothing: what proves the setting fired is *when*.
        assert elapsed < 0.5, f'the body read was not bounded ({elapsed:.2f}s)'

    @pytest.mark.asyncio
    async def test_streaming_is_bounded_too(self, monkeypatch):
        monkeypatch.setenv('BB_CLIENT_BODY_TIMEOUT', '0.05')
        wire = b'HTTP/1.1 200 OK\r\ncontent-length: 100\r\n\r\nabc'

        async def drain():
            async for _ in HTTP1ResponseRecipient().stream(_Silent(wire)):
                pass

        elapsed = await _time_to_raise(drain())
        assert elapsed < 0.5, f'the body read was not bounded ({elapsed:.2f}s)'


class TestTruncationIsNamed:
    @pytest.mark.asyncio
    async def test_a_body_cut_short_at_eof_is_a_client_error(self):
        """``_stream_body``'s slice loop subtracted a zero-length read forever."""
        wire = b'HTTP/1.1 200 OK\r\ncontent-length: 100\r\n\r\n' + b'x' * 30

        async def drain():
            async for _ in HTTP1ResponseRecipient().stream(_Canned(wire)):
                pass

        with pytest.raises(ClientError):
            await asyncio.wait_for(drain(), 2.0)

    @pytest.mark.asyncio
    async def test_a_declared_body_cut_short_is_a_client_error(self):
        wire = b'HTTP/1.1 200 OK\r\ncontent-length: 100\r\n\r\n' + b'x' * 30
        with pytest.raises(ClientError):
            await HTTP1ResponseRecipient().receive(_Canned(wire))
