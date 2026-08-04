"""HTTP/1.1 client: responses that legitimately omit ``Content-Length``.

RFC 9110 §8.6 forbids ``Content-Length`` on 1xx and 204, and a 304 carries
none of its own; RFC 9112 §6.3 makes such a response body-less by framing
rather than by a declared length of zero.  The client must read them as an
empty body, not as a length it then tries to parse.

``Headers.get`` returns ``b''`` for a missing field, so "absent" and "present
but empty" are the same value — a presence test written as ``is not None``
silently turns every one of these responses into ``int(b'')``.  The WebSocket
handshake (101) is the case that reaches this in practice.
"""
import pytest

from blackbull.client.http1 import HTTP1ResponseRecipient
from blackbull.server.recipient import AbstractReader


class _BytesReader(AbstractReader):
    """Minimal reader over a fixed buffer."""

    def __init__(self, data: bytes) -> None:
        self._buf = bytearray(data)

    async def read(self, n: int) -> bytes:
        chunk = bytes(self._buf[:n])
        del self._buf[:n]
        return chunk

    async def readexactly(self, n: int) -> bytes:
        if len(self._buf) < n:
            raise AssertionError(
                f'client asked for {n} bytes with only {len(self._buf)} left')
        chunk = bytes(self._buf[:n])
        del self._buf[:n]
        return chunk

    async def readuntil(self, sep: bytes) -> bytes:
        idx = self._buf.find(sep)
        end = len(self._buf) if idx == -1 else idx + len(sep)
        chunk = bytes(self._buf[:end])
        del self._buf[:end]
        return chunk

    @property
    def remaining(self) -> bytes:
        return bytes(self._buf)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'status_line, label',
    [(b'HTTP/1.1 101 Switching Protocols', '101'),
     (b'HTTP/1.1 204 No Content', '204'),
     (b'HTTP/1.1 304 Not Modified', '304')],
    ids=['switching-protocols', 'no-content', 'not-modified'],
)
async def test_response_without_content_length_reads_as_empty_body(
        status_line, label):
    reader = _BytesReader(status_line + b'\r\nDate: x\r\n\r\n')

    response = await HTTP1ResponseRecipient().receive(reader)

    assert response.body == b'', (
        f'{label} response with no Content-Length did not read as empty')


@pytest.mark.asyncio
async def test_a_declared_zero_length_still_reads_as_empty_body():
    """``Content-Length: 0`` is a real, and valid, declaration of no body."""
    reader = _BytesReader(
        b'HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n')

    response = await HTTP1ResponseRecipient().receive(reader)

    assert response.body == b''


@pytest.mark.asyncio
async def test_a_bodyless_response_leaves_the_next_one_unread():
    """Framing is preserved: nothing is consumed past the header block.

    A client that mis-frames a body-less response desyncs the keep-alive
    connection for the response that follows it.
    """
    reader = _BytesReader(
        b'HTTP/1.1 204 No Content\r\nDate: x\r\n\r\n'
        b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok')

    first = await HTTP1ResponseRecipient().receive(reader)
    assert first.status == 204
    assert first.body == b''

    second = await HTTP1ResponseRecipient().receive(reader)
    assert second.status == 200
    assert second.body == b'ok'
    assert reader.remaining == b''
