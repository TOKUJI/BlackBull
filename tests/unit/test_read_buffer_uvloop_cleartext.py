"""Regression: the uvloop cleartext read path must not crash on the read-buffer
window release.

uvloop 0.22.1's buffered read path (`__uv_stream_buffered_on_read` in
handles/stream.pyx) calls ``buffer_updated()`` while its Py_buffer export on
the ``get_buffer`` window is still acquired — the export is released in
uvloop's own ``finally``, immediately after our callback returns.
``ReadBuffer._drop_view`` used to call ``view.release()`` unconditionally,
which raised ``BufferError: memoryview has 1 exported buffer`` on every
cleartext connection.  TLS goes through SSLProtocol and never hit this — so
`blackbull-cleartext` (plain HTTP under uvloop) was broken at HEAD c618255
while TLS worked.

This test runs the real ``create_server`` seam over a loopback socket under
the uvloop event-loop policy — the exact path that crashed — and makes
sequential round trips, the first of which failed on the pre-fix code.
"""
import asyncio
import socket

import pytest

uvloop = pytest.importorskip("uvloop")

from tests.unit.test_connection_protocol_e2e import (  # noqa: E402
    _echo_app,
    _roundtrip,
    _serve_once,
)


def _do_roundtrips(port: int, request: bytes, n: int) -> list[bytes]:
    outs = []
    for _ in range(n):
        s = socket.create_connection(('127.0.0.1', port), timeout=5)
        try:
            s.sendall(request)
            out = b''
            while True:
                chunk = s.recv(65536)
                if not chunk:
                    break
                out += chunk
            outs.append(out)
        finally:
            s.close()
    return outs


async def _roundtrips(n: int) -> list[bytes]:
    server, port = await _serve_once(_echo_app)
    req = b'GET / HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n'
    try:
        return await asyncio.to_thread(_do_roundtrips, port, req, n)
    finally:
        server.close()
        await server.wait_closed()


def test_cleartext_socket_under_uvloop_roundtrips():
    """64 sequential cleartext round trips under uvloop must all succeed.

    The pre-fix code raised BufferError on every cleartext connection under
    uvloop (the first request already failed, rc=52).
    """
    old_policy = asyncio.get_event_loop_policy()
    asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
    try:
        outs = asyncio.run(_roundtrips(64))
    finally:
        asyncio.set_event_loop_policy(old_policy)
    assert len(outs) == 64
    failed = [o for o in outs if not o.startswith(b'HTTP/1.1 200 OK')]
    assert not failed, f'{len(failed)}/64 round trips did not return 200 OK'


# --- keep-alive (reaches ReadBuffer.compact) --------------------------------

def _do_keepalive(port: int, n: int) -> list[bytes]:
    """n keep-alive requests on ONE connection, each head padded so the
    consumed prefix crosses the compact threshold (4 KiB) on a live
    connection — the path that calls ``ReadBuffer.compact()`` while uvloop's
    read callback owns the transport (the F3b bench covered this under load;
    this locks it into CI)."""
    s = socket.create_connection(('127.0.0.1', port), timeout=5)
    outs = []
    try:
        for i in range(n):
            close_hdr = b'Connection: close' if i == n - 1 else \
                b'Connection: keep-alive'
            req = (b'GET / HTTP/1.1\r\nHost: localhost\r\n'
                   b'X-Pad: ' + b'a' * 1200 + b'\r\n' + close_hdr + b'\r\n\r\n')
            s.sendall(req)
            out = b''
            while not out.endswith(b'ok'):      # one response per request
                chunk = s.recv(65536)
                if not chunk:
                    break
                out += chunk
            outs.append(out)
        return outs
    finally:
        s.close()


async def _keepalive(n: int) -> list[bytes]:
    server, port = await _serve_once(_echo_app)
    try:
        return await asyncio.to_thread(_do_keepalive, port, n)
    finally:
        server.close()
        await server.wait_closed()


def test_cleartext_keepalive_under_uvloop_reaches_compact():
    """8 keep-alive requests on one connection (~1.2 KiB head each) under
    uvloop — the consumed prefix crosses 4 KiB, exercising ``compact()`` on
    a live connection.  All responses must be correct."""
    old_policy = asyncio.get_event_loop_policy()
    asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
    try:
        outs = asyncio.run(_keepalive(8))
    finally:
        asyncio.set_event_loop_policy(old_policy)
    assert len(outs) == 8
    failed = [o for o in outs if not o.startswith(b'HTTP/1.1 200 OK')]
    assert not failed, f'{len(failed)}/8 keep-alive responses failed'


# --- large body (reaches ReadBuffer._make_room) -----------------------------

def _do_large_body(port: int, size: int) -> bytes:
    s = socket.create_connection(('127.0.0.1', port), timeout=5)
    try:
        req = (b'POST / HTTP/1.1\r\nHost: localhost\r\n'
               b'Content-Length: ' + str(size).encode() + b'\r\n'
               b'Connection: close\r\n\r\n' + b'x' * size)
        s.sendall(req)
        out = b''
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            out += chunk
        return out
    finally:
        s.close()


async def _large_body(size: int) -> bytes:
    server, port = await _serve_once(_echo_app)
    try:
        return await asyncio.to_thread(_do_large_body, port, size)
    finally:
        server.close()
        await server.wait_closed()


def test_cleartext_large_body_under_uvloop_reaches_grow():
    """A 32 KiB POST body under uvloop forces the read buffer to grow past
    its initial 8 KiB allocation — exercising ``_make_room()`` (the bytearray
    mutation that would raise BufferError if the transport's export were
    still alive)."""
    old_policy = asyncio.get_event_loop_policy()
    asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
    try:
        out = asyncio.run(_large_body(32 * 1024))
    finally:
        asyncio.set_event_loop_policy(old_policy)
    assert out.startswith(b'HTTP/1.1 200 OK'), out[:120]


# --- small keep-alive requests must NOT grow the buffer (F5 finding) --------

def _do_keepalive_small(port: int, n: int) -> list[bytes]:
    """n B1-shaped keep-alive requests (tiny head, no padding) on one
    connection.  The F5 sprint finding: uvloop cleartext passes libuv's fixed
    64 KiB sizehint to ``get_buffer``, and honouring it grew the buffer to
    64 KiB and shrunk it back at every message boundary — a 64 KiB alloc/free
    churn per request."""
    s = socket.create_connection(('127.0.0.1', port), timeout=5)
    outs = []
    try:
        for i in range(n):
            close_hdr = b'Connection: close' if i == n - 1 else \
                b'Connection: keep-alive'
            req = (b'GET / HTTP/1.1\r\nHost: localhost\r\n' + close_hdr +
                   b'\r\n\r\n')
            s.sendall(req)
            out = b''
            while not out.endswith(b'ok'):      # one response per request
                chunk = s.recv(65536)
                if not chunk:
                    break
                out += chunk
            outs.append(out)
        return outs
    finally:
        s.close()


async def _keepalive_small(n: int) -> list[bytes]:
    server, port = await _serve_once(_echo_app)
    try:
        return await asyncio.to_thread(_do_keepalive_small, port, n)
    finally:
        server.close()
        await server.wait_closed()


def test_cleartext_keepalive_small_requests_do_not_grow():
    """B1-shaped keep-alive requests (tiny heads) must not grow the read
    buffer under uvloop cleartext.

    uvloop passes libuv's fixed 64 KiB sizehint on every ``get_buffer``; if
    ``get_buffer`` honours it (``want = max(sizehint, _MIN_READ)`` = 64 KiB),
    the buffer grows 8 KiB→64 KiB on the first request and
    ``compact()``/``_release()`` give it back at every message boundary — a
    64 KiB alloc/free churn per request (~4.2 µs/req at B1, the F5
    read-path finding).  Growth must be driven by bytes actually arriving,
    not by the hint.
    """
    from blackbull.server.read_buffer import ReadBuffer
    calls: list[int] = []
    orig = ReadBuffer._make_room

    def counting(self, want: int) -> memoryview:
        calls.append(want)
        return orig(self, want)

    ReadBuffer._make_room = counting
    old_policy = asyncio.get_event_loop_policy()
    try:
        asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
        outs = asyncio.run(_keepalive_small(200))
    finally:
        asyncio.set_event_loop_policy(old_policy)
        ReadBuffer._make_room = orig
    assert len(outs) == 200
    failed = [o for o in outs if not o.startswith(b'HTTP/1.1 200 OK')]
    assert not failed, f'{len(failed)}/200 keep-alive responses failed'
    assert not calls, (
        f'_make_room fired {len(calls)}× on small keep-alive requests '
        f'(sizehint-driven 64 KiB churn): {calls[:5]}')
