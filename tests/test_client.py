"""
Tests for the protocol-layer client (blackbull/client/).
========================================================

The client targets a real ``ASGIServer`` started in the same event loop;
plaintext h2c (HTTP/2 over plain TCP) is used so no TLS context is needed.
"""
import asyncio
import pathlib
import ssl
from http import HTTPMethod, HTTPStatus

import pytest
import pytest_asyncio

from blackbull.server.server import ASGIServer
from blackbull.client.client import Client
from blackbull.client.http1 import HTTP1Client
from blackbull.client.http2 import HTTP2Client
from blackbull.client.websocket import WebSocketClient
from blackbull.client.exceptions import HandshakeError


# Sleep budget after `server.run()` is scheduled, before the fixture yields.
# Mirrors the pattern in tests/test_asgi_server.py and is generous enough
# that the listening socket has been bound on slow CI runners.
_SERVER_STARTUP_WAIT_SECONDS = 0.15

# Ephemeral-port marker for socket.bind — the OS assigns a free port.
_EPHEMERAL_PORT = 0


# ---------------------------------------------------------------------------
# Fixture: an ASGIServer + simple echo app, started on an ephemeral port
# ---------------------------------------------------------------------------

async def _echo_app(scope, receive, send):
    """Reflects the request body, with a few path-specific behaviours.

    - ``/error`` → ``INTERNAL_SERVER_ERROR`` with body ``b'oh no'``
    - ``/path``  → ``OK`` with body equal to the request path
    - default    → ``OK`` with body equal to the request body, else ``b'hello'``

    Also accepts WebSocket scopes — echoes every received frame back to the client.
    """
    if scope.get('type') == 'websocket':
        await receive()  # consume the synthetic 'websocket.connect' event
        await send({'type': 'websocket.accept'})
        while True:
            event = await receive()
            etype = event.get('type')
            if etype == 'websocket.disconnect':
                return
            if etype == 'websocket.receive':
                if event.get('text') is not None:
                    await send({'type': 'websocket.send', 'text': event['text']})
                else:
                    await send({'type': 'websocket.send',
                                'bytes': event.get('bytes', b'')})
        return

    if scope.get('type') != 'http':
        return

    if scope['path'] == '/error':
        await send({'type': 'http.response.start',
                    'status': HTTPStatus.INTERNAL_SERVER_ERROR,
                    'headers': [(b'content-type', b'text/plain')]})
        await send({'type': 'http.response.body', 'body': b'oh no'})
        return

    if scope['path'] == '/path':
        await send({'type': 'http.response.start', 'status': HTTPStatus.OK,
                    'headers': [(b'content-type', b'text/plain')]})
        await send({'type': 'http.response.body',
                    'body': scope['path'].encode()})
        return

    body_parts: list[bytes] = []
    while True:
        event = await receive()
        if event.get('type') == 'http.disconnect':
            return
        body_parts.append(event.get('body', b''))
        if not event.get('more_body', False):
            break
    body = b''.join(body_parts) or b'hello'
    await send({'type': 'http.response.start', 'status': HTTPStatus.OK,
                'headers': [(b'content-type', b'text/plain')]})
    await send({'type': 'http.response.body', 'body': body})


@pytest_asyncio.fixture
async def server_port():
    server = ASGIServer(_echo_app)
    server.open_socket(port=_EPHEMERAL_PORT)
    port = server.port

    task = asyncio.create_task(server.run())
    await asyncio.sleep(_SERVER_STARTUP_WAIT_SECONDS)

    try:
        yield port
    finally:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


# Cert/key files used by the TLS fixture (mirrors test_asgi_server.py).
@pytest.fixture
def cert_path():
    return pathlib.Path(__file__).parent / 'cert.pem'


@pytest.fixture
def key_path():
    return pathlib.Path(__file__).parent / 'key.pem'


@pytest_asyncio.fixture
async def server_tls_port(cert_path, key_path, manage_cert_and_key):
    server = ASGIServer(_echo_app, certfile=cert_path, keyfile=key_path)
    server.open_socket(port=_EPHEMERAL_PORT)
    port = server.port

    task = asyncio.create_task(server.run())
    await asyncio.sleep(_SERVER_STARTUP_WAIT_SECONDS)

    try:
        yield port, cert_path
    finally:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


# ---------------------------------------------------------------------------
# TestHTTP2Client — round-trips against a real ASGIServer
# ---------------------------------------------------------------------------

class TestHTTP2Client:
    @pytest.mark.asyncio
    async def test_get_returns_status_and_body(self, server_port):
        async with HTTP2Client('localhost', server_port) as c:
            res = await c.request(HTTPMethod.GET, '/')
        assert res.status == HTTPStatus.OK
        assert res.body == b'hello'

    @pytest.mark.asyncio
    async def test_path_reflected(self, server_port):
        async with HTTP2Client('localhost', server_port) as c:
            res = await c.request(HTTPMethod.GET, '/path')
        assert res.status == HTTPStatus.OK
        assert res.body == b'/path'

    @pytest.mark.asyncio
    async def test_post_with_body(self, server_port):
        async with HTTP2Client('localhost', server_port) as c:
            res = await c.request(HTTPMethod.POST, '/echo', body=b'world')
        assert res.status == HTTPStatus.OK
        assert res.body == b'world'

    @pytest.mark.asyncio
    async def test_error_status_propagates(self, server_port):
        async with HTTP2Client('localhost', server_port) as c:
            res = await c.request(HTTPMethod.GET, '/error')
        assert res.status == HTTPStatus.INTERNAL_SERVER_ERROR
        assert res.body == b'oh no'

    @pytest.mark.asyncio
    async def test_response_headers_are_bytes(self, server_port):
        async with HTTP2Client('localhost', server_port) as c:
            res = await c.request(HTTPMethod.GET, '/')
        ct = res.headers.get(b'content-type')
        assert ct == b'text/plain'

    @pytest.mark.asyncio
    async def test_two_concurrent_requests(self, server_port):
        async with HTTP2Client('localhost', server_port) as c:
            r1, r2 = await asyncio.gather(
                c.request(HTTPMethod.GET, '/path'),
                c.request(HTTPMethod.POST, '/echo', body=b'second'),
            )
        assert r1.status == HTTPStatus.OK
        assert r2.status == HTTPStatus.OK
        assert r1.body == b'/path'
        assert r2.body == b'second'

    @pytest.mark.asyncio
    async def test_context_manager_cleans_up(self, server_port):
        client = HTTP2Client('localhost', server_port)
        async with client as c:
            await c.request(HTTPMethod.GET, '/')
        # After exiting, the receive task must be cancelled or done.
        assert client._receive_task is None or client._receive_task.done()


# ---------------------------------------------------------------------------
# TestHTTP1Client — round-trips against a real ASGIServer (HTTP/1.1)
# ---------------------------------------------------------------------------

class TestHTTP1Client:
    @pytest.mark.asyncio
    async def test_get_returns_status_and_body(self, server_port):
        async with HTTP1Client('localhost', server_port) as c:
            res = await c.request(HTTPMethod.GET, '/')
        assert res.status == HTTPStatus.OK
        assert res.body == b'hello'

    @pytest.mark.asyncio
    async def test_path_reflected(self, server_port):
        async with HTTP1Client('localhost', server_port) as c:
            res = await c.request(HTTPMethod.GET, '/path')
        assert res.status == HTTPStatus.OK
        assert res.body == b'/path'

    @pytest.mark.asyncio
    async def test_post_with_bytes_body(self, server_port):
        async with HTTP1Client('localhost', server_port) as c:
            res = await c.request(HTTPMethod.POST, '/echo', body=b'hello-bytes')
        assert res.status == HTTPStatus.OK
        assert res.body == b'hello-bytes'

    @pytest.mark.asyncio
    async def test_post_with_streaming_body(self, server_port):
        async def stream_body():
            for chunk in (b'one ', b'two ', b'three'):
                yield chunk

        async with HTTP1Client('localhost', server_port) as c:
            res = await c.request(HTTPMethod.POST, '/echo', body=stream_body())
        assert res.status == HTTPStatus.OK
        assert res.body == b'one two three'

    @pytest.mark.asyncio
    async def test_error_status_propagates(self, server_port):
        async with HTTP1Client('localhost', server_port) as c:
            res = await c.request(HTTPMethod.GET, '/error')
        assert res.status == HTTPStatus.INTERNAL_SERVER_ERROR
        assert res.body == b'oh no'

    @pytest.mark.asyncio
    async def test_keep_alive_two_requests(self, server_port):
        async with HTTP1Client('localhost', server_port) as c:
            r1 = await c.request(HTTPMethod.GET, '/path')
            r2 = await c.request(HTTPMethod.POST, '/echo', body=b'second')
        assert r1.body == b'/path'
        assert r2.body == b'second'

    @pytest.mark.asyncio
    async def test_host_header_auto_injected(self, server_port):
        async with HTTP1Client('localhost', server_port) as c:
            res = await c.request(HTTPMethod.GET, '/')
        assert res.status == HTTPStatus.OK


# ---------------------------------------------------------------------------
# Wire-level unit tests for HTTP1RequestSender — no server needed
# ---------------------------------------------------------------------------

class TestHTTP1RequestSenderWire:
    """Inspect the bytes the sender emits without standing up a server."""

    @pytest.mark.asyncio
    async def test_bytes_body_sets_content_length(self):
        from blackbull.client.http1 import HTTP1RequestSender
        from blackbull.server.headers import Headers
        from blackbull.server.sender import AbstractWriter

        class _BytesWriter(AbstractWriter):
            def __init__(self):
                self.data = b''

            async def write(self, data):
                self.data += data

        w = _BytesWriter()
        s = HTTP1RequestSender(w)
        await s.send(HTTPMethod.POST, '/echo',
                     Headers([(b'host', b'localhost')]),
                     body=b'hello')
        assert b'content-length: 5' in w.data.lower()
        assert b'transfer-encoding' not in w.data.lower()
        assert w.data.endswith(b'hello')

    @pytest.mark.asyncio
    async def test_streaming_body_uses_chunked(self):
        from blackbull.client.http1 import HTTP1RequestSender
        from blackbull.server.headers import Headers
        from blackbull.server.sender import AbstractWriter

        class _BytesWriter(AbstractWriter):
            def __init__(self):
                self.data = b''

            async def write(self, data):
                self.data += data

        async def gen():
            yield b'aa'
            yield b'bb'

        w = _BytesWriter()
        s = HTTP1RequestSender(w)
        await s.send(HTTPMethod.POST, '/echo',
                     Headers([(b'host', b'localhost')]),
                     body=gen())
        assert b'transfer-encoding: chunked' in w.data.lower()
        assert b'content-length' not in w.data.lower()
        # 'aa' is 2 bytes -> "2\r\naa\r\n", 'bb' likewise, then "0\r\n\r\n"
        assert b'2\r\naa\r\n' in w.data
        assert b'2\r\nbb\r\n' in w.data
        assert w.data.endswith(b'0\r\n\r\n')


# ---------------------------------------------------------------------------
# TestWebSocketClient — round-trips against a real ASGIServer (WebSocket)
# ---------------------------------------------------------------------------

class TestWebSocketClient:
    @pytest.mark.asyncio
    async def test_handshake_completes(self, server_port):
        async with WebSocketClient('localhost', server_port) as c:
            ws = await c.connect('/ws')
            await ws.close()
        assert ws.subprotocol is None

    @pytest.mark.asyncio
    async def test_text_echo(self, server_port):
        async with WebSocketClient('localhost', server_port) as c:
            ws = await c.connect('/ws')
            await ws.send_text('hello')
            event = await ws.receive()
            await ws.close()
        assert event['type'] == 'websocket.receive'
        assert event['text'] == 'hello'

    @pytest.mark.asyncio
    async def test_binary_echo(self, server_port):
        async with WebSocketClient('localhost', server_port) as c:
            ws = await c.connect('/ws')
            await ws.send_bytes(b'\x00\x01\x02')
            event = await ws.receive()
            await ws.close()
        assert event['type'] == 'websocket.receive'
        assert event['bytes'] == b'\x00\x01\x02'

    @pytest.mark.asyncio
    async def test_close_handshake(self, server_port):
        async with WebSocketClient('localhost', server_port) as c:
            ws = await c.connect('/ws')
            await ws.close()
        # No exception raised — close() drained gracefully.

    @pytest.mark.asyncio
    async def test_subprotocol_negotiation(self, server_port):
        # Set the available subprotocols on the running app via attribute.
        # The server reads ``getattr(self.app, 'available_ws_protocols', [])``
        # so attaching to the function works.
        _echo_app.available_ws_protocols = [b'chat']  # type: ignore[attr-defined]
        try:
            async with WebSocketClient('localhost', server_port) as c:
                ws = await c.connect('/ws', subprotocols=[b'chat', b'other'])
                await ws.close()
            assert ws.subprotocol == b'chat'
        finally:
            del _echo_app.available_ws_protocols  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# TestClient — ALPN-dispatching front door
# ---------------------------------------------------------------------------

def _make_tls_client_context(cert_path: pathlib.Path,
                             alpn: list[str]) -> ssl.SSLContext:
    ctx = ssl.create_default_context(cafile=str(cert_path))
    ctx.check_hostname = False  # self-signed cert in tests/
    ctx.verify_mode = ssl.CERT_NONE
    ctx.set_alpn_protocols(alpn)
    return ctx


class TestClient:
    @pytest.mark.asyncio
    async def test_plaintext_defaults_to_http1(self, server_port):
        async with Client('localhost', server_port) as c:
            assert isinstance(c, HTTP1Client)
            res = await c.request(HTTPMethod.GET, '/')
        assert res.status == HTTPStatus.OK
        assert res.body == b'hello'

    @pytest.mark.asyncio
    async def test_alpn_picks_h2_when_advertised(self, server_tls_port):
        port, cert_path = server_tls_port
        ctx = _make_tls_client_context(cert_path, ['h2', 'http/1.1'])
        async with Client('localhost', port, ssl=ctx) as c:
            assert isinstance(c, HTTP2Client)
            res = await c.request(HTTPMethod.GET, '/')
        assert res.status == HTTPStatus.OK
        assert res.body == b'hello'

    @pytest.mark.asyncio
    async def test_alpn_falls_back_to_http1_when_no_h2(self, server_tls_port):
        # Client advertises only http/1.1; the server still offers both, but
        # ALPN selection requires overlap, so the connection must fall back.
        port, cert_path = server_tls_port
        ctx = _make_tls_client_context(cert_path, ['http/1.1'])
        async with Client('localhost', port, ssl=ctx) as c:
            assert isinstance(c, HTTP1Client)
            res = await c.request(HTTPMethod.GET, '/')
        assert res.status == HTTPStatus.OK
        assert res.body == b'hello'
