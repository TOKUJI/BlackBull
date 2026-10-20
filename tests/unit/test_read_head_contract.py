"""``read_head`` answers each case the H/1.1 actor branches on differently.

A complete head, an idle close (silence), a truncated head (400), an
over-budget head with well-formed lines (431), and an over-budget blob with no
line terminator at all (400).
"""
import asyncio

import pytest

from blackbull.server.http1_actor import (BadRequestError, HeaderTooLargeError,
                                          _reject_oversized_head)
from blackbull.server.read_buffer import ReadBuffer
from blackbull.server.recipient import IncompleteReadError, ReadLimitExceeded

from tests.harness import feed, fed

pytestmark = pytest.mark.asyncio

HEAD = b'GET /x HTTP/1.1\r\nHost: localhost\r\n\r\n'


def _reader(data: bytes):
    return fed(data).reader


# ---------------------------------------------------------------------------
async def test_complete_head_comes_back_whole():
    assert await _reader(HEAD).read_head(8192) == HEAD


async def test_body_after_the_head_is_left_alone():
    """The head read must stop at the terminator: a byte of body consumed here
    is a byte the recipient never sees, and a desynced keep-alive stream."""
    reader = _reader(HEAD + b'BODYBYTES')
    assert await reader.read_head(8192) == HEAD
    assert await reader.readexactly(9) == b'BODYBYTES'


async def test_idle_close_is_an_empty_head():
    """Nothing sent, peer gone: no request, and so no response either."""
    assert await _reader(b'').read_head(8192) == b''


async def test_truncated_head_carries_what_arrived():
    """Distinct from the idle close above — this one is answered 400, and the
    actor decides that by looking at the partial."""
    with pytest.raises(IncompleteReadError) as caught:
        await _reader(b'GET /x HTTP/1.1\r\nHost: local').read_head(8192)
    assert caught.value.partial.startswith(b'GET /x HTTP/1.1\r\n')


async def test_over_budget_head_reports_the_breach():
    """Well-formed lines, too many of them → the actor's 431."""
    fat = (b'GET /x HTTP/1.1\r\n'
           + b'X-Pad: ' + b'p' * 200 + b'\r\n' * 1) * 40 + b'\r\n'
    with pytest.raises(ReadLimitExceeded) as caught:
        await _reader(fat).read_head(1024)
    assert b'\r\n' in caught.value.seen


async def test_over_budget_blob_with_no_terminator_reports_the_breach():
    """No CRLF anywhere → the actor's 400.  Same exception, different evidence,
    which is why ``seen`` travels with it rather than being re-derived."""
    with pytest.raises(ReadLimitExceeded) as caught:
        await _reader(b'x' * 4000).read_head(1024)
    assert b'\r\n' not in caught.value.seen
    assert caught.value.seen.startswith(b'xxx')


async def test_a_zero_budget_disables_the_bound():
    """0 means "no limit" everywhere the setting is read, so it has to mean
    that here too — otherwise the budget check fires on every request."""
    fat = b'GET /x HTTP/1.1\r\nX-Pad: ' + b'p' * 20_000 + b'\r\n\r\n'
    assert await _reader(fat).read_head(0) == fat


async def test_readuntil_overrun_stays_resident():
    reader = _reader(b'x' * 10_000 + b'\n')

    with pytest.raises(ReadLimitExceeded) as caught:
        await reader.readuntil(b'\n', limit=8)

    assert len(caught.value.seen) <= 9
    assert reader.buffered_len() == 10_001


async def test_readuntil_limit_wins_over_eof():
    reader = _reader(b'x' * 9_000)

    with pytest.raises(ReadLimitExceeded):
        await reader.readuntil(b'\n', limit=8192)

    assert reader.buffered_len() == 9_000


async def test_oversized_header_keeps_request_line_for_431_classification():
    raw = b'GET / HTTP/1.1\r\nX-Pad: ' + b'x' * 2_000 + b'\r\n\r\n'

    with pytest.raises(ReadLimitExceeded) as caught:
        await _reader(raw).read_head(128)

    assert caught.value.seen.startswith(b'GET / HTTP/1.1\r\n')
    with pytest.raises(HeaderTooLargeError):
        _reject_oversized_head(caught.value.seen, 128)


async def test_oversized_request_line_remains_a_400():
    reader = _reader(b'G' * 2_000 + b'\r\n\r\n')

    with pytest.raises(ReadLimitExceeded) as caught:
        await reader.read_head(128)

    with pytest.raises(BadRequestError):
        _reject_oversized_head(caught.value.seen, 128)


async def test_second_head_reads_cleanly_after_the_first():
    """The keep-alive case: two pipelined requests, read one at a time."""
    reader = _reader(HEAD + HEAD)
    assert await reader.read_head(8192) == HEAD
    assert await reader.read_head(8192) == HEAD
    assert await reader.read_head(8192) == b''


async def test_read_head_skips_the_empty_scan():
    """read_head must not call ``find_head_end`` while the buffer is empty —
    the "check" of the check-then-wait-then-check pattern.

    That empty call is a control-flow artifact (~0.79 µs/req on EC2, F5): an
    empty buffer can never be ``LIMIT_EXCEEDED`` (0 > limit is false) and the
    scan's resumption state is untouched (it would set ``_scanned = _w``,
    which already equals ``_r``), so skipping it is conformance-safe.
    """
    proto = fed(eof=False)
    empty_calls: list[int] = []
    orig = ReadBuffer.find_head_end

    def counting(self, limit: int = 0):
        if self.available == 0:
            empty_calls.append(self.available)
        return orig(self, limit)

    ReadBuffer.find_head_end = counting
    try:
        task = asyncio.create_task(proto.reader.read_head(8192))
        await asyncio.sleep(0)                 # let it reach the wait
        feed(proto, HEAD)
        assert await asyncio.wait_for(task, 1) == HEAD
    finally:
        ReadBuffer.find_head_end = orig
    assert not empty_calls, \
        f'find_head_end ran on an empty buffer {len(empty_calls)}×'

