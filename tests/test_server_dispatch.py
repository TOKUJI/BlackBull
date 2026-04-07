"""
Tests for server-side HTTP parsing and connection dispatch
==========================================================

HTTP1_1Handler.parse()
----------------------
HTTP1_1Handler.parse() builds an ASGI scope dict from raw HTTP/1.1 request
bytes.  It is responsible for detecting WebSocket upgrade requests and setting
scope['type'] = 'websocket'.  Bug 6 was that *after* parse() correctly set
scope['type']='websocket', WebsocketHandler.run() replaced the scope with
{'type': 'websocket.connect'}, dropping the path.  A test for parse() is the
first line of defence: if parse() itself sets the wrong type, WebsocketHandler
would never receive a correct scope.

client_connected_cb dispatch
-----------------------------
ASGIServer.client_connected_cb() reads the first line of the incoming stream
and decides which handler to instantiate (HTTP2Server, HTTP1_1Handler,
WebsocketHandler).  Testing this dispatch logic catches regressions where the
wrong handler is chosen, e.g. sending a WebSocket-upgrade HTTP request to
HTTP1_1Handler instead of WebsocketHandler.
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from blackbull.server.server import ASGIServer, WebsocketHandler, HTTP1_1Handler


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_scope(raw_request: bytes) -> dict:
    """Parse raw HTTP/1.1 request bytes and return the resulting scope dict.

    Uses HTTP1_1Handler.parse(), which superseded the deleted top-level parse().
    """
    handler = object.__new__(HTTP1_1Handler)
    handler.request = raw_request
    return handler.parse()


def _http_request(method='GET', path='/', version='HTTP/1.1',
                  headers: dict | None = None) -> bytes:
    """Build a minimal raw HTTP/1.1 request."""
    if headers is None:
        headers = {'Host': 'localhost:8000'}
    lines = [f'{method} {path} {version}']
    for k, v in headers.items():
        lines.append(f'{k}: {v}')
    lines.append('')
    lines.append('')
    return '\r\n'.join(lines).encode()


def _ws_request(path='/ws', host='localhost:8000') -> bytes:
    """Build a minimal HTTP/1.1 WebSocket upgrade request."""
    return _http_request(
        method='GET',
        path=path,
        headers={
            'Host': host,
            'Upgrade': 'websocket',
            'Connection': 'Upgrade',
            'Sec-WebSocket-Key': 'dGhlIHNhbXBsZSBub25jZQ==',
            'Sec-WebSocket-Version': '13',
        }
    )


# ---------------------------------------------------------------------------
# parse() – HTTP/1.1 scope building
# ---------------------------------------------------------------------------

class TestParse:
    """Unit tests for the top-level parse() function."""

    def test_method_is_extracted(self):
        scope = _get_scope(_http_request(method='GET'))
        assert scope['method'] == 'GET'

    def test_post_method(self):
        scope = _get_scope(_http_request(method='POST'))
        assert scope['method'] == 'POST'

    def test_path_is_extracted(self):
        scope = _get_scope(_http_request(path='/hello'))
        assert scope['path'] == '/hello'

    def test_root_path(self):
        scope = _get_scope(_http_request(path='/'))
        assert scope['path'] == '/'

    def test_http_version_is_extracted(self):
        scope = _get_scope(_http_request(version='HTTP/1.1'))
        assert scope['http_version'] == '1.1'

    def test_type_is_http_by_default(self):
        scope = _get_scope(_http_request())
        assert scope['type'] == 'http'

    def test_asgi_version_is_set(self):
        scope = _get_scope(_http_request())
        assert scope.get('asgi', {}).get('version') == '3.0'

    def test_server_host_and_port_are_parsed(self):
        scope = _get_scope(_http_request(headers={'Host': 'localhost:9000'}))
        assert scope['server'] == ['localhost', 9000]

    # --- WebSocket upgrade detection (upstream of Bug 6) ---

    def test_websocket_upgrade_sets_type_websocket(self):
        """HTTP1_1Handler.parse() must set scope['type']='websocket' for Upgrade: websocket.

        If parse() fails to detect the upgrade, the connection would be routed
        to HTTP1_1Handler instead of WebsocketHandler (Bug 6 root-cause).
        """
        scope = _get_scope(_ws_request())
        assert scope['type'] == 'websocket', (
            f"Expected type='websocket', got {scope['type']!r}. "
            "parse() must detect 'Upgrade: websocket' and set scope type."
        )

    def test_websocket_upgrade_sets_scheme_ws(self):
        """A WebSocket upgrade request must use scheme='ws'."""
        scope = _get_scope(_ws_request())
        assert scope['scheme'] == 'ws'

    def test_websocket_path_is_preserved(self):
        """parse() must keep the path for WebSocket requests.

        Bug 6 root-cause: if path is lost here the router can't dispatch.
        """
        scope = _get_scope(_ws_request(path='/chat'))
        assert scope['path'] == '/chat', (
            "parse() must preserve 'path' for WebSocket upgrade requests."
        )

    def test_headers_list_is_present(self):
        """scope['headers'] must be a list."""
        scope = _get_scope(_http_request())
        assert isinstance(scope['headers'], list)


# ---------------------------------------------------------------------------
# client_connected_cb dispatch
# ---------------------------------------------------------------------------

class _FakeReader:
    """Replay a pre-built byte string through the asyncio StreamReader API."""

    def __init__(self, data: bytes):
        self._buf = bytearray(data)

    async def readline(self) -> bytes:
        idx = self._buf.find(b'\n')
        if idx == -1:
            chunk, self._buf = bytes(self._buf), bytearray()
            return chunk
        chunk = bytes(self._buf[:idx + 1])
        del self._buf[:idx + 1]
        return chunk

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
        chunk = bytes(self._buf[:idx + len(sep)])
        del self._buf[:idx + len(sep)]
        return chunk

    async def readexactly(self, n: int) -> bytes:
        if len(self._buf) < n:
            raise asyncio.IncompleteReadError(bytes(self._buf), n)
        chunk = bytes(self._buf[:n])
        del self._buf[:n]
        return chunk


class _FakeTransport:
    """Minimal asyncio transport stub for transport.get_extra_info() calls."""

    def __init__(self, peername=None, sockname=None, ssl_object=None):
        self._extras = {
            'peername': peername,
            'sockname': sockname,
            'ssl_object': ssl_object,
        }

    def get_extra_info(self, key, default=None):
        return self._extras.get(key, default)


class _FakeWriter:
    def __init__(self, peername=('127.0.0.1', 54321),
                 sockname=('0.0.0.0', 8000), ssl_object=None):
        self.written = bytearray()
        self.closed = False
        self.transport = _FakeTransport(
            peername=peername,
            sockname=sockname,
            ssl_object=ssl_object,
        )

    def write(self, data: bytes):
        self.written += data

    async def drain(self):
        pass

    def close(self):
        self.closed = True

    async def wait_closed(self):
        pass


async def _noop_app(scope, receive, send):
    pass


class TestClientConnectedCbDispatch:
    """Test that client_connected_cb routes to the correct handler class."""

    @pytest.mark.asyncio
    async def test_websocket_request_dispatches_to_websocket_handler(self):
        """A WS-upgrade request must create a WebsocketHandler, not HTTP1_1Handler.

        This is the key regression test for Bug 6: the wrong handler type would
        either crash or fail to dispatch to the router's websocket endpoint.
        """
        raw = _ws_request(path='/ws')
        reader = _FakeReader(raw)
        writer = _FakeWriter()

        server = ASGIServer(_noop_app)
        dispatched_type = {}

        original_ws_init = WebsocketHandler.__init__

        def capturing_ws_init(self, *args, **kwargs):
            dispatched_type['handler'] = 'WebsocketHandler'
            original_ws_init(self, *args, **kwargs)

        with patch.object(WebsocketHandler, '__init__', capturing_ws_init):
            with patch.object(WebsocketHandler, 'run', new_callable=lambda: lambda self: asyncio.sleep(0)):
                await server.client_connected_cb(reader, writer)

        assert dispatched_type.get('handler') == 'WebsocketHandler', (
            "An HTTP Upgrade: websocket request must be routed to WebsocketHandler."
        )

    @pytest.mark.asyncio
    async def test_plain_http_request_dispatches_to_http11_handler(self):
        """A plain HTTP GET must create an HTTP1_1Handler."""
        raw = _http_request(method='GET', path='/hello')
        reader = _FakeReader(raw)
        writer = _FakeWriter()

        server = ASGIServer(_noop_app)
        dispatched_type = {}

        original_http_init = HTTP1_1Handler.__init__

        def capturing_http_init(self, *args, **kwargs):
            dispatched_type['handler'] = 'HTTP1_1Handler'
            original_http_init(self, *args, **kwargs)

        async def noop_run(self):
            pass

        with patch.object(HTTP1_1Handler, '__init__', capturing_http_init):
            with patch.object(HTTP1_1Handler, 'run', noop_run):
                await server.client_connected_cb(reader, writer)

        assert dispatched_type.get('handler') == 'HTTP1_1Handler', (
            "A plain HTTP GET must be routed to HTTP1_1Handler, not WebsocketHandler."
        )

    @pytest.mark.asyncio
    async def test_websocket_handler_receives_path_in_scope(self):
        """The scope forwarded to WebsocketHandler must contain the request path.

        Bug 6 upstream: if parse() strips the path, WebsocketHandler never
        sees it regardless of what run() does with the scope.
        """
        path = '/chat/room1'
        raw = _ws_request(path=path)
        reader = _FakeReader(raw)
        writer = _FakeWriter()

        server = ASGIServer(_noop_app)
        captured_scope = {}

        async def capturing_app(scope, receive, send):
            captured_scope.update(scope)

        server.app = capturing_app

        original_ws_init = WebsocketHandler.__init__

        def spy_init(self, *args, **kwargs):
            original_ws_init(self, *args, **kwargs)

        async def noop_run(self):
            # Simulate forwarding scope to app (as the fixed code does)
            await self.app(self.scope, self.receive, self.send)

        with patch.object(WebsocketHandler, 'run', noop_run):
            await server.client_connected_cb(reader, writer)

        assert captured_scope.get('path') == path, (
            f"Expected path={path!r} in scope, got {captured_scope.get('path')!r}."
        )

    @pytest.mark.asyncio
    async def test_writer_is_closed_after_connection(self):
        """client_connected_cb must always close the writer (even on error)."""
        raw = _http_request()
        reader = _FakeReader(raw)
        writer = _FakeWriter()

        server = ASGIServer(_noop_app)

        async def noop_run(self):
            pass

        with patch.object(HTTP1_1Handler, 'run', noop_run):
            await server.client_connected_cb(reader, writer)

        assert writer.closed, (
            "client_connected_cb must close the writer in its finally block."
        )


# ---------------------------------------------------------------------------
# scope['client'], scope['server'], scope['scheme'] population
# ---------------------------------------------------------------------------

class TestScopePopulation:
    """HTTP1_1Handler.run() must fill client, server, and scheme from the transport."""

    def _make_handler(self, raw: bytes, writer: _FakeWriter) -> HTTP1_1Handler:
        reader = _FakeReader(raw)
        handler = object.__new__(HTTP1_1Handler)
        handler.app = None
        handler.reader = reader
        handler.writer = writer
        handler.request = b''
        return handler

    @pytest.mark.asyncio
    async def test_client_is_set_from_transport_peername(self):
        """scope['client'] must reflect the remote address from the transport."""
        raw = _http_request()
        writer = _FakeWriter(peername=('192.168.1.10', 54321))
        handler = self._make_handler(raw, writer)

        captured = {}

        async def capture_app(scope, receive, send):
            captured.update(scope)

        handler.app = capture_app
        await handler.run()

        assert captured['client'] == ['192.168.1.10', 54321], (
            f"Expected client=['192.168.1.10', 54321], got {captured.get('client')!r}"
        )

    @pytest.mark.asyncio
    async def test_server_falls_back_to_sockname_when_no_host_header(self):
        """Without a Host header, scope['server'] must come from transport sockname."""
        raw = _http_request(headers={})  # no Host header
        writer = _FakeWriter(sockname=('0.0.0.0', 9000))
        handler = self._make_handler(raw, writer)

        captured = {}

        async def capture_app(scope, receive, send):
            captured.update(scope)

        handler.app = capture_app
        await handler.run()

        assert captured['server'] is not None, "scope['server'] must not be None"
        assert captured['server'][1] == 9000, (
            f"Expected port 9000 from sockname, got {captured['server']!r}"
        )

    @pytest.mark.asyncio
    async def test_server_from_host_header_takes_priority_over_sockname(self):
        """scope['server'] from Host: header must not be overwritten by sockname."""
        raw = _http_request(headers={'Host': 'example.com:8080'})
        writer = _FakeWriter(sockname=('0.0.0.0', 9999))
        handler = self._make_handler(raw, writer)

        captured = {}

        async def capture_app(scope, receive, send):
            captured.update(scope)

        handler.app = capture_app
        await handler.run()

        assert captured['server'] == ['example.com', 8080], (
            f"Host header must take priority; got {captured['server']!r}"
        )

    @pytest.mark.asyncio
    async def test_scheme_is_http_for_plain_connection(self):
        """scope['scheme'] must be 'http' when there is no TLS."""
        raw = _http_request()
        writer = _FakeWriter(ssl_object=None)
        handler = self._make_handler(raw, writer)

        captured = {}

        async def capture_app(scope, receive, send):
            captured.update(scope)

        handler.app = capture_app
        await handler.run()

        assert captured['scheme'] == 'http', (
            f"Expected scheme='http' for plain connection, got {captured['scheme']!r}"
        )

    @pytest.mark.asyncio
    async def test_scheme_is_https_for_tls_connection(self):
        """scope['scheme'] must be 'https' when the transport carries TLS."""
        raw = _http_request()
        # Any truthy value for ssl_object marks this as a TLS connection.
        writer = _FakeWriter(ssl_object=object())
        handler = self._make_handler(raw, writer)

        captured = {}

        async def capture_app(scope, receive, send):
            captured.update(scope)

        handler.app = capture_app
        await handler.run()

        assert captured['scheme'] == 'https', (
            f"Expected scheme='https' for TLS connection, got {captured['scheme']!r}"
        )

    @pytest.mark.asyncio
    async def test_scheme_is_wss_for_tls_websocket(self):
        """scope['scheme'] must be 'wss' for a WebSocket upgrade over TLS."""
        raw = _ws_request(path='/ws')
        writer = _FakeWriter(ssl_object=object())

        captured = {}

        async def capture_app(scope, receive, send):
            captured.update(scope)

        reader = _FakeReader(raw)
        handler = object.__new__(HTTP1_1Handler)
        handler.reader = reader
        handler.writer = writer
        handler.request = b''

        # Intercept WebsocketHandler.run to capture the scope it receives.
        from unittest.mock import patch

        async def noop_ws_run(self):
            captured.update(self.scope)

        with patch.object(WebsocketHandler, 'run', noop_ws_run):
            handler.app = capture_app
            await handler.run()

        assert captured.get('scheme') == 'wss', (
            f"Expected scheme='wss' for TLS WebSocket, got {captured.get('scheme')!r}"
        )
