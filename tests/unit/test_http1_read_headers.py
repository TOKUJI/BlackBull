"""copy-reduction-http1 P2 — `_read_headers` accumulates via bytearray.

The change replaced the O(n²) ``self._request += line`` with an amortised-O(1)
bytearray accumulation, published back as ``bytes``.  These lock in that the
loop is byte-for-byte equivalent: the request line is preserved, every header
line is appended in order, the block ends at the CRLFCRLF terminator, and the
published ``self._request`` is a ``bytes`` ending in that terminator.

The oversize / cap-hit path is covered by
``tests/unit/test_cap_log_sites.py::test_header_max_total_logs``; the parse
path by ``tests/conformance/http1/``.
"""
import pytest

from blackbull.server.http1_actor import HTTP1Actor
from blackbull.server.recipient import AbstractReader
from blackbull.server.sender import AbstractWriter


class _FakeReader(AbstractReader):
    """Yields pre-buffered bytes one CRLF-terminated line per readuntil()."""
    def __init__(self, data: bytes):
        self._buf = bytearray(data)

    async def read(self, n: int = -1) -> bytes:
        if n < 0:
            chunk, self._buf = bytes(self._buf), bytearray()
            return chunk
        chunk = bytes(self._buf[:n])
        del self._buf[:n]
        return chunk

    async def readuntil(self, sep: bytes) -> bytes:
        idx = self._buf.find(sep)
        if idx == -1:
            chunk, self._buf = bytes(self._buf), bytearray()
            return chunk + sep
        end = idx + len(sep)
        chunk = bytes(self._buf[:end])
        del self._buf[:end]
        return chunk

    async def readexactly(self, n: int) -> bytes:
        from blackbull.server.recipient import IncompleteReadError
        if len(self._buf) < n:
            raise IncompleteReadError(bytes(self._buf))
        chunk = bytes(self._buf[:n])
        del self._buf[:n]
        return chunk


class _FakeWriter(AbstractWriter):
    def __init__(self):
        self.written = bytearray()

    async def write(self, data: bytes) -> None:
        self.written += data

    async def writelines(self, parts) -> None:
        for p in parts:
            self.written += p

    async def close(self) -> None:
        pass


async def _noop_app(scope, receive, send):
    pass


def _actor_for(raw: bytes) -> HTTP1Actor:
    """Build an actor with the request line pre-read (as ConnectionActor does)
    and the remaining header lines queued in the reader."""
    first_line, rest = raw.split(b'\r\n', 1)
    return HTTP1Actor(
        _FakeReader(rest), _FakeWriter(), _noop_app, None,
        request=first_line + b'\r\n',
        peername=('127.0.0.1', 5), sockname=('0.0.0.0', 8000), ssl=False,
    )


@pytest.mark.asyncio
async def test_many_headers_accumulate_in_order():
    lines = [b'GET /x HTTP/1.1', b'Host: localhost']
    for i in range(50):
        lines.append(b'X-H-' + str(i).encode() + b': v' + str(i).encode())
    raw = b'\r\n'.join(lines) + b'\r\n\r\n'

    actor = _actor_for(raw)
    await actor._read_headers(0)  # 0 = no total cap

    assert isinstance(actor._request, bytes), 'buffer must be published as bytes'
    assert actor._request == raw, 'every line must be preserved, in order'
    assert actor._request.endswith(b'\r\n\r\n')


@pytest.mark.asyncio
async def test_zero_header_request_terminates():
    """HTTP/1.0-style request with no headers: request line + empty line."""
    raw = b'GET / HTTP/1.0\r\n\r\n'
    actor = _actor_for(raw)
    await actor._read_headers(0)
    assert actor._request == raw
    assert isinstance(actor._request, bytes)


@pytest.mark.asyncio
async def test_already_terminated_prefix_is_left_untouched():
    """If the pre-read buffer already ends with CRLFCRLF, the loop reads
    nothing and the buffer is unchanged."""
    raw = b'GET / HTTP/1.1\r\nHost: x\r\n\r\n'
    # Pre-load the *entire* block as the request prefix.
    actor = HTTP1Actor(
        _FakeReader(b''), _FakeWriter(), _noop_app, None,
        request=raw,
        peername=('127.0.0.1', 5), sockname=('0.0.0.0', 8000), ssl=False,
    )
    await actor._read_headers(0)
    assert actor._request == raw
