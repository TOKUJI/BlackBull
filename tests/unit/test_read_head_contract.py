"""``read_head`` is a contract, not a capability — every reader answers alike.

The H/1.1 actor asks its reader for one message head and does not ask what kind
of reader it is.  That only holds if the answers agree, so this file drives the
*same* wire bytes through every reader the server can end up with and asserts
one outcome per case:

* :class:`~blackbull.server.connection_protocol.BufferReader` — production,
  finds the terminator in a single scan of its own buffer;
* :class:`~blackbull.server.recipient.AsyncioReader` — a real
  ``asyncio.StreamReader`` underneath, one ``readuntil`` per line;
* :class:`~blackbull.server.recipient.PrefixReader` — bytes already taken off
  the stream replayed in front of it, which is how a caller hands the actor a
  request it has partly read.

The cases are the ones the actor branches on, and each maps to a different
answer on the wire: a complete head, an idle close (silence), a truncated head
(400), an over-budget head with well-formed lines (431), and an over-budget
blob with no line terminator at all (400).  A reader that disagreed on any of
them would put a hole in exactly one deployment shape.
"""
import asyncio

import pytest

from blackbull.server.connection_protocol import ConnectionProtocol
from blackbull.server.recipient import (AsyncioReader, IncompleteReadError,
                                        PrefixReader, ReadLimitExceeded)

pytestmark = pytest.mark.asyncio

HEAD = b'GET /x HTTP/1.1\r\nHost: localhost\r\n\r\n'


# ---------------------------------------------------------------------------
# One reader per builder, each fed *data* and then closed by the peer.
# ---------------------------------------------------------------------------

def _buffer_reader(data: bytes):
    proto = ConnectionProtocol()
    proto.connection_made(_NullTransport())
    view = proto.get_buffer(len(data))
    view[:len(data)] = data
    proto.buffer_updated(len(data))
    proto.eof_received()
    return proto.reader


def _asyncio_reader(data: bytes):
    sr = asyncio.StreamReader()
    sr.feed_data(data)
    sr.feed_eof()
    return AsyncioReader(sr)


def _prefix_reader(data: bytes):
    """Split so the seam falls mid-head — the case a naive replay gets wrong."""
    cut = min(7, len(data))
    return PrefixReader(data[:cut], _asyncio_reader(data[cut:]))


class _NullTransport:
    def pause_reading(self): pass
    def resume_reading(self): pass
    def write(self, data): pass
    def close(self): pass
    def is_closing(self): return False
    def get_extra_info(self, name, default=None): return default


READERS = pytest.mark.parametrize(
    'make_reader',
    [_buffer_reader, _asyncio_reader, _prefix_reader],
    ids=['buffer', 'asyncio', 'prefix'],
)


# ---------------------------------------------------------------------------


@READERS
async def test_complete_head_comes_back_whole(make_reader):
    assert await make_reader(HEAD).read_head(8192) == HEAD


@READERS
async def test_body_after_the_head_is_left_alone(make_reader):
    """The head read must stop at the terminator: a byte of body consumed here
    is a byte the recipient never sees, and a desynced keep-alive stream."""
    reader = make_reader(HEAD + b'BODYBYTES')
    assert await reader.read_head(8192) == HEAD
    assert await reader.readexactly(9) == b'BODYBYTES'


@READERS
async def test_idle_close_is_an_empty_head(make_reader):
    """Nothing sent, peer gone: no request, and so no response either."""
    assert await make_reader(b'').read_head(8192) == b''


@READERS
async def test_truncated_head_carries_what_arrived(make_reader):
    """Distinct from the idle close above — this one is answered 400, and the
    actor decides that by looking at the partial."""
    with pytest.raises(IncompleteReadError) as caught:
        await make_reader(b'GET /x HTTP/1.1\r\nHost: local').read_head(8192)
    assert caught.value.partial.startswith(b'GET /x HTTP/1.1\r\n')


@READERS
async def test_over_budget_head_reports_the_breach(make_reader):
    """Well-formed lines, too many of them → the actor's 431."""
    fat = (b'GET /x HTTP/1.1\r\n'
           + b'X-Pad: ' + b'p' * 200 + b'\r\n' * 1) * 40 + b'\r\n'
    with pytest.raises(ReadLimitExceeded) as caught:
        await make_reader(fat).read_head(1024)
    assert b'\r\n' in caught.value.seen


@READERS
async def test_over_budget_blob_with_no_terminator_reports_the_breach(make_reader):
    """No CRLF anywhere → the actor's 400.  Same exception, different evidence,
    which is why ``seen`` travels with it rather than being re-derived."""
    with pytest.raises(ReadLimitExceeded) as caught:
        await make_reader(b'x' * 4000).read_head(1024)
    assert b'\r\n' not in caught.value.seen
    assert caught.value.seen.startswith(b'xxx')


@READERS
async def test_a_zero_budget_disables_the_bound(make_reader):
    """0 means "no limit" everywhere the setting is read, so it has to mean
    that here too — otherwise the budget check fires on every request."""
    fat = b'GET /x HTTP/1.1\r\nX-Pad: ' + b'p' * 20_000 + b'\r\n\r\n'
    assert await make_reader(fat).read_head(0) == fat


@READERS
async def test_second_head_reads_cleanly_after_the_first(make_reader):
    """The keep-alive case: two pipelined requests, read one at a time."""
    reader = make_reader(HEAD + HEAD)
    assert await reader.read_head(8192) == HEAD
    assert await reader.read_head(8192) == HEAD
    assert await reader.read_head(8192) == b''
