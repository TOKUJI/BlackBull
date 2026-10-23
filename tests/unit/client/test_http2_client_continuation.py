"""A field block split across CONTINUATION must be reassembled and decoded,
because HPACK state is connection-wide.

RFC 9113 §4.3 does not leave this to preference:

    An endpoint receiving HEADERS, PUSH_PROMISE, or CONTINUATION frames needs
    to reassemble field blocks and perform decompression **even if the frames
    are to be discarded**.  A receiver MUST terminate the connection with a
    connection error of type COMPRESSION_ERROR if it does not decompress a
    field block.

The client did neither.  ``Headers.parse_payload()`` runs only under
``if self.end_headers``, and CONTINUATION reached ``_NullResponder``, whose own
docstring said so.  So the decoder never advanced over those instructions and
**every later HEADERS on the connection decoded against a stale dynamic
table** — silently, producing wrong header values rather than an error.  That
is the desync class this client has closed four times from other directions,
and the test that names it is ``test_the_next_response_still_decodes``: the
observable is not the split response, it is the *next* one.

It needs no hostile peer.  A conforming server MUST split a field block that
exceeds the peer's ``SETTINGS_MAX_FRAME_SIZE`` — 16 KiB by default — which a
large cookie, a long CSP or a handful of ``Set-Cookie`` fields reaches.

Reassembly creates a buffer fed entirely by the peer, so the bound lands with
the fix.  It invents no name: the HTTP/2 field block is the HTTP/2 spelling of
"the head", so ``BB_CLIENT_HEAD_MAX_TOTAL`` bounds the accumulation and
``BB_CLIENT_HEAD_TIMEOUT`` bounds the wait for END_HEADERS — the same two the
HTTP/1.1 head already answers to, and the same two the server spends on its
own side of this frame.
"""
from __future__ import annotations

import asyncio

import pytest
from hpack import Encoder

from blackbull.client.http2 import HTTP2Client, _PendingResponse
from blackbull.protocol.frame_types import ErrorCodes, FrameTypes
from blackbull.server.recipient import AbstractReader
from blackbull.server.sender import AbstractWriter

# A bound that does not fire presents as a hang, so every test carries a
# deadline: red must be reported, not waited on.
pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(10)]

_HEADERS = 0x1
_DATA = 0x0
_PUSH_PROMISE = 0x5
_CONTINUATION = 0x9
_END_STREAM = 0x1
_END_HEADERS = 0x4
_PADDED = 0x8
_PRIORITY = 0x20


def _frame(type_: int, flags: int, stream_id: int, payload: bytes) -> bytes:
    """One frame on the wire — length, type, flags, stream id, payload."""
    return (len(payload).to_bytes(3, 'big') + bytes([type_, flags])
            + stream_id.to_bytes(4, 'big') + payload)


class _Wire(AbstractReader):
    """Delivers scripted bytes, then stays connected and says nothing.

    Silence rather than EOF: EOF ends the receive loop for a reason that is
    not the one under test, so a missing deadline would present as a pass.
    """

    def __init__(self, payload: bytes = b'') -> None:
        self._buf, self._pos = bytearray(payload), 0
        self.consumed = 0

    def feed(self, data: bytes) -> None:
        self._buf += data

    async def readexactly(self, n: int) -> bytes:
        while len(self._buf) - self._pos < n:
            await asyncio.sleep(0.005)
        out = bytes(self._buf[self._pos:self._pos + n])
        self._pos += n
        self.consumed += n
        return out

    async def read(self, n: int = -1) -> bytes:
        return await self.readexactly(max(n, 0))


class _Paced(AbstractReader):
    """Delivers *first*, then one *repeat* per *gap* seconds, forever.

    A pre-buffered wire cannot tell an absolute deadline from a per-frame one:
    with every frame already in the buffer no time passes while they are
    consumed, so both clocks measure only the idle tail and a per-frame timer
    passes the test it was supposed to fail.  This one makes the frames arrive
    the way a peer sends them, each gap comfortably inside the deadline.
    """

    def __init__(self, first: bytes, repeat: bytes, gap: float) -> None:
        self._buf, self._pos = bytearray(first), 0
        self._repeat, self._gap = repeat, gap

    async def readexactly(self, n: int) -> bytes:
        while len(self._buf) - self._pos < n:
            await asyncio.sleep(self._gap)
            self._buf += self._repeat
        out = bytes(self._buf[self._pos:self._pos + n])
        self._pos += n
        return out

    async def read(self, n: int = -1) -> bytes:
        return await self.readexactly(max(n, 0))


class _NullWriter(AbstractWriter):
    async def write(self, data: bytes) -> None:
        pass


def _client(wire: _Wire) -> HTTP2Client:
    c = HTTP2Client('localhost', 1)
    c._reader = wire
    c._writer = _NullWriter()
    c.sent = []                                      # type: ignore[attr-defined]

    async def _capture(frame):
        c.sent.append(frame)                         # type: ignore[attr-defined]

    c._control_sender = _capture
    return c


def _pending(c: HTTP2Client, stream_id: int = 1) -> asyncio.Future:
    future = asyncio.get_running_loop().create_future()
    c._open_stream(stream_id)
    c._responses[stream_id] = _PendingResponse(future=future)
    return future


def _split_block(encoder: Encoder, fields: list[tuple[str, str]],
                 at: int) -> tuple[bytes, bytes]:
    """One HPACK block, cut in two — the shape a conforming peer sends when
    the block is larger than the peer's MAX_FRAME_SIZE."""
    block = encoder.encode(fields)
    assert 0 < at < len(block), 'the cut must land inside the block'
    return block[:at], block[at:]


def _sent(c: HTTP2Client, kind) -> list:
    return [f for f in c.sent if f.FrameType() == kind]   # type: ignore[attr-defined]


async def _drive(c: HTTP2Client, *, until=None, seconds: float = 2.0) -> None:
    """Run the receive loop until it ends, or until *until* is true.

    It cannot simply be awaited: the wait for a frame to *begin* is unbounded
    by design — server streaming and long polling are a peer behaving
    correctly — so a loop that is working correctly never returns on a quiet
    wire.  A connection error ends it; a successful response does not.
    """
    task = asyncio.create_task(c._receive_loop())
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    try:
        while True:
            if task.done() or (until is not None and until()):
                return
            if loop.time() > deadline:
                raise AssertionError(
                    'the receive loop neither ended nor met the condition')
            await asyncio.sleep(0.005)
    finally:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


async def _resolved(future, seconds: float = 2.0):
    return await asyncio.wait_for(asyncio.shield(future), seconds)


# ----------------------------------------------------------------------
# The correctness half: the block is reassembled and decoded
# ----------------------------------------------------------------------

class TestReassembly:
    async def test_a_split_field_block_is_decoded(self):
        enc = Encoder()
        head, tail = _split_block(
            enc, [(':status', '200'), ('x-a', 'one'), ('x-b', 'two')], at=4)
        wire = _Wire(_frame(_HEADERS, _END_STREAM, 1, head)
                     + _frame(_CONTINUATION, _END_HEADERS, 1, tail))
        c = _client(wire)
        future = _pending(c)

        await _drive(c, until=future.done)
        response = await _resolved(future)

        assert response.status == 200
        assert dict(response.headers) == {b'x-a': b'one', b'x-b': b'two'}

    async def test_the_next_response_still_decodes(self):
        """The defect's real shape.  HPACK's dynamic table is connection-wide,
        so a block the decoder never walked leaves every later block decoding
        against a table that is missing its insertions — wrong values, no
        error.  A split *first* response is what makes the *second* wrong."""
        enc = Encoder()
        head, tail = _split_block(
            enc, [(':status', '200'), ('x-secret', 'first-value')], at=3)
        # The second response repeats the field, so a conforming encoder
        # references the dynamic-table entry the first block inserted.  A
        # decoder that skipped the first block cannot resolve that reference.
        second = enc.encode([(':status', '200'), ('x-secret', 'first-value')])
        wire = _Wire(_frame(_HEADERS, _END_STREAM, 1, head)
                     + _frame(_CONTINUATION, _END_HEADERS, 1, tail)
                     + _frame(_HEADERS, _END_STREAM | _END_HEADERS, 3, second))
        c = _client(wire)
        one, two = _pending(c, 1), _pending(c, 3)

        await _drive(c, until=lambda: one.done() and two.done())

        assert dict((await _resolved(one)).headers) == {b'x-secret': b'first-value'}
        assert dict((await _resolved(two)).headers) == {b'x-secret': b'first-value'}, \
            'the second response decoded against a stale dynamic table'

    async def test_several_continuations_are_all_absorbed(self):
        enc = Encoder()
        block = enc.encode([(':status', '200'), ('x-a', 'v' * 200)])
        cuts = [block[i:i + 20] for i in range(0, len(block), 20)]
        wire = _Wire(_frame(_HEADERS, _END_STREAM, 1, cuts[0])
                     + b''.join(_frame(_CONTINUATION, 0, 1, c) for c in cuts[1:-1])
                     + _frame(_CONTINUATION, _END_HEADERS, 1, cuts[-1]))
        c = _client(wire)
        future = _pending(c)

        await _drive(c, until=future.done)
        assert dict((await _resolved(future)).headers) == {b'x-a': b'v' * 200}

    async def test_the_response_waits_for_end_headers(self):
        """END_STREAM on the HEADERS does not end the message: the field
        section is not complete until END_HEADERS, so resolving on the first
        frame hands the caller a response whose headers are still arriving."""
        enc = Encoder()
        head, tail = _split_block(enc, [(':status', '200'), ('x-a', 'one')], at=3)
        wire = _Wire(_frame(_HEADERS, _END_STREAM, 1, head))
        c = _client(wire)
        future = _pending(c)
        task = asyncio.create_task(c._receive_loop())
        try:
            await asyncio.sleep(0.05)
            assert not future.done(), \
                'resolved before the field section was complete'
            wire.feed(_frame(_CONTINUATION, _END_HEADERS, 1, tail))
            assert dict((await _resolved(future)).headers) == {b'x-a': b'one'}
        finally:
            task.cancel()


# ----------------------------------------------------------------------
# RFC 9113 §6.10 — nothing may interleave with an open header block
# ----------------------------------------------------------------------

# ----------------------------------------------------------------------
# The bound that lands with the fix — no new name
# ----------------------------------------------------------------------

class TestTheHeadDeadline:
    async def test_a_stalled_header_block_ends_the_connection(self, monkeypatch):
        """The peer opens a block and stops.  ``_receive_frame``'s wait for a
        frame to *begin* is deliberately unbounded — server streaming and long
        polling are the peer behaving correctly — but a peer that owes
        CONTINUATION is not idle, it has abandoned a message mid-delivery."""
        monkeypatch.setenv('BB_CLIENT_HEAD_TIMEOUT', '0.2')
        enc = Encoder()
        head, _ = _split_block(enc, [(':status', '200'), ('x-a', 'one')], at=3)
        c = _client(_Wire(_frame(_HEADERS, 0, 1, head)))
        future = _pending(c)

        await _drive(c, seconds=2.0)

        with pytest.raises(Exception):
            await _resolved(future)

    async def test_an_empty_continuation_flood_is_caught_by_the_clock(
            self, monkeypatch):
        """Zero-length CONTINUATION — CVE-2019-9518's shape — adds nothing to
        the total, so the byte budget never fires however many arrive.  The
        deadline is what owns this one, which is why it runs from when the
        block opened rather than from the last frame: re-arming per frame is
        what a peer sending empty frames forever would satisfy."""
        monkeypatch.setenv('BB_CLIENT_HEAD_TIMEOUT', '0.3')
        enc = Encoder()
        head, _ = _split_block(enc, [(':status', '200'), ('x-a', 'one')], at=3)
        # One empty CONTINUATION every 0.05s: each gap is a sixth of the
        # deadline, so a clock re-armed per frame never fires and this test
        # would pass against the very design it names.
        c = _client(_Paced(_frame(_HEADERS, 0, 1, head),
                           _frame(_CONTINUATION, 0, 1, b''), gap=0.05))
        future = _pending(c)

        await _drive(c, seconds=3.0)

        with pytest.raises(Exception):
            await _resolved(future)

    async def test_a_block_that_completes_in_time_is_not_refused(self, monkeypatch):
        monkeypatch.setenv('BB_CLIENT_HEAD_TIMEOUT', '2.0')
        enc = Encoder()
        head, tail = _split_block(enc, [(':status', '200'), ('x-a', 'one')], at=3)
        wire = _Wire(_frame(_HEADERS, _END_STREAM, 1, head))
        c = _client(wire)
        future = _pending(c)
        task = asyncio.create_task(c._receive_loop())
        try:
            await asyncio.sleep(0.1)
            wire.feed(_frame(_CONTINUATION, _END_HEADERS, 1, tail))
            assert dict((await _resolved(future)).headers) == {b'x-a': b'one'}
        finally:
            task.cancel()


# ----------------------------------------------------------------------
# RFC 9113 §4.3 names three frame types, and §6.10 names two that open a
# block.  PUSH_PROMISE is in both lists.
# ----------------------------------------------------------------------

def _promise(promised_id: int, block: bytes) -> bytes:
    return promised_id.to_bytes(4, 'big') + block


class TestPushPromise:
    async def test_a_promise_advances_the_decoder(self):
        """The client acts on no push, but §4.3 requires the block decoded
        *anyway*: the dynamic table advances as a side effect of reading it.
        Dropping the promise undecoded left the next response decoding against
        a table missing its insertions."""
        enc = Encoder()
        promise = _promise(2, enc.encode(
            [(':method', 'GET'), (':path', '/p.css'), ('x-mark', 'from-push')]))
        answer = enc.encode([(':status', '200'), ('x-mark', 'from-push')])
        wire = _Wire(_frame(_PUSH_PROMISE, _END_HEADERS, 1, promise)
                     + _frame(_HEADERS, _END_STREAM | _END_HEADERS, 1, answer))
        c = _client(wire)
        future = _pending(c)

        await _drive(c, until=future.done)
        assert dict((await _resolved(future)).headers) == {b'x-mark': b'from-push'}

    async def test_a_split_promise_does_not_end_the_connection(self):
        """§6.10 lets PUSH_PROMISE open a block, so its CONTINUATION is
        conforming.  Treating only HEADERS as an opener made that CONTINUATION
        look unsolicited and killed the connection — taking the real response
        on the other stream with it."""
        enc = Encoder()
        block = enc.encode([(':method', 'GET'), (':path', '/p.css'),
                            ('x-mark', 'from-push')])
        promise = _promise(2, block)
        answer = enc.encode([(':status', '200'), ('x-mark', 'from-push')])
        wire = _Wire(_frame(_PUSH_PROMISE, 0, 1, promise[:6])
                     + _frame(_CONTINUATION, _END_HEADERS, 1, promise[6:])
                     + _frame(_HEADERS, _END_STREAM | _END_HEADERS, 1, answer))
        c = _client(wire)
        future = _pending(c)

        await _drive(c, until=future.done)

        assert not _sent(c, FrameTypes.GOAWAY), \
            'a conforming split promise ended the connection'
        assert dict((await _resolved(future)).headers) == {b'x-mark': b'from-push'}

    async def test_a_padded_promise_finds_the_promised_stream(self):
        """§6.6 puts the pad-length octet *before* the promised stream id."""
        from blackbull.protocol.frame import FrameFactory

        enc = Encoder()
        block = enc.encode([(':method', 'GET'), (':path', '/p.css')])
        payload = bytes([4]) + _promise(7, block) + b'\x00' * 4
        frame = FrameFactory().load(
            _frame(_PUSH_PROMISE, _END_HEADERS | _PADDED, 1, payload))
        assert frame.promised_stream_id == 7


# ----------------------------------------------------------------------
# RFC 9113 §6.2 — padding belongs to the frame that carried the flag
# ----------------------------------------------------------------------

# ----------------------------------------------------------------------
# §4.3's other half: what happens when the block cannot be decoded
# ----------------------------------------------------------------------

