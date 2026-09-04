"""What ``BB_CLIENT_BODY_MAX_TOTAL`` costs in memory, pinned at the published numbers.

The cap counts **body octets**.  Reaching it costs ``2N + 121 bytes per
slice`` in peak traced memory, on both protocols and all three HTTP/1.1
framings, because every buffering path accumulates slices in a list and then
``b''.join`` them — at the join the slices and the joined result are both
live.  ``env.py`` and ``docs/reference/env-vars.md`` publish that so an
operator sizing the knob against a container memory limit sizes it against the
right number; this file is what stops the published numbers and the code from
drifting apart.

**Two claims, two tests, because they fail differently.**
``TestTheBufferedBodyCostsTwiceTheCap`` pins the headline ``~2x`` at the
peer write size the client itself reads at, 64 KiB.
``TestTheExcessIsPerSliceNotPerOctet`` pins the 121, which is what lets an
operator compute their own peer's number — 2.002 at 64 KiB writes, 2.030 at
4 KiB, 2.238 at 512 B — instead of trusting one reading taken at ours.

The band is ``[1.9, 2.2]`` and its job is to catch a **drift**.  It is a band
about *this fixture's* write size, not about any peer: a peer writing below
about 900 bytes exceeds 2.2 legitimately, which is exactly why the second test
exists.  ``tracemalloc`` counts octets requested from the allocator rather
than RSS, so the reading is arithmetic and not an allocator measurement: it
matches ``2 + 121/write`` to four decimal places from 512 B to 64 KiB, and is
identical on 3.11, 3.12, 3.13 and 3.14.

**What the band alone cannot catch, and what does.**  Accumulating into a
single ``bytearray`` and returning ``bytes(buf)`` — the obvious "fix", and the
one BLA-305 proposed — measures 2.03 to 2.13, *inside* this band, and is
worse than what is here at the write size that matters (2.072 against 2.002).
No threshold both tolerates a small-writing peer and rejects that.  The
per-slice test does reject it, because a bytearray strategy has no per-slice
cost at all and the recovered constant collapses to about zero — but that is
a consequence, not a guarantee, so the *reason* not to make the change is
written in ``env.py`` beside the code someone would edit.  Reaching ~1 N means
returning the buffer itself rather than a ``bytes``, which is a public API
change tracked as ``BLA-325``.

**Why the fixture manufactures a distinct object per read, and yields.**
``tracemalloc`` counts an object once however many references point at it, so
a fake reader that hands back the *same* slice every time reads ~1 N and every
assertion here passes vacuously; ``TestTheInstrumentIsNotBlind`` is the
control against that.  And a reader that never awaits anything real holds one
cancelled ``asyncio`` timer handle per read to the peak — 16,384 of them for
an 8 MiB body at 512-byte writes, which reads as 0.42 of body cost that no
body ever paid, because the loop only purges them when it gets a turn.  Both
fixtures therefore yield once per read, as a socket does.
"""
from __future__ import annotations

import asyncio
import logging
import tracemalloc
from collections import deque
from collections.abc import Awaitable, Iterable

import pytest

from blackbull.client.http1 import HTTP1ResponseRecipient
from blackbull.client.http2 import HTTP2Client, _PendingResponse
from blackbull.protocol.frame_types import FrameTypes
from blackbull.server.recipient import AbstractReader
from blackbull.server.sender import AbstractWriter

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(60)]

#: The cap under test, and the body delivered against it — exactly at the cap,
#: which is the only size at which the multiplier is what an operator sees.
#: Large enough that the fixed costs (head, framing, list overhead) are 0.2% of
#: it and the ratio is the accumulation and nothing else; small enough that all
#: six measurements in this file finish well inside a second.
_CAP = 8 * 1024 * 1024

#: How much the peer hands over per read, which is what fixes the slice count
#: and therefore the excess over 2x.  Equal to
#: ``blackbull.client.http1._STREAM_CHUNK_SIZE``, the most the client asks for
#: in one read, so the headline measurement is the cheapest a peer can make it.
#: Not imported from there: the fixture is the peer, and a peer's write size is
#: not ours to inherit.
_PEER_WRITE = 64 * 1024

#: A peer that writes in small pieces, for the shape below.
_SMALL_PEER_WRITE = 1024

#: Bytes of overhead per slice, independent of the slice's size: 41 for the
#: ``bytes`` object's header and its pointer in the list, 80 for the
#: ``Py_buffer`` scratch ``bytes.join`` builds one per item.  Measured constant
#: to within a byte from 256 B slices to 64 KiB, and identical on 3.11 to 3.14.
_PER_SLICE = 121

#: The published multiplier, with room for the slice size the peer chooses.
_BAND = (1.9, 2.2)

#: Two readings, two different diagnoses, and the second is the likely one.
_BLIND_CONTROL_FAILED = (
    'the shared-{kind} control read {ratio:.3f}, not ~1.  Around 1 means the '
    'harness is sound; around 2 means the accumulation now copies whether or '
    'not the slices are distinct — which is what a bytearray rewrite does, '
    'and is the change the band above cannot see.  Anything else means the '
    'instrument is counting something other than the body.')


class _ScriptedReader(AbstractReader):
    """Serves framing octets literally and manufactures body octets on demand.

    The body is never resident as a whole before the read.  A reader holding
    it would put N octets on the scale that no accumulation strategy can move,
    and the ratio would measure the fixture instead of the code.

    *share_one_slice* hands back the same object every read.  That is the
    blind-instrument control, not a peer: see the module docstring.
    """

    def __init__(self, script: Iterable[bytes | int], *,
                 write_size: int = _PEER_WRITE,
                 share_one_slice: bool = False) -> None:
        self._script: deque[bytes | int] = deque(script)
        self._literal = b''
        self._manufacture = 0
        self._write = write_size
        self._shared = bytes(write_size) if share_one_slice else None

    async def read(self, n: int = -1) -> bytes:
        # A real socket read yields, and that matters to what is measured:
        # ``_body_read`` opens an ``asyncio.timeout`` per read, and asyncio
        # purges the cancelled timer handles only when the loop gets a turn.
        # A reader that never yields holds one per read to the peak — 16,384
        # of them for an 8 MiB body at 512-byte writes, which reads as 0.42
        # of body cost that no body ever paid.
        await asyncio.sleep(0)
        while not self._literal and not self._manufacture:
            if not self._script:
                return b''
            item = self._script.popleft()
            if isinstance(item, int):
                self._manufacture = item
            else:
                self._literal = item
        if self._literal:
            take = len(self._literal) if n < 0 else min(n, len(self._literal))
            out, self._literal = self._literal[:take], self._literal[take:]
            return out
        take = self._manufacture if n < 0 else min(n, self._manufacture)
        take = min(take, self._write)
        self._manufacture -= take
        if self._shared is not None and take == len(self._shared):
            return self._shared
        return bytes(take)


def _declared_script(total: int, write: int = _PEER_WRITE) -> list[bytes | int]:
    return [b'HTTP/1.1 200 OK\r\ncontent-length: %d\r\n\r\n' % total, total]


def _chunked_script(total: int, write: int = _PEER_WRITE) -> list[bytes | int]:
    script: list[bytes | int] = [
        b'HTTP/1.1 200 OK\r\ntransfer-encoding: chunked\r\n\r\n']
    for _ in range(total // write):
        script += [b'%x\r\n' % write, write, b'\r\n']
    script.append(b'0\r\n\r\n')
    return script


def _close_delimited_script(total: int,
                            write: int = _PEER_WRITE) -> list[bytes | int]:
    return [b'HTTP/1.1 200 OK\r\nconnection: close\r\n\r\n', total]


_SCRIPTS = {
    'declared': _declared_script,
    'chunked': _chunked_script,
    'close-delimited': _close_delimited_script,
}


async def _peak_ratio(work: Awaitable[int], denominator: int) -> float:
    """Peak traced octets over *denominator*, from what *work* returns as N.

    Started here rather than by the harness so the baseline is this
    measurement's own; the delta over that baseline is taken explicitly, so an
    outer ``-X tracemalloc`` cannot fold its own traces into the ratio.
    """
    ours = not tracemalloc.is_tracing()
    tracemalloc.start()
    try:
        base, _ = tracemalloc.get_traced_memory()
        tracemalloc.reset_peak()
        received = await work
        _, peak = tracemalloc.get_traced_memory()
    finally:
        if ours:
            tracemalloc.stop()
    assert received == denominator, (
        f'the fixture delivered {received} octets, not {denominator} — '
        f'the ratio would be measured against the wrong body')
    return (peak - base) / denominator


async def _http1_body_octets(reader: AbstractReader) -> int:
    response = await HTTP1ResponseRecipient(request_method='GET').receive(reader)
    return len(response.body)


class _SilentWriter(AbstractWriter):
    async def write(self, data: bytes) -> None:
        pass


async def _drop(frame) -> None:
    """Swallow the WINDOW_UPDATEs the credit path emits."""


def _h2_client() -> HTTP2Client:
    client = HTTP2Client('localhost', 1)
    client._writer = _SilentWriter()
    client._control_sender = _drop
    return client


async def _http2_body_octets(client: HTTP2Client, total: int, *,
                             frame_size: int = _PEER_WRITE,
                             share_one_slice: bool = False) -> int:
    """Feed *total* octets of DATA to one stream and return the joined length.

    ``_on_response_data`` is the accumulation site and ``_complete`` is the
    join, so this drives both without a socket or a settled connection.  The
    loop turn between frames is the receive loop's own — see
    :meth:`_ScriptedReader.read` for why leaving it out changes the reading.
    """
    future = asyncio.get_running_loop().create_future()
    client._responses[1] = _PendingResponse(future=future)
    shared = bytes(frame_size) if share_one_slice else None
    for _ in range(total // frame_size):
        payload = shared if shared is not None else bytes(frame_size)
        await asyncio.sleep(0)
        await client._on_response_data(
            client._factory.create(FrameTypes.DATA, 0, 1, data=payload))
    await client._on_response_data(
        client._factory.create(FrameTypes.DATA, 1, 1, data=b''))
    return len((await future).body)


@pytest.fixture(autouse=True)
def _cap_at_the_body(monkeypatch):
    """The cap set to exactly what arrives: the at-the-cap case is the only
    one where the multiplier is what an operator is charged.

    ``pytest.ini`` captures at DEBUG, and ``Data.__init__`` logs its payload —
    so under the harness alone every 64 KiB frame is rendered to a quarter-
    megabyte ``repr`` inside the traced region.  Production has DEBUG off; a
    measurement that left it on would be measuring the harness.
    """
    monkeypatch.setenv('BB_CLIENT_BODY_MAX_TOTAL', str(_CAP))
    package = logging.getLogger('blackbull')
    previous = package.level
    package.setLevel(logging.WARNING)   # setLevel, not .level: it clears the
    yield                               # per-logger isEnabledFor cache.
    package.setLevel(previous)


class TestTheBufferedBodyCostsTwiceTheCap:
    @pytest.mark.parametrize('framing', sorted(_SCRIPTS))
    async def test_an_http1_body_at_the_cap_peaks_at_about_twice_it(self, framing):
        """``_read_body`` joins a list of slices, whatever the framing chose.

        All three framings meet at that one join, so all three are charged the
        same multiplier — which is why the documentation states it once, for
        the cap, rather than once per framing.
        """
        reader = _ScriptedReader(_SCRIPTS[framing](_CAP))
        ratio = await _peak_ratio(_http1_body_octets(reader), _CAP)
        assert _BAND[0] <= ratio <= _BAND[1], (
            f'{framing}: a body at the cap peaked at {ratio:.3f} x the cap; '
            f'the documented multiplier is ~2 and the band is {_BAND}')

    async def test_an_http2_body_at_the_cap_peaks_at_about_twice_it(self):
        """The same shape and the same knob.

        ``client_body_max_total`` is shared across the two protocols because
        the peer picks which client answers, so the multiplier is a claim about
        both — ``_complete`` joins ``pending.body_parts`` exactly as
        ``_read_body`` joins its slices.
        """
        client = _h2_client()
        ratio = await _peak_ratio(_http2_body_octets(client, _CAP), _CAP)
        assert _BAND[0] <= ratio <= _BAND[1], (
            f'an HTTP/2 body at the cap peaked at {ratio:.3f} x the cap; '
            f'the documented multiplier is ~2 and the band is {_BAND}')


class TestTheExcessIsPerSliceNotPerOctet:
    """Why the multiplier is not one number: the peer picks the slice count.

    Peak is ``2N + 121 x slices``, and the peer's write size sets ``slices``.
    Two measurements at the same body and two write sizes give the constant
    back by subtraction, which is what the documentation publishes and what
    lets an operator work out their own peer's number instead of trusting a
    single reading taken at ours.

    HTTP/2 has the same shape with the DATA frame in place of the read — its
    slice count is the peer's frame size, bounded above by
    ``client_h2_max_frame_size`` — so it is not measured twice here.
    """

    async def test_the_constant_is_recoverable_from_two_write_sizes(self):
        wide = await _peak_ratio(_http1_body_octets(_ScriptedReader(
            _declared_script(_CAP))), _CAP)
        narrow = await _peak_ratio(_http1_body_octets(_ScriptedReader(
            _declared_script(_CAP, _SMALL_PEER_WRITE),
            write_size=_SMALL_PEER_WRITE)), _CAP)
        slices = _CAP // _SMALL_PEER_WRITE - _CAP // _PEER_WRITE
        per_slice = _CAP * (narrow - wide) / slices
        assert 100 <= per_slice <= 145, (
            f'the per-slice excess measured {per_slice:.1f} bytes, not ~'
            f'{_PER_SLICE}: {wide:.4f} at {_PEER_WRITE} byte writes and '
            f'{narrow:.4f} at {_SMALL_PEER_WRITE}.  The documentation gives '
            f'operators 2 + {_PER_SLICE}/write to size their own peer with, '
            f'so a moved constant makes that arithmetic wrong.')


class TestTheInstrumentIsNotBlind:
    """The control: the same code paths, one shared slice, ~1 N.

    ``tracemalloc`` counts an object once however many references point at it.
    A fixture that reuses one slice therefore reads ~1 N through code that
    genuinely doubles, which is how the first measurement of this ratio came
    out at 1.00.  These two prove the reading above is the accumulation and not
    the harness — and they are the reason the assertions above have a *lower*
    edge at all.

    They also happen to be the one thing here that a ``bytearray`` rewrite
    cannot pass: a strategy that copies costs the same whether the slices are
    distinct or not, so it reads ~2 here too.  That is a side effect and not
    the guarantee — the reason not to make that change is written in
    ``env.py``, where it will be read.
    """

    async def test_a_reused_http1_slice_reads_about_one(self):
        reader = _ScriptedReader(_declared_script(_CAP), share_one_slice=True)
        ratio = await _peak_ratio(_http1_body_octets(reader), _CAP)
        assert ratio < 1.5, _BLIND_CONTROL_FAILED.format(kind='slice', ratio=ratio)

    async def test_a_reused_http2_payload_reads_about_one(self):
        client = _h2_client()
        ratio = await _peak_ratio(
            _http2_body_octets(client, _CAP, share_one_slice=True), _CAP)
        assert ratio < 1.5, _BLIND_CONTROL_FAILED.format(kind='payload', ratio=ratio)
