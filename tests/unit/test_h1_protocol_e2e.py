"""End-to-end: a real socket, `loop.create_server`, and the existing actor.

The unit tests drive the protocol with a fake transport.  This one puts the
whole seam together — kernel → `get_buffer` → `ReadBuffer` → `BufferReader` →
`HTTP1Actor` → `AsyncioWriter` → transport — over a real loopback connection,
because the parts that break at that seam (buffer lifetime under `recv_into`,
drain against a real high-water mark, EOF ordering) do not break against a
fake.

This is the cut-over target: `create_server` with a protocol factory, in place
of `start_server` with its StreamReader/StreamWriter pair.
"""
import asyncio
import socket

import pytest

from blackbull.server.h1_protocol import H1Protocol
from blackbull.server.http1_actor import HTTP1Actor
from blackbull.server.sender import AsyncioWriter

pytestmark = pytest.mark.asyncio


async def _echo_app(conn, receive, send):
    await send({'type': 'http.response.start', 'status': 200,
                'headers': [(b'content-type', b'text/plain')]})
    await send({'type': 'http.response.body', 'body': b'ok'})


class _ServedProtocol(H1Protocol):
    """Runs an `HTTP1Actor` over itself once the transport is up.

    Mirrors what `ConnectionActor` will do after the cut-over: the actor reads
    the head through `read_head`, and every later read — body, keep-alive,
    pipelined surplus — comes off the same buffer.
    """

    def __init__(self, app):
        super().__init__()
        self._app = app
        self._task: asyncio.Task | None = None

    def connection_made(self, transport):
        super().connection_made(transport)
        self._task = asyncio.create_task(self._serve())

    async def _serve(self):
        try:
            head = await self.reader.read_head(limit=65536)
            if not head:
                return
            actor = HTTP1Actor(
                self.reader, AsyncioWriter(self), self._app, None,
                request=head,
                peername=self.get_extra_info('peername'),
                sockname=self.get_extra_info('sockname'),
            )
            await actor.run()
        finally:
            self.close()


async def _serve_once(app):
    loop = asyncio.get_running_loop()
    server = await loop.create_server(
        lambda: _ServedProtocol(app), '127.0.0.1', 0)
    port = server.sockets[0].getsockname()[1]
    return server, port


def _roundtrip(port: int, request: bytes, timeout: float = 5.0) -> bytes:
    s = socket.create_connection(('127.0.0.1', port), timeout=timeout)
    try:
        s.sendall(request)
        out = b''
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            out += chunk
        return out
    finally:
        s.close()


class TestRealSocket:
    async def test_single_request_round_trips(self):
        server, port = await _serve_once(_echo_app)
        try:
            got = await asyncio.to_thread(
                _roundtrip, port,
                b'GET / HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n')
        finally:
            server.close()
            await server.wait_closed()
        assert got.startswith(b'HTTP/1.1 200 OK'), got[:120]
        assert got.endswith(b'ok')

    async def test_head_split_across_tcp_segments(self):
        """The kernel decides segment boundaries, not us.

        A head delivered in pieces exercises the resumable scan against a real
        `recv_into` — the path where a stale `memoryview` into a resized
        buffer would surface as `BufferError` rather than a wrong answer.
        """
        server, port = await _serve_once(_echo_app)

        def _dribble():
            s = socket.create_connection(('127.0.0.1', port), timeout=5)
            try:
                req = (b'GET / HTTP/1.1\r\nHost: localhost\r\n'
                       b'X-Pad: ' + b'a' * 3000 + b'\r\n'
                       b'Connection: close\r\n\r\n')
                for i in range(0, len(req), 137):     # ragged, unaligned
                    s.sendall(req[i:i + 137])
                out = b''
                while True:
                    chunk = s.recv(65536)
                    if not chunk:
                        break
                    out += chunk
                return out
            finally:
                s.close()

        try:
            got = await asyncio.to_thread(_dribble)
        finally:
            server.close()
            await server.wait_closed()
        assert got.startswith(b'HTTP/1.1 200 OK'), got[:120]

    async def test_body_is_read_through_the_same_buffer(self):
        async def echo_body(conn, receive, send):
            body = b''
            while True:
                event = await receive()
                body += event.get('body', b'')
                if not event.get('more_body'):
                    break
            await send({'type': 'http.response.start', 'status': 200,
                        'headers': [(b'content-type', b'application/octet-stream')]})
            await send({'type': 'http.response.body', 'body': body})

        server, port = await _serve_once(echo_body)
        payload = b'x' * 5000
        try:
            got = await asyncio.to_thread(
                _roundtrip, port,
                b'POST /echo HTTP/1.1\r\nHost: localhost\r\n'
                b'Content-Length: ' + str(len(payload)).encode() + b'\r\n'
                b'Connection: close\r\n\r\n' + payload)
        finally:
            server.close()
            await server.wait_closed()
        assert got.startswith(b'HTTP/1.1 200 OK'), got[:120]
        assert got.endswith(payload)

    async def test_keep_alive_second_request_off_resident_bytes(self):
        """Two requests pipelined into one segment.

        The second head is already resident when the first completes, which is
        the case the whole design exists for — and the case the layered reader
        had to hand back through `unread`.
        """
        server, port = await _serve_once(_echo_app)
        try:
            got = await asyncio.to_thread(
                _roundtrip, port,
                b'GET /a HTTP/1.1\r\nHost: localhost\r\n\r\n'
                b'GET /b HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n')
        finally:
            server.close()
            await server.wait_closed()
        assert got.count(b'HTTP/1.1 200 OK') == 2, got[:200]
