"""
Tests for server-side HTTP parsing and connection dispatch
==========================================================

HTTP11Handler.parse()
----------------------
HTTP11Handler.parse() builds an ASGI scope dict from raw HTTP/1.1 request
bytes.  It is responsible for detecting WebSocket upgrade requests and setting
scope['type'] = 'websocket'.  Bug 6 was that *after* parse() correctly set
scope['type']='websocket', WebSocketHandler.run() replaced the scope with
{'type': 'websocket.connect'}, dropping the path.  A test for parse() is the
first line of defence: if parse() itself sets the wrong type, WebSocketHandler
would never receive a correct scope.

client_connected_cb dispatch
-----------------------------
ASGIServer.client_connected_cb() reads the first line of the incoming stream
and decides which handler to instantiate (HTTP2Handler, HTTP11Handler,
WebSocketHandler).  Testing this dispatch logic catches regressions where the
wrong handler is chosen, e.g. sending a WebSocket-upgrade HTTP request to
HTTP11Handler instead of WebSocketHandler.
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from http import HTTPStatus

from blackbull.server.server import ASGIServer, WebSocketHandler, HTTP11Handler
from blackbull.server.parser import _make_scope as _make_http2_scope


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_scope(raw_request: bytes) -> dict:
    """Parse raw HTTP/1.1 request bytes and return the resulting scope dict.

    Uses HTTP11Handler.parse(), which superseded the deleted top-level parse().
    """
    handler = object.__new__(HTTP11Handler)
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

    def test_http_version_is_extracted_10(self):
        scope = _get_scope(_http_request(version='HTTP/1.0'))
        assert scope['http_version'] == '1.0'

    def test_http2_version_string_is_spec_compliant(self):
        """ASGI spec: http_version must be '2' for HTTP/2, not '2.0'."""
        scope = _make_http2_scope()
        assert scope['http_version'] == '2'

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
        """HTTP11Handler.parse() must set scope['type']='websocket' for Upgrade: websocket.

        If parse() fails to detect the upgrade, the connection would be routed
        to HTTP11Handler instead of WebSocketHandler (Bug 6 root-cause).
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

    def test_headers_is_iterable_of_pairs(self):
        """scope['headers'] must be an ASGI-compliant iterable of (bytes, bytes) pairs."""
        from blackbull.server.headers import Headers
        scope = _get_scope(_http_request())
        assert isinstance(scope['headers'], Headers)
        for name, value in scope['headers']:
            assert isinstance(name, bytes)
            assert isinstance(value, bytes)

    # P3 — scope['root_path']
    def test_root_path_is_present_in_scope(self):
        """scope must include 'root_path' (ASGI spec §3.2 HTTP connection scope)."""
        scope = _get_scope(_http_request())
        assert 'root_path' in scope, (
            "ASGI spec §3.2 requires scope['root_path']; it is currently commented out "
            "in parse() — uncomment or add it."
        )

    def test_root_path_default_is_empty_string(self):
        """scope['root_path'] must default to '' when the server is not mounted."""
        scope = _get_scope(_http_request())
        assert scope.get('root_path') == '', (
            f"Expected root_path=''; got {scope.get('root_path')!r}"
        )

    def test_root_path_from_x_forwarded_prefix(self):
        """X-Forwarded-Prefix: /api must set scope['root_path'] to '/api'."""
        scope = _get_scope(_http_request(
            headers={'Host': 'localhost:8000', 'X-Forwarded-Prefix': '/api'}
        ))
        assert scope.get('root_path') == '/api', (
            f"Expected root_path='/api' from X-Forwarded-Prefix; got {scope.get('root_path')!r}"
        )

    def test_root_path_nested_prefix(self):
        """X-Forwarded-Prefix: /api/v1 must set scope['root_path'] to '/api/v1'."""
        scope = _get_scope(_http_request(
            headers={'Host': 'localhost:8000', 'X-Forwarded-Prefix': '/api/v1'}
        ))
        assert scope.get('root_path') == '/api/v1', (
            f"Expected root_path='/api/v1' from X-Forwarded-Prefix; got {scope.get('root_path')!r}"
        )

    def test_root_path_not_set_when_prefix_absent(self):
        """When X-Forwarded-Prefix is absent, scope['root_path'] must remain ''."""
        scope = _get_scope(_http_request(
            headers={'Host': 'localhost:8000'}
        ))
        assert scope.get('root_path') == '', (
            f"Expected root_path='' when no X-Forwarded-Prefix; got {scope.get('root_path')!r}"
        )

    def test_path_excludes_query_string(self):
        """scope['path'] must be the path component only, without '?' or query string.

        Bug: HTTP11Handler.parse() sets scope['path'] = path.decode() where
        path is the raw URI from the request line.  For /tasks?done=true the
        raw URI includes the query string, so the router gets '/tasks?done=true'
        and fails to match the registered '/tasks' route.
        """
        scope = _get_scope(_http_request(path='/tasks?done=true'))
        assert scope['path'] == '/tasks', (
            f"scope['path'] must not include query string; got {scope['path']!r}"
        )

    def test_query_string_populated_when_present(self):
        """scope['query_string'] must contain the bytes after '?'."""
        scope = _get_scope(_http_request(path='/tasks?done=true&page=2'))
        assert scope['query_string'] == b'done=true&page=2', (
            f"Expected query_string=b'done=true&page=2'; got {scope['query_string']!r}"
        )

    def test_query_string_empty_bytes_when_absent(self):
        """scope['query_string'] must be b'' when there is no query string."""
        scope = _get_scope(_http_request(path='/tasks'))
        assert scope['query_string'] == b'', (
            f"Expected empty query_string; got {scope['query_string']!r}"
        )


# ---------------------------------------------------------------------------
# HTTP/2 scope fields
# ---------------------------------------------------------------------------

def _make_h2_headers_frame(extra_headers: list | None = None) -> object:
    """Build and load an HTTP/2 HEADERS frame through FrameFactory.

    Returns the decoded frame object (with .pseudo_headers and .headers
    populated by HPACK decode) ready to pass to HTTP2HEADParser.
    """
    from hpack import Encoder
    from blackbull.protocol.frame import FrameFactory, FrameTypes, HeaderFrameFlags

    encoder = Encoder()
    header_list = [
        (b':method', b'GET'),
        (b':path',   b'/'),
        (b':scheme', b'https'),
    ]
    if extra_headers:
        header_list.extend(extra_headers)

    block = encoder.encode(header_list)
    flags = HeaderFrameFlags.END_HEADERS | HeaderFrameFlags.END_STREAM
    stream_id = 1
    raw = (len(block).to_bytes(3, 'big')
           + FrameTypes.HEADERS          # bytes enum value
           + bytes([flags])
           + stream_id.to_bytes(4, 'big')
           + block)

    return FrameFactory().load(raw)


class _FakeStream:
    identifier = 1


class TestHTTP2ScopeFields:
    """HTTP2HEADParser.parse() must populate scope fields correctly."""

    def test_root_path_default_empty_string(self):
        """scope['root_path'] must be '' when no X-Forwarded-Prefix header is present."""
        from blackbull.server.parser import ParserFactory
        frame = _make_h2_headers_frame()
        scope = ParserFactory.Get(frame, _FakeStream()).parse()
        assert scope.get('root_path') == '', (
            f"Expected root_path=''; got {scope.get('root_path')!r}"
        )

    def test_root_path_from_x_forwarded_prefix(self):
        """x-forwarded-prefix: /api must set scope['root_path'] to '/api'.

        Bug: HTTP2HEADParser.parse() calls _make_scope() which hard-codes
        root_path='' and never reads any header — so a reverse-proxy prefix
        is silently ignored for HTTP/2 connections.
        """
        from blackbull.server.parser import ParserFactory
        frame = _make_h2_headers_frame(
            extra_headers=[(b'x-forwarded-prefix', b'/api')]
        )
        scope = ParserFactory.Get(frame, _FakeStream()).parse()
        assert scope.get('root_path') == '/api', (
            f"Expected root_path='/api' from x-forwarded-prefix; "
            f"got {scope.get('root_path')!r}"
        )

    def test_root_path_nested_prefix(self):
        """x-forwarded-prefix: /api/v2 must set scope['root_path'] to '/api/v2'."""
        from blackbull.server.parser import ParserFactory
        frame = _make_h2_headers_frame(
            extra_headers=[(b'x-forwarded-prefix', b'/api/v2')]
        )
        scope = ParserFactory.Get(frame, _FakeStream()).parse()
        assert scope.get('root_path') == '/api/v2', (
            f"Expected root_path='/api/v2'; got {scope.get('root_path')!r}"
        )


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
        """A WS-upgrade request must create a WebSocketHandler, not HTTP11Handler.

        This is the key regression test for Bug 6: the wrong handler type would
        either crash or fail to dispatch to the router's websocket endpoint.
        """
        raw = _ws_request(path='/ws')
        reader = _FakeReader(raw)
        writer = _FakeWriter()

        server = ASGIServer(_noop_app)
        dispatched_type = {}

        original_ws_init = WebSocketHandler.__init__

        def capturing_ws_init(self, *args, **kwargs):
            dispatched_type['handler'] = 'WebSocketHandler'
            original_ws_init(self, *args, **kwargs)

        with patch.object(WebSocketHandler, '__init__', capturing_ws_init):
            with patch.object(WebSocketHandler, 'run', new_callable=lambda: lambda self: asyncio.sleep(0)):
                await server.client_connected_cb(reader, writer)

        assert dispatched_type.get('handler') == 'WebSocketHandler', (
            "An HTTP Upgrade: websocket request must be routed to WebSocketHandler."
        )

    @pytest.mark.asyncio
    async def test_plain_http_request_dispatches_to_http11_handler(self):
        """A plain HTTP GET must create an HTTP11Handler."""
        raw = _http_request(method='GET', path='/hello')
        reader = _FakeReader(raw)
        writer = _FakeWriter()

        server = ASGIServer(_noop_app)
        dispatched_type = {}

        original_http_init = HTTP11Handler.__init__

        def capturing_http_init(self, *args, **kwargs):
            dispatched_type['handler'] = 'HTTP11Handler'
            original_http_init(self, *args, **kwargs)

        async def noop_run(self):
            pass

        with patch.object(HTTP11Handler, '__init__', capturing_http_init):
            with patch.object(HTTP11Handler, 'run', noop_run):
                await server.client_connected_cb(reader, writer)

        assert dispatched_type.get('handler') == 'HTTP11Handler', (
            "A plain HTTP GET must be routed to HTTP11Handler, not WebSocketHandler."
        )

    @pytest.mark.asyncio
    async def test_websocket_handler_receives_path_in_scope(self):
        """The scope forwarded to WebSocketHandler must contain the request path.

        Bug 6 upstream: if parse() strips the path, WebSocketHandler never
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

        original_ws_init = WebSocketHandler.__init__

        def spy_init(self, *args, **kwargs):
            original_ws_init(self, *args, **kwargs)

        async def noop_run(self, **kwargs):
            # Simulate forwarding scope to app (as the fixed code does)
            async def _noop_send(event, *args, **kwargs): pass
            async def _noop_receive(): return {}
            await self.app(self.scope, _noop_receive, _noop_send)

        with patch.object(WebSocketHandler, 'run', noop_run):
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

        with patch.object(HTTP11Handler, 'run', noop_run):
            await server.client_connected_cb(reader, writer)

        assert writer.closed, (
            "client_connected_cb must close the writer in its finally block."
        )


# ---------------------------------------------------------------------------
# scope['client'], scope['server'], scope['scheme'] population
# ---------------------------------------------------------------------------

class TestScopePopulation:
    """HTTP11Handler.run() must fill client, server, and scheme from the transport."""

    def _make_handler(self, raw: bytes, writer) -> HTTP11Handler:
        reader = _FakeReader(raw)
        handler = object.__new__(HTTP11Handler)
        handler.app = None
        handler.reader = reader
        handler.writer = writer
        handler.request = b''
        return handler

    @pytest.mark.asyncio
    async def test_client_is_set_from_transport_peername(self):
        """scope['client'] must reflect the remote address from the transport."""
        raw = _http_request()
        writer = _make_stream_writer_mock(peername=('192.168.1.10', 54321))
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
        writer = _make_stream_writer_mock(sockname=('0.0.0.0', 9000))
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
        writer = _make_stream_writer_mock(sockname=('0.0.0.0', 9999))
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
        writer = _make_stream_writer_mock(ssl_object=None)
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
        writer = _make_stream_writer_mock(ssl_object=object())
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
    async def test_scheme_is_wss_for_tls_websocket(self):  # noqa: E303
        """scope['scheme'] must be 'wss' for a WebSocket upgrade over TLS."""
        raw = _ws_request(path='/ws')
        writer = _make_stream_writer_mock(ssl_object=object())

        captured = {}

        async def capture_app(scope, receive, send):
            captured.update(scope)

        reader = _FakeReader(raw)
        handler = object.__new__(HTTP11Handler)
        handler.reader = reader
        handler.writer = writer
        handler.request = b''

        async def noop_ws_run(self, **kwargs):
            captured.update(self.scope)

        with patch.object(WebSocketHandler, 'run', noop_ws_run):
            handler.app = capture_app
            await handler.run()

        assert captured.get('scheme') == 'wss', (
            f"Expected scheme='wss' for TLS WebSocket, got {captured.get('scheme')!r}"
        )


# ---------------------------------------------------------------------------
# make_sender – ASGI event dict → HTTP/1.1 wire bytes
# ---------------------------------------------------------------------------

def _make_stream_writer_mock(peername=('127.0.0.1', 54321),
                              sockname=('0.0.0.0', 8000),
                              ssl_object=None):
    """Return a MagicMock that satisfies the AsyncioWriter duck-type contract.

    AsyncioWriter only requires write() and drain(), so a MagicMock with those
    attrs configured is sufficient — no asyncio internals needed.
    The ``transport`` attribute mirrors the _FakeTransport API so that
    HTTP11Handler.run() can call transport.get_extra_info('peername') etc.
    """
    sw = MagicMock()
    sw.transport = _FakeTransport(peername=peername, sockname=sockname, ssl_object=ssl_object)
    sw.written = bytearray()
    sw.write = MagicMock(side_effect=lambda data: sw.written.extend(data))
    sw.drain = AsyncMock()
    return sw


class TestMakeSender:
    """HTTP11Handler.make_sender() must serialize ASGI event dicts to HTTP/1.1
    wire-format bytes, not Python dict repr.

    The ASGI HTTP send interface uses two event types:
      * 'http.response.start' → status line + headers, terminated by CRLF CRLF
      * 'http.response.body'  → raw body bytes

    Current bug: make_sender() falls through to ``str(x).encode()`` for any
    non-bytes value, writing Python dict repr to the socket instead of valid HTTP.
    """

    def _make_handler(self):
        sw = _make_stream_writer_mock()
        handler = object.__new__(HTTP11Handler)
        handler.app = None
        handler.reader = None
        handler.writer = sw
        handler.request = b''
        return handler, sw

    @pytest.mark.asyncio
    async def test_response_start_writes_status_line(self):
        """'http.response.start' + 'http.response.body' must produce a valid HTTP/1.1 status line."""
        handler, sw = self._make_handler()
        send = handler.make_sender()

        await send({'type': 'http.response.start', 'status': 200, 'headers': []})
        await send({'type': 'http.response.body', 'body': b''})

        written = bytes(sw.written)
        assert written.startswith(b'HTTP/1.1 200'), (
            f"Expected status line starting with b'HTTP/1.1 200', got {written[:40]!r}"
        )

    @pytest.mark.asyncio
    async def test_response_start_writes_reason_phrase(self):
        """'http.response.start' + 'http.response.body' must include the standard reason phrase."""
        handler, sw = self._make_handler()
        send = handler.make_sender()

        await send({'type': 'http.response.start', 'status': 404, 'headers': []})
        await send({'type': 'http.response.body', 'body': b''})

        written = bytes(sw.written)
        assert b'Not Found' in written, (
            f"Expected '404 Not Found' in status line, got {written[:60]!r}"
        )

    @pytest.mark.asyncio
    async def test_response_start_writes_headers(self):
        """'http.response.start' + body must write each header as 'Name: Value\\r\\n'."""
        handler, sw = self._make_handler()
        send = handler.make_sender()

        await send({
            'type': 'http.response.start',
            'status': 200,
            'headers': [
                (b'content-type', b'text/plain'),
                (b'content-length', b'5'),
            ],
        })
        await send({'type': 'http.response.body', 'body': b''})

        written = bytes(sw.written)
        assert b'content-type: text/plain\r\n' in written, (
            f"Expected 'content-type: text/plain\\r\\n' in response, got {written!r}"
        )
        assert b'content-length: 5\r\n' in written, (
            f"Expected 'content-length: 5\\r\\n' in response, got {written!r}"
        )

    @pytest.mark.asyncio
    async def test_response_start_ends_with_blank_line(self):
        """After start+body, the header section must be terminated with CRLFCRLF."""
        handler, sw = self._make_handler()
        send = handler.make_sender()

        await send({'type': 'http.response.start', 'status': 200, 'headers': []})
        await send({'type': 'http.response.body', 'body': b''})

        written = bytes(sw.written)
        assert b'\r\n\r\n' in written, (
            f"Header block must contain \\r\\n\\r\\n, got {written!r}"
        )

    @pytest.mark.asyncio
    async def test_response_body_writes_bytes(self):
        """'http.response.body' must write body bytes verbatim to the socket."""
        handler, sw = self._make_handler()
        send = handler.make_sender()

        body = b'Hello, world!'
        await send({'type': 'http.response.body', 'body': body, 'more_body': False})

        written = bytes(sw.written)
        assert body in written, (
            f"Expected body bytes {body!r} to appear in output, got {written!r}"
        )

    @pytest.mark.asyncio
    async def test_full_response_is_valid_http11(self):
        """Sending start then body must produce a complete, parseable HTTP/1.1 response."""
        handler, sw = self._make_handler()
        send = handler.make_sender()

        await send({
            'type': 'http.response.start',
            'status': 200,
            'headers': [(b'content-length', b'2')],
        })
        await send({
            'type': 'http.response.body',
            'body': b'OK',
            'more_body': False,
        })

        wire = bytes(sw.written)
        assert wire.startswith(b'HTTP/1.1 200'), wire[:60]
        assert b'\r\n\r\n' in wire, f"Missing header/body separator in {wire!r}"
        header_part, body_part = wire.split(b'\r\n\r\n', 1)
        assert body_part == b'OK', (
            f"Expected body b'OK', got {body_part!r}"
        )

    @pytest.mark.asyncio
    async def test_response_start_is_not_dict_repr(self):
        """make_sender must NOT write Python dict repr to the socket.

        This is the concrete regression check for the bug: the old fallback
        ``str(x).encode()`` produced b\"{'type': 'http.response.start', ...}\"
        which is not valid HTTP.
        """
        handler, sw = self._make_handler()
        send = handler.make_sender()

        await send({'type': 'http.response.start', 'status': 200, 'headers': []})
        await send({'type': 'http.response.body', 'body': b''})

        written = bytes(sw.written)
        assert not written.startswith(b'{'), (
            f"make_sender wrote Python dict repr instead of HTTP wire format: {written[:80]!r}"
        )


# ---------------------------------------------------------------------------
# P2 — HTTP/1.1 Keep-Alive
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestHTTP11KeepAlive:
    """HTTP11Handler must loop and process multiple requests on one connection.

    P2 bug: run() calls the app once then returns, closing the connection.
    Keep-Alive requires re-reading request headers and calling the app again
    until the client sends Connection: close or the connection drops.
    """

    async def test_two_requests_on_one_connection_calls_app_twice(self):
        """Two back-to-back pipelined requests must each reach the app."""
        req1 = _http_request(method='GET', path='/one',
                             headers={'Host': 'localhost:8000',
                                      'Connection': 'keep-alive'})
        req2 = _http_request(method='GET', path='/two',
                             headers={'Host': 'localhost:8000',
                                      'Connection': 'close'})
        # Simulate client_connected_cb: the first line of req1 is pre-consumed
        # and passed to the handler; the reader holds only the remaining bytes.
        req1_first_line, req1_rest = req1.split(b'\r\n', 1)
        reader = _FakeReader(req1_rest + req2)
        writer = _FakeWriter()

        call_count = 0

        async def counting_app(scope, receive, send):
            nonlocal call_count
            call_count += 1

        handler = HTTP11Handler(counting_app, reader, writer,
                                req1_first_line + b'\r\n')
        await handler.run()

        assert call_count == 2, (
            f'Expected app called twice (once per request), got {call_count}. '
            'HTTP/1.1 Keep-Alive requires looping over requests on one connection.'
        )

    async def test_connection_close_ends_loop(self):
        """Connection: close on first request — app called exactly once."""
        req = _http_request(method='GET', path='/',
                            headers={'Host': 'localhost:8000',
                                     'Connection': 'close'})
        first_line, rest = req.split(b'\r\n', 1)
        reader = _FakeReader(rest)
        writer = _FakeWriter()

        call_count = 0

        async def counting_app(scope, receive, send):
            nonlocal call_count
            call_count += 1

        handler = HTTP11Handler(counting_app, reader, writer,
                                first_line + b'\r\n')
        await handler.run()

        assert call_count == 1

    async def test_keep_alive_paths_differ_between_requests(self):
        """Each request in a Keep-Alive connection must have its own scope path."""
        req1 = _http_request(method='GET', path='/alpha',
                             headers={'Host': 'localhost:8000',
                                      'Connection': 'keep-alive'})
        req2 = _http_request(method='GET', path='/beta',
                             headers={'Host': 'localhost:8000',
                                      'Connection': 'close'})
        req1_first_line, req1_rest = req1.split(b'\r\n', 1)
        reader = _FakeReader(req1_rest + req2)
        writer = _FakeWriter()

        paths = []

        async def capturing_app(scope, receive, send):
            paths.append(scope['path'])

        handler = HTTP11Handler(capturing_app, reader, writer,
                                req1_first_line + b'\r\n')
        await handler.run()

        assert '/alpha' in paths and '/beta' in paths, (
            f'Expected both paths in scope; got {paths}'
        )

    async def test_incomplete_read_on_first_request_closes_silently(self):
        """IncompleteReadError during the first request's headers must close silently.

        A real asyncio.StreamReader raises IncompleteReadError when the client
        closes the connection before the full header block arrives.  This is a
        normal network event — run() must catch it and return without raising,
        so client_connected_cb does not log a spurious error.
        """
        req = _http_request(method='GET', path='/',
                            headers={'Host': 'localhost:8000'})
        first_line, _ = req.split(b'\r\n', 1)
        writer = _FakeWriter()

        reader = MagicMock()
        reader.readuntil = AsyncMock(
            side_effect=asyncio.IncompleteReadError(b'Host: localh', None))

        handler = HTTP11Handler(_noop_app, reader, writer, first_line + b'\r\n')

        await handler.run()  # must return normally, not raise

    async def test_incomplete_read_after_first_request_app_called_once(self):
        """IncompleteReadError on second request must not affect the first.

        The client completes one full request then drops the connection mid-way
        through sending the second request's headers.  The app must have been
        called exactly once, and run() must return normally.
        """
        req = _http_request(method='GET', path='/one',
                            headers={'Host': 'localhost:8000',
                                     'Connection': 'keep-alive'})
        first_line, rest = req.split(b'\r\n', 1)
        writer = _FakeWriter()

        call_count = 0

        async def counting_app(scope, receive, send):
            nonlocal call_count
            call_count += 1

        reader = MagicMock()
        reader.readuntil = AsyncMock(side_effect=[
            rest,                                                      # first request headers
            asyncio.IncompleteReadError(b'GET /two HTTP', None),       # disconnect mid-second request
        ])
        reader.read = AsyncMock(return_value=b'')

        handler = HTTP11Handler(counting_app, reader, writer, first_line + b'\r\n')

        await handler.run()  # must return normally, not raise

        assert call_count == 1, (
            f'Expected app called once before disconnect; got {call_count}'
        )


# ---------------------------------------------------------------------------
# P2 — HTTP/1.1 duplicate headers (RFC 7230 §3.2.2)
# ---------------------------------------------------------------------------

class TestHTTP11DuplicateHeaders:
    """HTTP11Handler.parse() must preserve every occurrence of a repeated header.

    P2 bug: parse() accumulates headers into a plain dict with ``mapping[key]
    = value``, so the second occurrence silently overwrites the first.
    RFC 7230 §3.2.2 allows multiple fields with the same name; they must all
    appear in scope['headers'] as separate tuples.
    """

    def _raw_with_duplicate(self, name: str, values: list[str]) -> bytes:
        lines = ['GET / HTTP/1.1', 'Host: localhost:8000']
        for v in values:
            lines.append(f'{name}: {v}')
        lines += ['', '']
        return '\r\n'.join(lines).encode()

    def test_single_header_preserved(self):
        """Baseline: a single header appears in scope['headers']."""
        scope = _get_scope(_http_request(headers={'Host': 'localhost:8000',
                                                  'Accept': 'text/html'}))
        keys = [k for k, _ in scope['headers']]
        assert b'accept' in keys

    def test_two_set_cookie_both_in_headers(self):
        """Two Set-Cookie headers must both appear in scope['headers'].

        P2 bug: only the last value is kept when a dict is used.
        """
        raw = self._raw_with_duplicate('Set-Cookie', ['a=1', 'b=2'])
        scope = _get_scope(raw)
        cookie_values = [v for k, v in scope['headers'] if k == b'set-cookie']
        assert len(cookie_values) == 2, (
            f'Expected 2 Set-Cookie entries, got {cookie_values!r}. '
            'Duplicate headers must not be silently overwritten.'
        )

    def test_duplicate_accept_both_preserved(self):
        """Two Accept headers must both appear — not merged or dropped."""
        raw = self._raw_with_duplicate('Accept', ['text/html', 'application/json'])
        scope = _get_scope(raw)
        accept_values = [v for k, v in scope['headers'] if k == b'accept']
        assert len(accept_values) == 2, (
            f'Expected 2 Accept entries, got {accept_values!r}.'
        )

    def test_first_value_not_overwritten(self):
        """The first value of a duplicate header must survive in scope['headers']."""
        raw = self._raw_with_duplicate('X-Custom', ['first', 'second'])
        scope = _get_scope(raw)
        values = [v for k, v in scope['headers'] if k == b'x-custom']
        assert b'first' in values, (
            f'First header value lost: {values!r}. dict assignment silently overwrites it.'
        )


# ---------------------------------------------------------------------------
# P2 — ASGI http.disconnect
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestHTTPDisconnect:
    """HTTP11Handler.receive() must return {'type': 'http.disconnect'} on EOF.

    P2 item: ASGI spec §2.3 requires the receive() callable to return an
    http.disconnect event when the client closes the connection.  Currently
    ``reader.read()`` raises IncompleteReadError or returns b'', which
    propagates as an unhandled exception rather than a clean ASGI event.
    """

    async def test_receive_returns_disconnect_on_eof(self):
        """IncompleteReadError from the reader must become http.disconnect."""
        # Content-Length: 10 ensures the recipient calls reader.read(10),
        # which raises IncompleteReadError — the disconnect scenario under test.
        raw = _http_request(headers={'Host': 'localhost:8000', 'Content-Length': '10'})
        writer = _FakeWriter()
        disconnect_received = []

        async def app(scope, receive, send):
            event = await receive()
            disconnect_received.append(event)

        reader = MagicMock()
        reader.readuntil = AsyncMock(return_value=b'\r\n\r\n')
        reader.read = AsyncMock(
            side_effect=asyncio.IncompleteReadError(b'', 10)
        )

        handler = HTTP11Handler(app, reader, writer, b'GET / HTTP/1.1\r\n')
        handler.request = raw
        await handler.run()

        assert any(e.get('type') == 'http.disconnect'
                   for e in disconnect_received), (
            f'Expected http.disconnect event; got {disconnect_received}'
        )

    async def test_disconnect_event_has_correct_type(self):
        """The disconnect event dict must have type == 'http.disconnect'."""
        raw = _http_request(headers={'Host': 'localhost:8000',
                                     'Content-Length': '5'})
        writer = _FakeWriter()
        events = []

        async def app(scope, receive, send):
            event = await receive()
            events.append(event)

        reader = MagicMock()
        reader.readuntil = AsyncMock(return_value=b'\r\n\r\n')
        reader.read = AsyncMock(
            side_effect=asyncio.IncompleteReadError(b'hel', 5)
        )

        handler = HTTP11Handler(app, reader, writer, b'GET / HTTP/1.1\r\n')
        handler.request = raw
        await handler.run()

        types = [e.get('type') for e in events]
        assert 'http.disconnect' in types, (
            f'Received events: {types}'
        )


# ---------------------------------------------------------------------------
# P2 — HTTP/1.1 100 Continue
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestHTTP11Expect100Continue:
    """When Expect: 100-continue is present the server must send 100 first.

    P2 item: RFC 7231 §5.1.1 — a server that receives Expect: 100-continue
    on a request with a body SHOULD send ``HTTP/1.1 100 Continue\r\n\r\n``
    before reading the request body.  This lets the client know it should
    proceed with sending the (potentially large) body.

    Current bug: no interim response is sent; the server reads the body
    immediately without signalling the client.
    """

    def _make_expect_request(self, body: bytes = b'hello') -> bytes:
        lines = [
            'POST /upload HTTP/1.1',
            'Host: localhost:8000',
            f'Content-Length: {len(body)}',
            'Expect: 100-continue',
            '', '',
        ]
        return '\r\n'.join(lines).encode() + body

    async def test_100_continue_sent_before_body(self):
        """'100 Continue' must appear in writer output before the body is read."""
        body = b'hello'
        raw = self._make_expect_request(body)
        writer = _FakeWriter()
        body_read_position = None

        async def app(scope, receive, send):
            nonlocal body_read_position
            body_read_position = bytes(writer.written)
            await receive()

        reader = MagicMock()
        reader.readuntil = AsyncMock(return_value=b'\r\n\r\n')
        reader.read = AsyncMock(return_value=body)

        handler = HTTP11Handler(app, reader, writer, b'POST /upload HTTP/1.1\r\n')
        handler.request = raw
        await handler.run()

        assert body_read_position is not None
        assert b'100' in body_read_position, (
            f'Expected "100 Continue" to be written before body is read; '
            f'wire bytes at receive() time: {body_read_position!r}'
        )

    async def test_body_is_read_after_100(self):
        """The actual request body must still be returned from receive()."""
        body = b'payload'
        raw = self._make_expect_request(body)
        writer = _FakeWriter()
        received_body = None

        async def app(scope, receive, send):
            nonlocal received_body
            event = await receive()
            received_body = event.get('body')

        reader = MagicMock()
        reader.readuntil = AsyncMock(return_value=b'\r\n\r\n')
        reader.read = AsyncMock(return_value=body)

        handler = HTTP11Handler(app, reader, writer, b'POST /upload HTTP/1.1\r\n')
        handler.request = raw
        await handler.run()

        assert received_body == body, (
            f'Expected body={body!r}; got {received_body!r}'
        )

    async def test_no_100_without_expect_header(self):
        """A normal POST must not receive a spurious 100 Continue."""
        body = b'data'
        lines = ['POST / HTTP/1.1', 'Host: localhost:8000',
                 f'Content-Length: {len(body)}', '', '']
        raw = '\r\n'.join(lines).encode() + body
        writer = _FakeWriter()

        async def noop_app(scope, receive, send):
            await receive()

        reader = MagicMock()
        reader.readuntil = AsyncMock(return_value=b'\r\n\r\n')
        reader.read = AsyncMock(return_value=body)

        handler = HTTP11Handler(noop_app, reader, writer, b'POST / HTTP/1.1\r\n')
        handler.request = raw
        await handler.run()

        written = bytes(writer.written)
        assert b'100' not in written, (
            f'Spurious 100 Continue sent without Expect header: {written!r}'
        )

    async def test_case_insensitive_expect(self):
        """Expect: 100-Continue (mixed case) must still trigger 100 Continue."""
        lines = ['POST /upload HTTP/1.1', 'Host: localhost:8000',
                 'Content-Length: 5', 'Expect: 100-Continue', '', '']
        raw = '\r\n'.join(lines).encode()
        writer = _FakeWriter()

        async def app(scope, receive, send):
            await receive()

        reader = MagicMock()
        reader.readuntil = AsyncMock(return_value=b'\r\n\r\n')
        reader.read = AsyncMock(return_value=b'hello')

        handler = HTTP11Handler(app, reader, writer, b'POST /upload HTTP/1.1\r\n')
        handler.request = raw
        await handler.run()

        assert b'100' in bytes(writer.written), (
            f'Expected 100 Continue for mixed-case Expect header; '
            f'got {bytes(writer.written)!r}'
        )

    async def test_final_response_after_100(self):
        """After 100 Continue the app must be able to send a final 200 response.

        The wire output must contain exactly two HTTP/1.1 status lines:
        the interim 100 and the final 200.
        """
        body = b'data'
        raw = self._make_expect_request(body)
        writer = _FakeWriter()

        async def app(scope, receive, send):  # pyright: ignore[reportUnusedVariable]
            await receive()
            await send({'type': 'http.response.start', 'status': 200, 'headers': []})
            await send({'type': 'http.response.body', 'body': b'ok'})

        reader = MagicMock()
        reader.readuntil = AsyncMock(return_value=b'\r\n\r\n')
        reader.read = AsyncMock(return_value=body)

        handler = HTTP11Handler(app, reader, writer, b'POST /upload HTTP/1.1\r\n')
        handler.request = raw
        await handler.run()

        wire = bytes(writer.written)
        assert wire.count(b'HTTP/1.1') == 2, (
            f'Expected exactly 2 HTTP/1.1 lines (100 + 200); got: {wire!r}'
        )
        assert b'HTTP/1.1 200' in wire, f'No final 200 found: {wire!r}'


# ---------------------------------------------------------------------------
# P3 — HTTP/1.1 auto Content-Length and Date headers
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestHTTP11AutoHeaders:
    """HTTP1Sender must automatically inject Content-Length and Date when the
    app does not supply them (RFC 7230 §3.3.2, RFC 7231 §7.1.1.2).

    Rationale: client parsers rely on Content-Length to delimit the response
    body, and Date lets caches and clients compute freshness.  The server is
    the only party that knows the final body length (after possible buffering)
    and the current time at send time.
    """

    def _make_handler(self):
        sw = _make_stream_writer_mock()
        handler = object.__new__(HTTP11Handler)
        handler.app = None
        handler.reader = None
        handler.writer = sw
        handler.request = b''
        return handler, sw

    async def test_content_length_injected_when_absent(self):
        """When the app sends no Content-Length, the response must include one."""
        handler, sw = self._make_handler()
        send = handler.make_sender()

        await send({'type': 'http.response.start', 'status': 200, 'headers': []})
        await send({'type': 'http.response.body', 'body': b'hello', 'more_body': False})

        wire = bytes(sw.written).lower()
        assert b'content-length:' in wire, (
            f'Expected auto-injected Content-Length header; got {wire!r}'
        )

    async def test_content_length_value_matches_body(self):
        """The injected Content-Length value must equal the actual body length."""
        handler, sw = self._make_handler()
        send = handler.make_sender()
        body = b'hello world'

        await send({'type': 'http.response.start', 'status': 200, 'headers': []})
        await send({'type': 'http.response.body', 'body': body, 'more_body': False})

        wire = bytes(sw.written).lower()
        assert f'content-length: {len(body)}'.encode() in wire, (
            f'Expected content-length: {len(body)}; got {wire!r}'
        )

    async def test_date_header_injected(self):
        """Every response must include a Date header (RFC 7231 §7.1.1.2)."""
        handler, sw = self._make_handler()
        send = handler.make_sender()

        await send({'type': 'http.response.start', 'status': 200, 'headers': []})
        await send({'type': 'http.response.body', 'body': b'hi', 'more_body': False})

        wire = bytes(sw.written).lower()
        assert b'date:' in wire, (
            f'Expected auto-injected Date header; got {wire!r}'
        )

    async def test_app_supplied_content_length_not_duplicated(self):
        """If the app already provides Content-Length, it must not appear twice."""
        handler, sw = self._make_handler()
        send = handler.make_sender()
        body = b'hello'

        await send({
            'type': 'http.response.start',
            'status': 200,
            'headers': [(b'content-length', str(len(body)).encode())],
        })
        await send({'type': 'http.response.body', 'body': body, 'more_body': False})

        wire = bytes(sw.written).lower()
        assert wire.count(b'content-length:') == 1, (
            f'Content-Length must appear exactly once; got {wire!r}'
        )

    @pytest.mark.asyncio
    async def test_bytes_path_content_length_not_duplicated(self):
        """Bytes high-level call: lowercase content-length in headers must not be doubled."""
        handler, sw = self._make_handler()
        send = handler.make_sender()
        body = b'hello'
        await send(body, HTTPStatus.OK, [(b'content-length', str(len(body)).encode())])
        wire = bytes(sw.written).lower()
        assert wire.count(b'content-length:') == 1, (
            f'content-length must appear exactly once; got {wire!r}'
        )

    @pytest.mark.asyncio
    async def test_bytes_path_uppercase_content_length_not_duplicated(self):
        """Bytes high-level call: mixed-case Content-Length must also not be doubled."""
        handler, sw = self._make_handler()
        send = handler.make_sender()
        body = b'hello'
        await send(body, HTTPStatus.OK, [(b'Content-Length', str(len(body)).encode())])
        wire = bytes(sw.written).lower()
        assert wire.count(b'content-length:') == 1, (
            f'content-length must appear exactly once; got {wire!r}'
        )


# ---------------------------------------------------------------------------
# P3 — Streaming request body (more_body=True)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestStreamingRequestBody:
    """HTTP1Recipient must yield one http.request event per chunk for
    Transfer-Encoding: chunked requests, each with more_body=True until the
    final chunk (ASGI spec §2.3, HTTP/1.1 RFC 7230 §4.1).

    Currently the recipient reads the *entire* body in one call and returns
    a single event with more_body=False.  Streaming allows the app to process
    large uploads incrementally without buffering the full body.
    """

    def _make_recipient(self, chunked_wire: bytes):
        from blackbull.server.recipient import HTTP1Recipient, AsyncioReader
        from blackbull.server.headers import Headers
        scope = {
            'headers': Headers([(b'transfer-encoding', b'chunked')]),
        }
        reader = AsyncioReader(_FakeReader(chunked_wire))
        return HTTP1Recipient(reader, scope)

    async def test_first_chunk_has_more_body_true(self):
        """The first chunk event of a multi-chunk body must have more_body=True."""
        # chunk 1: "hello" (5 bytes), chunk 2: "world" (5 bytes), terminator
        wire = b'5\r\nhello\r\n5\r\nworld\r\n0\r\n\r\n'
        recipient = self._make_recipient(wire)

        event = await recipient()
        assert event['type'] == 'http.request'
        assert event.get('more_body') is True, (
            f'First chunk must have more_body=True; got {event!r}'
        )
        assert event['body'] == b'hello', (
            f'Expected first chunk body=b"hello"; got {event["body"]!r}'
        )

    async def test_last_chunk_has_more_body_false(self):
        """The event for the final (zero-length terminator) chunk must have
        more_body=False to signal end of stream."""
        wire = b'5\r\nhello\r\n0\r\n\r\n'
        recipient = self._make_recipient(wire)

        events = []
        while True:
            event = await recipient()
            events.append(event)
            if not event.get('more_body', False):
                break

        assert events[-1].get('more_body') is False, (
            f'Final event must have more_body=False; got {events[-1]!r}'
        )

    async def test_chunks_are_not_concatenated(self):
        """Each chunk must be its own http.request event, not merged into one."""
        wire = b'3\r\nfoo\r\n3\r\nbar\r\n0\r\n\r\n'
        recipient = self._make_recipient(wire)

        events = []
        while True:
            event = await recipient()
            events.append(event)
            if not event.get('more_body', False):
                break

        bodies = [e['body'] for e in events if e.get('body')]
        assert len(bodies) >= 2, (
            f'Expected at least 2 separate chunk events; got bodies={bodies}'
        )
        assert b'foo' in bodies and b'bar' in bodies, (
            f'Expected both chunks as separate events; got {bodies}'
        )


# ---------------------------------------------------------------------------
# P3 — ASGI http.response.trailers
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestHTTPResponseTrailers:
    """HTTP1Sender must handle the http.response.trailers ASGI send event.

    Trailers are headers appended after a chunked response body (RFC 7230
    §4.1.2).  The ASGI spec defines the event:
        {'type': 'http.response.trailers',
         'headers': [(b'x-checksum', b'abc')], 'more_trailers': False}
    The response must use Transfer-Encoding: chunked to accommodate trailers.
    """

    def _make_handler(self):
        sw = _make_stream_writer_mock()
        handler = object.__new__(HTTP11Handler)
        handler.app = None
        handler.reader = None
        handler.writer = sw
        handler.request = b''
        return handler, sw

    async def test_trailers_event_does_not_raise(self):
        """HTTP1Sender must accept http.response.trailers without raising."""
        handler = self._make_handler()[0]
        send = handler.make_sender()

        await send({'type': 'http.response.start', 'status': 200,
                    'headers': [(b'transfer-encoding', b'chunked')]})
        await send({'type': 'http.response.body', 'body': b'hello', 'more_body': True})
        # Must not raise:
        await send({'type': 'http.response.trailers',
                    'headers': [(b'x-checksum', b'abc123')], 'more_trailers': False})

    async def test_trailer_header_appears_in_wire_output(self):
        """Trailer headers must be written to the socket after the body."""
        handler, sw = self._make_handler()
        send = handler.make_sender()

        await send({'type': 'http.response.start', 'status': 200,
                    'headers': [(b'transfer-encoding', b'chunked')]})
        await send({'type': 'http.response.body', 'body': b'hello', 'more_body': True})
        await send({'type': 'http.response.trailers',
                    'headers': [(b'x-checksum', b'abc123')], 'more_trailers': False})

        wire = bytes(sw.written).lower()
        assert b'x-checksum: abc123' in wire, (
            f'Trailer header must appear in wire output; got {wire!r}'
        )

    async def test_trailer_comes_after_body(self):
        """The trailer header must appear after the body bytes in the wire output."""
        handler, sw = self._make_handler()
        send = handler.make_sender()
        body = b'response-body'

        await send({'type': 'http.response.start', 'status': 200,
                    'headers': [(b'transfer-encoding', b'chunked')]})
        await send({'type': 'http.response.body', 'body': body, 'more_body': True})
        await send({'type': 'http.response.trailers',
                    'headers': [(b'x-trailer', b'value')], 'more_trailers': False})

        wire = bytes(sw.written)
        body_pos = wire.find(body)
        trailer_pos = wire.lower().find(b'x-trailer')
        assert body_pos != -1, f'Body not found in wire: {wire!r}'
        assert trailer_pos != -1, f'Trailer not found in wire: {wire!r}'
        assert trailer_pos > body_pos, (
            f'Trailer (pos {trailer_pos}) must come after body (pos {body_pos})'
        )


# ---------------------------------------------------------------------------
# P3 — Access logging
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestAccessLogging:
    """HTTP11Handler must emit one access log entry per completed request.

    The entry must include the HTTP method, request path, and response status
    code — the minimum fields needed for traffic analysis and debugging.
    Expected logger name: 'blackbull.access' (or 'blackbull.server.server').
    """

    async def test_access_log_emitted_per_request(self, caplog):
        import logging
        raw = _http_request(method='GET', path='/status')
        writer = _FakeWriter()

        async def app(scope, receive, send):
            await send({'type': 'http.response.start', 'status': 200, 'headers': []})
            await send({'type': 'http.response.body', 'body': b'', 'more_body': False})

        handler = HTTP11Handler(app, _FakeReader(raw), writer, b'GET /status HTTP/1.1\r\n')
        handler.request = raw

        with caplog.at_level(logging.INFO):
            await handler.run()

        assert caplog.records, 'Expected at least one log record per request'
        messages = ' '.join(r.message for r in caplog.records)
        assert 'GET' in messages and '/status' in messages, (
            f'Access log must include method and path; records: '
            f'{[r.message for r in caplog.records]}'
        )

    async def test_access_log_includes_status_code(self, caplog):
        import logging
        raw = _http_request(method='GET', path='/')
        writer = _FakeWriter()

        async def app(scope, receive, send):
            await send({'type': 'http.response.start', 'status': 404, 'headers': []})
            await send({'type': 'http.response.body', 'body': b'', 'more_body': False})

        handler = HTTP11Handler(app, _FakeReader(raw), writer, b'GET / HTTP/1.1\r\n')
        handler.request = raw

        with caplog.at_level(logging.INFO):
            await handler.run()

        messages = ' '.join(r.message for r in caplog.records)
        assert '404' in messages, (
            f'Access log must include response status; records: '
            f'{[r.message for r in caplog.records]}'
        )

    async def test_websocket_access_log_emitted(self, caplog):
        """HTTP11Handler must emit one access log entry per completed WebSocket session."""
        import logging
        raw = _ws_request(path='/chat')
        writer = _FakeWriter()

        app_called = []

        async def app(scope, receive, send):
            app_called.append(scope.get('type'))
            # Accept and immediately close
            await send({'type': 'websocket.accept'})

        with patch.object(WebSocketHandler, 'run', new=AsyncMock()) as mock_run:
            handler = HTTP11Handler(app, _FakeReader(raw), writer, raw[:1])
            handler.request = raw
            with caplog.at_level(logging.INFO):
                await handler.run()

        access_records = [r for r in caplog.records
                          if r.name == 'blackbull.access']
        assert access_records, 'Expected one access log entry for WebSocket session'
        msg = access_records[0].message
        assert '/chat' in msg, f'Log must include request path; got: {msg!r}'
        assert '101' in msg, f'Log must include upgrade status 101; got: {msg!r}'

    async def test_websocket_access_log_includes_close_code(self, caplog):
        """The WebSocket access log entry must include the RFC 6455 close code."""
        import logging
        from blackbull.server.server import AccessLogRecord, _make_capturing_receive

        record = AccessLogRecord(
            client_ip='127.0.0.1', method='GET', path='/ws', http_version='1.1',
            status=101,
        )

        events = [
            {'type': 'websocket.connect'},
            {'type': 'websocket.disconnect', 'code': 1001},
        ]
        idx = 0

        async def fake_receive():
            nonlocal idx
            ev = events[idx]
            idx += 1
            return ev

        wrapped = _make_capturing_receive(fake_receive, record)
        await wrapped()  # websocket.connect — close_code stays None
        assert record.close_code is None
        await wrapped()  # websocket.disconnect with code 1001
        assert record.close_code == 1001

    async def test_http2_access_log_emitted_per_stream(self, caplog):
        """HTTP2Handler must emit one access log entry per completed HTTP/2 stream."""
        import logging
        from blackbull.server.server import AccessLogRecord, _run_with_log

        async def app(scope, receive, send):
            pass

        async def noop_send(event, *a, **kw):
            pass

        record = AccessLogRecord(
            client_ip='10.0.0.1', method='GET', path='/h2', http_version='2',
        )
        scope = {
            'type': 'http', 'method': 'GET', 'path': '/h2',
            'http_version': '2', 'client': ['10.0.0.1', 0],
        }

        async def fake_receive():
            return {'type': 'http.request', 'body': b'', 'more_body': False}

        with caplog.at_level(logging.INFO):
            await _run_with_log(app(scope, fake_receive, noop_send), record)

        access_records = [r for r in caplog.records
                          if r.name == 'blackbull.access']
        assert access_records, 'Expected one access log entry for HTTP/2 stream'
        msg = access_records[0].message
        assert '/h2' in msg, f'Log must include request path; got: {msg!r}'


# ---------------------------------------------------------------------------
# Recipient coverage tests
# ---------------------------------------------------------------------------

class TestAsyncioReader:
    def test_invalid_stream_raises_typeerror(self):
        from blackbull.server.recipient import AsyncioReader
        with pytest.raises(TypeError, match='read()'):
            AsyncioReader(object())  # plain object has no read/readuntil


class TestHTTP1Recipient:
    def _make_reader(self, data: bytes):
        """Return an AbstractReader that yields *data* from read()."""
        from blackbull.server.recipient import AbstractReader

        class _BufReader(AbstractReader):
            def __init__(self, buf):
                self._buf = buf

            async def read(self, n):
                chunk, self._buf = self._buf[:n], self._buf[n:]
                return chunk

            async def readuntil(self, sep):
                idx = self._buf.find(sep)
                if idx == -1:
                    chunk, self._buf = self._buf, b''
                    return chunk
                chunk, self._buf = self._buf[:idx + len(sep)], self._buf[idx + len(sep):]
                return chunk

            async def readexactly(self, n):
                chunk, self._buf = self._buf[:n], self._buf[n:]
                return chunk

        return _BufReader(data)

    @pytest.mark.asyncio
    async def test_unsupported_transfer_encoding_raises(self):
        from blackbull.server.recipient import HTTP1Recipient

        scope = {
            'headers': [(b'transfer-encoding', b'gzip')],
        }
        reader = self._make_reader(b'')
        with pytest.raises(NotImplementedError, match='not supported'):
            HTTP1Recipient(reader, scope)

    @pytest.mark.asyncio
    async def test_second_call_returns_disconnect(self):
        from blackbull.server.recipient import HTTP1Recipient

        scope = {
            'headers': [(b'content-length', b'5')],
        }
        reader = self._make_reader(b'hello')
        r = HTTP1Recipient(reader, scope)
        first = await r()
        assert first['type'] == 'http.request'
        second = await r()
        assert second == {'type': 'http.disconnect'}


class TestHTTP2Recipient:
    @pytest.mark.asyncio
    async def test_with_initial_frame_enqueues_event(self):
        from blackbull.server.recipient import HTTP2Recipient
        from blackbull.protocol.frame import Data

        frame = MagicMock(spec=Data)
        frame.payload = b'body'
        frame.end_stream = True

        r = HTTP2Recipient(frame)
        event = await r()
        assert event == {'type': 'http.request', 'body': b'body', 'more_body': False}


class TestWebSocketRecipientUnsupportedOpcode:
    @pytest.mark.asyncio
    async def test_unknown_opcode_logs_warning(self, caplog):
        import logging
        from blackbull.server.recipient import WebSocketRecipient, AbstractReader
        from blackbull.server.sender import WSOpcode

        class _FakeReader(AbstractReader):
            """Returns one frame with a reserved/unknown opcode (0x03)."""
            _called = False

            async def read(self, n):
                return b'\x00' * n

            async def readuntil(self, sep):
                return b''

            async def readexactly(self, n):
                # Frame header: FIN=1, opcode=0x03, no mask, payload_len=2
                # Then 2 payload bytes.
                if not self._called:
                    self._called = True
                    # byte0: 0b10000011 (FIN=1, RSV=0, opcode=3)
                    # byte1: 0b00000010 (MASK=0, len=2)
                    header = bytes([0x83, 0x02])
                    payload = b'\x00\x00'
                    data = (header + payload)
                    return data[:n]
                return b'\x00' * n

        reader = _FakeReader()
        writer = MagicMock()
        writer.write = MagicMock()
        writer.drain = AsyncMock()

        r = WebSocketRecipient(reader, writer, require_masked=False)
        r._connect_sent = True  # skip the connect event

        # Patch _read_frame_header and _read_payload to feed a fake unknown-opcode frame
        from blackbull.server.sender import WebSocketSender, WSFrameHeader

        fake_header = WSFrameHeader(fin=True, rsv1=False, rsv2=False, rsv3=False, opcode=0x03, masked=False, length=2)

        eof = asyncio.IncompleteReadError(b'', 2)
        with patch.object(WebSocketSender, '_read_frame_header',
                          AsyncMock(side_effect=[fake_header, eof])):
            with patch.object(WebSocketSender, '_read_payload', AsyncMock(return_value=b'\x00\x00')):
                with caplog.at_level(logging.WARNING, logger='blackbull.server.recipient'):
                    event = await r()

        assert event['type'] == 'websocket.receive'
        assert any('unsupported opcode' in r.message for r in caplog.records)
