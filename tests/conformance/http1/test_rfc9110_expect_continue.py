"""RFC 9110 §10.1.1 / §8.6 — ``Expect: 100-continue`` on a persistent connection.

Two contracts, both exercised over a real socket against a live server so the
assertions are wire bytes rather than sender internals:

* **§10.1.1** — every request carrying ``Expect: 100-continue`` gets the
  interim response, not just the first one on the connection.  The sender is
  shared across keep-alive requests, so its "response already complete" guard
  must be cleared before the interim response is written; otherwise request 2
  onward silently gets nothing and the client stalls until its own Expect
  timeout before sending the body.

* **§8.6** — ``A server MUST NOT send a Content-Length header field in any
  response with a status code of 1xx (Informational)``.  An interim response
  has no body, and a length a proxy believes bounds one invites a framing
  desync on the connection the real response still has to use.
"""
import asyncio

import pytest

from blackbull import BlackBull
from blackbull.testing import NativeTestServer


def _make_app() -> BlackBull:
    app = BlackBull()

    @app.route(methods=['POST'], path='/upload')
    async def upload(conn, receive, send):
        body = await conn.body()
        await send(body, 200, [(b'content-type', b'application/octet-stream')])

    return app


_REQUEST = (b'POST /upload HTTP/1.1\r\n'
            b'Host: localhost\r\n'
            b'Expect: 100-continue\r\n'
            b'Content-Length: 3\r\n\r\n')


async def _read_message(reader, timeout: float = 2.0) -> tuple[bytes, dict, bytes]:
    """Read one complete HTTP message; return ``(status_line, headers, body)``."""
    head = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), timeout)
    lines = head.split(b'\r\n')
    status_line = lines[0]
    headers: dict[bytes, bytes] = {}
    for line in lines[1:]:
        if b':' in line:
            name, _, value = line.partition(b':')
            headers[name.strip().lower()] = value.strip()
    length = int(headers.get(b'content-length', b'0'))
    body = await asyncio.wait_for(reader.readexactly(length), timeout) if length else b''
    return status_line, headers, body


@pytest.mark.asyncio
async def test_100_continue_sent_on_every_keepalive_request():
    """RFC 9110 §10.1.1 — the interim response is per-request, not per-connection."""
    async with NativeTestServer(_make_app()) as server:
        reader, writer = await asyncio.open_connection('127.0.0.1', server.port)
        try:
            for i in range(1, 4):
                writer.write(_REQUEST)
                await writer.drain()

                status_line, _, _ = await _read_message(reader)
                assert status_line.startswith(b'HTTP/1.1 100'), (
                    f'request {i} on the keep-alive connection got '
                    f'{status_line!r} instead of an interim 100 Continue')

                writer.write(b'abc')
                await writer.drain()

                status_line, _, body = await _read_message(reader)
                assert status_line.startswith(b'HTTP/1.1 200'), status_line
                assert body == b'abc'
        finally:
            writer.close()


@pytest.mark.asyncio
async def test_100_continue_carries_no_content_length():
    """RFC 9110 §8.6 — a 1xx response MUST NOT carry Content-Length."""
    async with NativeTestServer(_make_app()) as server:
        reader, writer = await asyncio.open_connection('127.0.0.1', server.port)
        try:
            writer.write(_REQUEST)
            await writer.drain()

            status_line, headers, _ = await _read_message(reader)
            assert status_line.startswith(b'HTTP/1.1 100'), status_line
            assert b'content-length' not in headers, (
                f'interim response carried Content-Length: '
                f'{headers[b"content-length"]!r}')
            assert b'transfer-encoding' not in headers

            writer.write(b'abc')
            await writer.drain()
            status_line, _, body = await _read_message(reader)
            assert status_line.startswith(b'HTTP/1.1 200'), status_line
            assert body == b'abc'
        finally:
            writer.close()
