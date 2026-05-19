"""Shared fixtures + helpers for RFC 9112 (HTTP/1.1) conformance tests.

These tests speak HTTP/1.1 to a real listening BlackBull instance over a raw
TCP socket so we can send byte-exact requests — including the malformed
ones the RFC reserves for rejection.  Higher-level HTTP clients (httpx,
aiohttp) refuse to send the framing edge cases we need to test.

The application running under test is intentionally minimal so behaviour
differences come from the framework, not the handler:

* ``GET /``        — replies "ok" (text)
* ``POST /echo``   — replies with the request body verbatim
* ``GET /chunked`` — returns a streaming response (triggers chunked TE)
"""
from __future__ import annotations

import asyncio
import socket
from dataclasses import dataclass
from http import HTTPMethod
from multiprocessing import Process

import pytest

from blackbull import BlackBull, read_body
from blackbull.response import StreamingResponse


# ---------------------------------------------------------------------------
# The application under test
# ---------------------------------------------------------------------------

def _make_app() -> BlackBull:
    app = BlackBull()

    @app.route(path='/')
    async def index(scope, receive, send):
        await send({'type': 'http.response.start', 'status': 200,
                    'headers': [(b'content-type', b'text/plain')]})
        await send({'type': 'http.response.body', 'body': b'ok'})

    @app.route(path='/echo', methods=[HTTPMethod.GET, HTTPMethod.POST, HTTPMethod.PUT])
    async def echo(scope, receive, send):
        body = await read_body(receive)
        await send({'type': 'http.response.start', 'status': 200,
                    'headers': [(b'content-type', b'application/octet-stream')]})
        await send({'type': 'http.response.body', 'body': body})

    @app.route(path='/chunked')
    async def chunked(scope, receive, send):
        async def events():
            for ch in (b'a', b'b', b'c'):
                yield ch
        await StreamingResponse(events())(scope, receive, send)

    return app


@pytest.fixture(scope='module')
def h1_app():
    """A live BlackBull HTTP/1.1 server bound to a free port."""
    app = _make_app()
    app.create_server(port=0)
    p = Process(target=lambda: asyncio.run(app.run()))
    p.start()
    app.wait_for_port(timeout=10.0)
    yield app
    app.stop()
    p.terminate()
    p.join(timeout=5)


# ---------------------------------------------------------------------------
# Raw HTTP/1.1 wire helper
# ---------------------------------------------------------------------------

@dataclass
class RawResponse:
    """Minimal HTTP/1.1 response parsed off the wire.

    Used by the test files to assert that, e.g., a malformed request was
    rejected with 400 / the connection was closed / a specific status line
    came back.
    """
    raw:        bytes
    status:     int | None
    reason:     bytes
    headers:    list[tuple[bytes, bytes]]
    body:       bytes
    closed:     bool       # True if the peer closed (EOF before timeout)

    def header(self, name: bytes) -> bytes | None:
        """First matching header value, case-insensitively; None when absent."""
        name = name.lower()
        for k, v in self.headers:
            if k.lower() == name:
                return v
        return None

    def headers_named(self, name: bytes) -> list[bytes]:
        """All matching header values, in order."""
        name = name.lower()
        return [v for k, v in self.headers if k.lower() == name]


def send_raw(
    host: str, port: int, request: bytes, *,
    timeout: float = 2.0,
    read_max: int = 16384,
) -> RawResponse:
    """Send *request* to (host, port) and return the response.

    Reads until EOF or ``timeout`` seconds — whichever comes first.  We
    deliberately do not implement persistent-connection logic here: tests
    that need it open the socket explicitly via :func:`open_socket`.
    """
    sock = socket.create_connection((host, port), timeout=timeout)
    try:
        sock.sendall(request)
        sock.shutdown(socket.SHUT_WR)
        chunks = []
        sock.settimeout(timeout)
        try:
            while len(b''.join(chunks)) < read_max:
                buf = sock.recv(4096)
                if not buf:
                    closed = True
                    break
                chunks.append(buf)
            else:
                closed = False
        except (socket.timeout, TimeoutError):
            closed = False
    finally:
        sock.close()

    raw = b''.join(chunks)
    return _parse_response(raw, closed=closed)


def open_socket(host: str, port: int, timeout: float = 2.0) -> socket.socket:
    """Open a raw TCP connection.  The caller is responsible for closing."""
    sock = socket.create_connection((host, port), timeout=timeout)
    sock.settimeout(timeout)
    return sock


def read_until_eof(sock: socket.socket, max_bytes: int = 65536) -> bytes:
    """Drain a socket until EOF or timeout; return whatever was received."""
    out = []
    try:
        while sum(len(c) for c in out) < max_bytes:
            buf = sock.recv(4096)
            if not buf:
                break
            out.append(buf)
    except (socket.timeout, TimeoutError):
        pass
    return b''.join(out)


def parse_response(raw: bytes, *, closed: bool = False) -> RawResponse:
    """Public wrapper for parsing a captured HTTP/1.1 response."""
    return _parse_response(raw, closed=closed)


def _parse_response(raw: bytes, *, closed: bool) -> RawResponse:
    """Parse the wire bytes of an HTTP/1.1 response.

    Tolerant of partial data — when the headers haven't fully arrived, the
    status, headers, and body fields are populated with whatever was seen
    (``status=None`` indicates the status line never landed).
    """
    if not raw:
        return RawResponse(b'', None, b'', [], b'', closed)
    head, _, body = raw.partition(b'\r\n\r\n')
    lines = head.split(b'\r\n')
    status = None
    reason = b''
    if lines and lines[0].startswith(b'HTTP/'):
        parts = lines[0].split(b' ', 2)
        if len(parts) >= 2:
            try:
                status = int(parts[1])
            except ValueError:
                pass
            if len(parts) >= 3:
                reason = parts[2]
    headers: list[tuple[bytes, bytes]] = []
    for ln in lines[1:]:
        if b':' not in ln:
            continue
        k, _, v = ln.partition(b':')
        headers.append((k.strip(), v.strip()))
    return RawResponse(raw, status, reason, headers, body, closed)
