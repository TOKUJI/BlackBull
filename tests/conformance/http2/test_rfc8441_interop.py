"""RFC 8441 end-to-end interop tests — WebSocket over HTTP/2.

These tests use BlackBull's own public ``WebSocketH2Client`` (built on
top of ``HTTP2Client``) and BlackBull's own ``ws_codec.encode_frame``
for WebSocket framing — true dogfooding: the test client uses the same
protocol stack as the server.

The external ``h2`` library is NOT used.  The ``test_rfc8441.py`` file
covers frame-level correctness via in-process fakes; this file covers
end-to-end interop over real TLS + H2 connections.
"""

from __future__ import annotations

import asyncio
import multiprocessing
import os
import ssl
import time
from types import SimpleNamespace

import pytest

from blackbull import BlackBull
from blackbull.client.websocket_h2 import WebSocketH2Client
from blackbull.router import Scheme
from blackbull.server import ASGIServer
from blackbull.server.ws_codec import WSOpcode


# ---------------------------------------------------------------------------
# TLS helpers
# ---------------------------------------------------------------------------

_CERT = os.path.join(os.path.dirname(__file__), '..', '..', 'cert.pem')
_KEY  = os.path.join(os.path.dirname(__file__), '..', '..', 'key.pem')


def _ssl_context_for_h2() -> ssl.SSLContext:
    """SSLContext with ALPN ``h2`` and no cert verification (self-signed)."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ctx.set_alpn_protocols(['h2'])
    return ctx


# ---------------------------------------------------------------------------
# Server fixture
# ---------------------------------------------------------------------------

@pytest.fixture(scope='module')
def rfc8441_server_port():
    """A BlackBull TLS server with ``BB_H2_ENABLE_WEBSOCKET=1`` running
    in a forked subprocess.  Yields a namespace with ``.port``."""
    app = BlackBull()

    @app.route(path='/ws', scheme=Scheme.websocket)
    async def ws_echo(scope, receive, send):
        assert scope['type'] == 'websocket'
        await receive()  # websocket.connect
        await send({'type': 'websocket.accept'})
        while True:
            event = await receive()
            if event['type'] == 'websocket.receive':
                # BlackBull's WS recipient emits both keys with one set
                # to None, so check for non-None rather than membership.
                if event.get('text') is not None:
                    await send({'type': 'websocket.send', 'text': event['text']})
                elif event.get('bytes') is not None:
                    await send({'type': 'websocket.send', 'bytes': event['bytes']})
            elif event['type'] == 'websocket.disconnect':
                break

    @app.route(path='/ws-chat', scheme=Scheme.websocket)
    async def ws_chat(scope, receive, send):
        assert scope['type'] == 'websocket'
        await receive()
        await send({'type': 'websocket.accept', 'subprotocol': 'chat'})
        await receive()  # disconnect

    import os as _os
    _os.environ['BB_H2_ENABLE_WEBSOCKET'] = '1'

    server = ASGIServer(app, certfile=_CERT, keyfile=_KEY)
    server.open_socket(0)

    def _child():
        # Sprint 10 caution — settings cache is `@functools.cache`-d on
        # `get_settings()`; without this reset the child would inherit
        # the parent's cache and BB_H2_ENABLE_WEBSOCKET=1 would silently
        # no-op.
        from blackbull.env import reset_settings_cache
        reset_settings_cache()
        asyncio.run(server.run())

    proc = multiprocessing.Process(target=_child, daemon=True)
    proc.start()
    try:
        server.wait_for_port(timeout=10.0)
        yield SimpleNamespace(port=server.port)
    finally:
        server.close()
        proc.terminate()
        proc.join(timeout=5)


# ---------------------------------------------------------------------------
# §1 — Extended CONNECT handshake (real TLS, BlackBull client)
# ---------------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.asyncio(loop_scope='function')
class TestRFC8441Handshake:
    """The Extended CONNECT handshake must produce :status 200,
    indicating the server accepted the WebSocket-over-H2 request.  The
    SETTINGS advertisement is verified at the frame level in
    ``test_rfc8441.py``; this section covers end-to-end interop."""

    async def test_extended_connect_returns_200(self, rfc8441_server_port):
        async with WebSocketH2Client(
            '127.0.0.1', rfc8441_server_port.port,
            ssl=_ssl_context_for_h2(),
        ) as c:
            ws = await c.connect(path='/ws')
            assert c.connect_status == 200
            await ws.close()

    async def test_extended_connect_custom_path(self, rfc8441_server_port):
        async with WebSocketH2Client(
            '127.0.0.1', rfc8441_server_port.port,
            ssl=_ssl_context_for_h2(),
        ) as c:
            ws = await c.connect(path='/ws-chat')
            assert c.connect_status == 200
            await ws.close()


# ---------------------------------------------------------------------------
# §2 — WebSocket data exchange over H2
# ---------------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.asyncio(loop_scope='function')
class TestRFC8441DataExchange:
    """After the Extended CONNECT handshake, WebSocket frames must flow
    correctly over H2 DATA frames in both directions.  All outgoing
    frames are masked (RFC 6455 §5.1) via ``ws_codec.encode_frame``."""

    async def test_echo_text_frame(self, rfc8441_server_port):
        async with WebSocketH2Client(
            '127.0.0.1', rfc8441_server_port.port,
            ssl=_ssl_context_for_h2(),
        ) as c:
            ws = await c.connect(path='/ws')
            payload = b'Hello from BlackBull client!'
            await ws.send_text(payload.decode())
            opcode, echoed = await ws.receive(timeout=3.0)
            assert opcode == WSOpcode.TEXT
            assert echoed == payload
            await ws.close()

    async def test_echo_binary_frame(self, rfc8441_server_port):
        """A masked binary frame covering all 256 byte values must echo back."""
        async with WebSocketH2Client(
            '127.0.0.1', rfc8441_server_port.port,
            ssl=_ssl_context_for_h2(),
        ) as c:
            ws = await c.connect(path='/ws')
            payload = bytes(range(256))
            await ws.send_bytes(payload)
            opcode, echoed = await ws.receive(timeout=3.0)
            assert opcode == WSOpcode.BINARY
            assert echoed == payload
            await ws.close()

    async def test_multiple_frames_in_sequence(self, rfc8441_server_port):
        async with WebSocketH2Client(
            '127.0.0.1', rfc8441_server_port.port,
            ssl=_ssl_context_for_h2(),
        ) as c:
            ws = await c.connect(path='/ws')
            messages = [b'frame-1', b'frame-2', b'frame-3']
            for msg in messages:
                await ws.send_bytes(msg)
            for msg in messages:
                opcode, echoed = await ws.receive(timeout=3.0)
                assert echoed == msg
            await ws.close()

    async def test_medium_binary_frame_within_max_frame_size(
        self, rfc8441_server_port,
    ):
        """An 8 KiB binary frame fits inside a single H2 DATA frame (RFC
        9113 §4.2 default ``max_frame_size`` is 16,384) and inside the
        RFC §6.9.2 default 65,535-byte initial window — exercises
        BINARY echo at a non-trivial payload size without needing
        client-side DATA-frame splitting or ``WINDOW_UPDATE``."""
        async with WebSocketH2Client(
            '127.0.0.1', rfc8441_server_port.port,
            ssl=_ssl_context_for_h2(),
        ) as c:
            ws = await c.connect(path='/ws')
            payload = os.urandom(8192)
            await ws.send_bytes(payload)
            opcode, echoed = await ws.receive(timeout=5.0)
            assert opcode == WSOpcode.BINARY
            assert echoed == payload
            await ws.close()

    async def test_large_binary_frame_exceeds_h2_limits(
        self, rfc8441_server_port,
    ):
        """A 64 KiB payload exceeds both the H2 ``max_frame_size``
        default (16,384) and the RFC initial window (65,535).  Exercises
        outgoing DATA splitting via ``HTTP2Sender`` and receive-side
        ``WINDOW_UPDATE`` emission together."""
        async with WebSocketH2Client(
            '127.0.0.1', rfc8441_server_port.port,
            ssl=_ssl_context_for_h2(),
        ) as c:
            ws = await c.connect(path='/ws')
            payload = os.urandom(65536)
            await ws.send_bytes(payload)
            opcode, echoed = await ws.receive(timeout=5.0)
            assert opcode == WSOpcode.BINARY
            assert echoed == payload
            await ws.close()


# ---------------------------------------------------------------------------
# §3 — Close sequence
# ---------------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.asyncio(loop_scope='function')
class TestRFC8441Close:
    """The WebSocket close sequence over H2 must work cleanly: client
    sends a close frame + END_STREAM, the session detects closure, and
    the server's handler exits without raising."""

    async def test_normal_close(self, rfc8441_server_port):
        async with WebSocketH2Client(
            '127.0.0.1', rfc8441_server_port.port,
            ssl=_ssl_context_for_h2(),
        ) as c:
            ws = await c.connect(path='/ws')
            await ws.send_bytes(b'ping')
            _, echoed = await ws.receive(timeout=3.0)
            assert echoed == b'ping'
            await ws.close()


# ---------------------------------------------------------------------------
# §4 — Latency characterisation
# ---------------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.asyncio(loop_scope='function')
class TestRFC8441Latency:
    """Handshake latency sanity check — guards against pathological
    hangs in the Extended CONNECT path.  Ten consecutive handshakes
    must each complete under 2 s; the average must be under 500 ms
    (generous for localhost)."""

    async def test_ten_handshakes_within_reasonable_time(
        self, rfc8441_server_port,
    ):
        latencies: list[float] = []
        for _ in range(10):
            async with WebSocketH2Client(
                '127.0.0.1', rfc8441_server_port.port,
                ssl=_ssl_context_for_h2(),
            ) as c:
                t0 = time.monotonic()
                ws = await c.connect(path='/ws')
                elapsed = time.monotonic() - t0
                latencies.append(elapsed)
                assert c.connect_status == 200
                assert elapsed < 2.0, (
                    f'handshake took {elapsed:.3f}s — exceeds 2.0s cap')
                await ws.close()
        avg = sum(latencies) / len(latencies)
        assert avg < 0.5, (
            f'average handshake latency {avg*1000:.1f}ms exceeds 500ms cap')


# ---------------------------------------------------------------------------
# §5 — Dogfooding verification: the public client lives in `blackbull.client`
# ---------------------------------------------------------------------------

class TestDogfoodingVerification:
    """Meta-tests that lock in the property that the interop client is
    part of BlackBull itself — no external H2 or WS dependency.  These
    tests run by default (no `integration` marker)."""

    def test_client_lives_in_blackbull_client_package(self):
        import inspect
        from blackbull.client import WebSocketH2Client
        module = inspect.getmodule(WebSocketH2Client)
        assert module is not None
        assert module.__name__.startswith('blackbull.client'), (
            f'WebSocketH2Client must live under blackbull.client; '
            f'found in {module.__name__}')

    def test_client_uses_http2client_internally(self):
        import inspect
        from blackbull.client.websocket_h2 import WebSocketH2Client
        src = inspect.getsource(WebSocketH2Client)
        assert 'HTTP2Client' in src, (
            'WebSocketH2Client must reference HTTP2Client internally')

    def test_session_uses_ws_codec_encode_frame(self):
        import inspect
        from blackbull.client.websocket_h2 import WebSocketH2Session
        src = inspect.getsource(WebSocketH2Session)
        assert 'encode_frame' in src, (
            'WebSocketH2Session must use ws_codec.encode_frame for masking')
