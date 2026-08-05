"""The receive side is native: a chunk is ``bytes``, and the end is ``None``.

`Connection` is the parsed request head; the body chunk is the other half of
the same message.  They were in different worlds — the head native, the body an
ASGI ``http.request`` dict that the recipient built and ``read_body`` took apart
one frame later.  The dict was built per chunk, on *every* handler form,
including the two that never look at one.

So the framing produces a value and the boundary moves into the call protocol:

    chunk = await recipient.next_chunk()     # bytes  → more body
    chunk is None                            # end of body
    raise ClientDisconnected                 # peer went away mid-body

``None`` rather than ``b''`` for the same reason ``NativeResponse`` decides
presence with ``is not None``: an empty body is a real body, and one rule in
both directions is worth more than the sentinel.

``receive()`` is unchanged and still returns the ASGI dicts — it mints them per
call now, so the caller who wants the ASGI encoding is the one who pays for it.
Every test that asserts the dict sequence here is guarding that compat surface.
"""
import asyncio

import pytest

from blackbull.connection import Connection
from blackbull.headers import Headers
from blackbull.protocol.frame_types import Data, DataFrameFlags, FrameTypes
from blackbull.request import ClientDisconnected
from blackbull.server.recipient import (
    AbstractReader, AsyncioReader, HTTP1Recipient, HTTP2Recipient,
    IncompleteReadError,
)


class _Source:
    """A byte stream that can run out mid-read."""

    def __init__(self, data: bytes = b''):
        self._d = bytearray(data)

    async def read(self, n: int = -1) -> bytes:
        out = bytes(self._d[:n]) if n >= 0 else bytes(self._d)
        del self._d[:len(out)]
        return out

    async def readexactly(self, n: int) -> bytes:
        if len(self._d) < n:
            out = bytes(self._d)
            self._d.clear()
            raise IncompleteReadError(out)
        out = bytes(self._d[:n])
        del self._d[:n]
        return out

    async def readuntil(self, sep: bytes = b'\n') -> bytes:
        i = self._d.find(sep)
        if i == -1:
            out = bytes(self._d)
            self._d.clear()
            raise IncompleteReadError(out)
        end = i + len(sep)
        out = bytes(self._d[:end])
        del self._d[:end]
        return out


def _conn(headers, path: str = '/p') -> Connection:
    return Connection(method='POST', path=path, raw_path=path.encode(),
                      headers=Headers(headers), type='http')


def _h1(wire: bytes, headers, **kw) -> HTTP1Recipient:
    return HTTP1Recipient(AsyncioReader(_Source(wire)), _conn(headers), **kw)


async def _drain_native(recipient) -> list[bytes]:
    """Every chunk the native channel yields, up to (not including) the end."""
    out = []
    while (chunk := await recipient.next_chunk()) is not None:
        out.append(chunk)
    return out


async def _drain_asgi(recipient) -> list[dict]:
    """Every event ``receive()`` yields, up to and including the terminal one."""
    out = []
    while True:
        event = await recipient()
        out.append(event)
        if event['type'] == 'http.disconnect' or not event.get('more_body'):
            return out


# ---------------------------------------------------------------------------
# HTTP/1.1 — the native channel
# ---------------------------------------------------------------------------

class TestHTTP1NativeChannel:

    @pytest.mark.asyncio
    async def test_content_length_yields_chunks_then_none(self):
        r = _h1(b'0123456789', [(b'content-length', b'10')], chunk_size=4)
        assert await _drain_native(r) == [b'0123', b'4567', b'89']

    @pytest.mark.asyncio
    async def test_end_is_none_not_empty_bytes(self):
        """``b''`` would be indistinguishable from a real empty chunk."""
        r = _h1(b'hi', [(b'content-length', b'2')], chunk_size=64)
        assert await r.next_chunk() == b'hi'
        assert await r.next_chunk() is None

    @pytest.mark.asyncio
    async def test_bodyless_request_ends_immediately(self):
        r = _h1(b'', [], chunk_size=64)
        assert await r.next_chunk() is None

    @pytest.mark.asyncio
    async def test_zero_content_length_ends_immediately(self):
        r = _h1(b'', [(b'content-length', b'0')], chunk_size=64)
        assert await r.next_chunk() is None

    @pytest.mark.asyncio
    async def test_chunked_yields_each_chunk_then_none(self):
        wire = b'5\r\nhello\r\n5\r\nworld\r\n0\r\n\r\n'
        r = _h1(wire, [(b'transfer-encoding', b'chunked')])
        assert await _drain_native(r) == [b'hello', b'world']

    @pytest.mark.asyncio
    async def test_end_is_idempotent(self):
        """A caller that asks again past the end gets the end again."""
        r = _h1(b'', [], chunk_size=64)
        assert await r.next_chunk() is None
        assert await r.next_chunk() is None

    @pytest.mark.asyncio
    async def test_peer_vanishing_mid_body_raises(self):
        """EOF mid-body is not end-of-body — a truncated upload must not read
        as a complete one."""
        r = _h1(b'abc', [(b'content-length', b'10')], chunk_size=64)
        with pytest.raises(ClientDisconnected):
            await r.next_chunk()

    @pytest.mark.asyncio
    async def test_body_timeout_raises(self):
        class _Stalled(AbstractReader):
            async def readexactly(self, n):
                raise asyncio.TimeoutError()

            async def read(self, n=-1):
                raise asyncio.TimeoutError()

            async def readuntil(self, sep=b'\n'):
                raise asyncio.TimeoutError()

        r = HTTP1Recipient(_Stalled(), _conn([(b'content-length', b'5')]),
                           body_timeout=0.01, chunk_size=64)
        with pytest.raises(ClientDisconnected):
            await r.next_chunk()


# ---------------------------------------------------------------------------
# HTTP/1.1 — the ASGI compat surface is byte-identical
# ---------------------------------------------------------------------------

class TestHTTP1ASGICompat:

    @pytest.mark.asyncio
    async def test_content_length_event_sequence_unchanged(self):
        r = _h1(b'0123456789', [(b'content-length', b'10')], chunk_size=4)
        assert await _drain_asgi(r) == [
            {'type': 'http.request', 'body': b'0123', 'more_body': True},
            {'type': 'http.request', 'body': b'4567', 'more_body': True},
            {'type': 'http.request', 'body': b'89', 'more_body': False},
        ]

    @pytest.mark.asyncio
    async def test_chunked_event_sequence_unchanged(self):
        wire = b'5\r\nhello\r\n5\r\nworld\r\n0\r\n\r\n'
        r = _h1(wire, [(b'transfer-encoding', b'chunked')])
        assert await _drain_asgi(r) == [
            {'type': 'http.request', 'body': b'hello', 'more_body': True},
            {'type': 'http.request', 'body': b'world', 'more_body': True},
            {'type': 'http.request', 'body': b'', 'more_body': False},
        ]

    @pytest.mark.asyncio
    async def test_bodyless_event_sequence_unchanged(self):
        r = _h1(b'', [], chunk_size=64)
        assert await r() == {'type': 'http.request', 'body': b'',
                             'more_body': False}
        assert await r() == {'type': 'http.disconnect'}

    @pytest.mark.asyncio
    async def test_mid_body_eof_surfaces_as_disconnect_event(self):
        """The exception is the *native* signal; ``receive()`` keeps the dict."""
        r = _h1(b'abc', [(b'content-length', b'10')], chunk_size=64)
        assert await r() == {'type': 'http.disconnect'}

    @pytest.mark.asyncio
    async def test_the_two_channels_share_end_state(self):
        """H1's ``__call__`` drives ``next_chunk``, so the end is shared."""
        r = _h1(b'hi', [(b'content-length', b'2')], chunk_size=64)
        assert await r() == {'type': 'http.request', 'body': b'hi',
                             'more_body': False}
        assert await r.next_chunk() is None

    @pytest.mark.asyncio
    async def test_both_channels_carry_the_same_bytes(self):
        wire = b'0123456789'
        native = await _drain_native(
            _h1(wire, [(b'content-length', b'10')], chunk_size=3))
        events = await _drain_asgi(
            _h1(wire, [(b'content-length', b'10')], chunk_size=3))
        assert b''.join(native) == b''.join(e['body'] for e in events) == wire


# ---------------------------------------------------------------------------
# HTTP/2
# ---------------------------------------------------------------------------

def _data(payload: bytes, *, end: bool) -> Data:
    flags = DataFrameFlags.END_STREAM if end else 0
    return Data(len(payload), FrameTypes.DATA, flags, 1, data=payload)


class TestHTTP2NativeChannel:

    @pytest.mark.asyncio
    async def test_data_frames_yield_chunks_then_none(self):
        r = HTTP2Recipient()
        r.put_DATAFrame(_data(b'hello', end=False))
        r.put_DATAFrame(_data(b'world', end=True))
        assert await _drain_native(r) == [b'hello', b'world']

    @pytest.mark.asyncio
    async def test_end_of_stream_on_headers_ends_immediately(self):
        r = HTTP2Recipient()
        r.mark_end_of_stream_on_headers()
        assert await r.next_chunk() is None

    @pytest.mark.asyncio
    async def test_disconnect_raises(self):
        r = HTTP2Recipient()
        r.put_DATAFrame(_data(b'partial', end=False))
        r.put_disconnect()
        assert await r.next_chunk() == b'partial'
        with pytest.raises(ClientDisconnected):
            await r.next_chunk()

    @pytest.mark.asyncio
    async def test_asgi_event_sequence_unchanged(self):
        r = HTTP2Recipient()
        r.put_DATAFrame(_data(b'hello', end=False))
        r.put_DATAFrame(_data(b'world', end=True))
        assert await _drain_asgi(r) == [
            {'type': 'http.request', 'body': b'hello', 'more_body': True},
            {'type': 'http.request', 'body': b'world', 'more_body': False},
        ]

    @pytest.mark.asyncio
    async def test_the_two_channels_share_end_state(self):
        """A reader that starts on one channel must not hang on the other.

        ``__call__`` does not *consult* the end marker — a full-form handler
        calling ``receive()`` past END_STREAM still waits for the disconnect
        event, as it always did — but it must still *set* it, or a later
        ``conn.body()`` blocks on a queue that will never be fed again.
        """
        r = HTTP2Recipient()
        r.put_DATAFrame(_data(b'hello', end=True))
        assert await r() == {'type': 'http.request', 'body': b'hello',
                             'more_body': False}
        assert await asyncio.wait_for(r.next_chunk(), timeout=1) is None

    @pytest.mark.asyncio
    async def test_the_two_channels_share_end_state_on_headers(self):
        r = HTTP2Recipient()
        r.mark_end_of_stream_on_headers()
        assert await r() == {'type': 'http.request', 'body': b'',
                             'more_body': False}
        assert await asyncio.wait_for(r.next_chunk(), timeout=1) is None

    @pytest.mark.asyncio
    async def test_consume_time_credit_still_replays(self):
        """Flow control is replayed when the app *pops*, on either channel."""
        credited: list[int] = []

        async def credit(n: int) -> None:
            credited.append(n)

        r = HTTP2Recipient(credit_callback=credit)
        r.put_DATAFrame(_data(b'x' * 100, end=True))
        assert await r.next_chunk() == b'x' * 100
        assert credited == [100]
        assert r.take_uncredited() == 0


# ---------------------------------------------------------------------------
# Connection.body() / stream() ride the native channel
# ---------------------------------------------------------------------------

class TestConnectionUsesTheNativeChannel:

    @pytest.mark.asyncio
    async def test_body_reads_the_whole_payload(self):
        conn = _conn([(b'content-length', b'10')])
        conn._receive = HTTP1Recipient(
            AsyncioReader(_Source(b'0123456789')), conn, chunk_size=4)
        assert await conn.body() == b'0123456789'

    @pytest.mark.asyncio
    async def test_stream_yields_chunks(self):
        conn = _conn([(b'content-length', b'10')])
        conn._receive = HTTP1Recipient(
            AsyncioReader(_Source(b'0123456789')), conn, chunk_size=4)
        assert [c async for c in conn.stream()] == [b'0123', b'4567', b'89']

    @pytest.mark.asyncio
    async def test_truncated_upload_raises_with_the_partial(self):
        conn = _conn([(b'content-length', b'10')])
        conn._receive = HTTP1Recipient(
            AsyncioReader(_Source(b'0123')), conn, chunk_size=4)
        with pytest.raises(ClientDisconnected) as exc:
            await conn.body()
        assert exc.value.partial == b'0123'

    @pytest.mark.asyncio
    async def test_external_asgi_receive_still_works(self):
        """Under uvicorn the channel is a plain callable with no native arm."""
        events = [
            {'type': 'http.request', 'body': b'ab', 'more_body': True},
            {'type': 'http.request', 'body': b'cd', 'more_body': False},
        ]

        async def receive():
            return events.pop(0)

        conn = _conn([])
        conn._receive = receive
        assert await conn.body() == b'abcd'

    @pytest.mark.asyncio
    async def test_native_body_read_builds_no_asgi_dicts(self):
        """The point of the whole exercise, asserted rather than described.

        ``conn.body()`` used to cost one ``http.request`` dict per chunk — 16
        for a 64 KiB upload at the 4 KiB default — none of which it read.
        """
        conn = _conn([(b'content-length', b'10')])
        recipient = HTTP1Recipient(
            AsyncioReader(_Source(b'0123456789')), conn, chunk_size=2)

        built = 0
        original = type(recipient).__call__

        async def counting(self):
            nonlocal built
            built += 1
            return await original(self)

        type(recipient).__call__ = counting
        try:
            conn._receive = recipient
            assert await conn.body() == b'0123456789'
        finally:
            type(recipient).__call__ = original

        assert built == 0, f'conn.body() went through receive() {built} times'
