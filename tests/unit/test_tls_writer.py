"""A TLS write reaches the transport whole before it waits (BLA-522).

Several HTTP/2 streams share one writer, so a write that suspended between its
pieces could let another stream's frames land inside it.
"""
import pytest

from blackbull.server.sender import TLSWriter

pytestmark = pytest.mark.asyncio

CHUNK = 1024 * 1024


class _Backing:
    def __init__(self):
        self.log = []

    def write(self, data):
        self.log.append(bytes(data))

    def writelines(self, parts):
        self.log.append(b''.join(parts))

    async def drain(self):
        self.log.append('drain')


@pytest.mark.parametrize('call', ['write', 'writelines'])
async def test_every_piece_precedes_the_one_drain(call):
    backing = _Backing()
    data = bytes(range(256)) * (5 * CHUNK // 256 + 1)
    writer = TLSWriter(backing)
    if call == 'write':
        await writer.write(data)
    else:
        await writer.writelines([b'head', data, b'tail'])
    *pieces, last = backing.log
    assert last == 'drain' and 'drain' not in pieces
    assert max(map(len, pieces)) <= CHUNK
    assert b''.join(pieces) == (data if call == 'write' else b'head' + data + b'tail')
