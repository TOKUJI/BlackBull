"""Tests for HTTP1Actor and RequestActor (Phase 6 Step 3)."""
import asyncio
import pytest
from unittest.mock import ANY, AsyncMock, patch

from blackbull.event_aggregator import EventAggregator
from blackbull.server.http1_actor import HTTP1Actor, RequestActor
from blackbull.server.recipient import AbstractReader, IncompleteReadError
from blackbull.server.sender import AbstractWriter
from blackbull.server.websocket_actor import WebSocketActor


# ---------------------------------------------------------------------------
# In-process reader/writer fakes (no live sockets)
# ---------------------------------------------------------------------------

class _FakeTransport:
    def __init__(self, peername=('127.0.0.1', 54321), sockname=('0.0.0.0', 8000)):
        self._extras = {
            'peername': peername,
            'sockname': sockname,
            'ssl_object': None,
        }

    def get_extra_info(self, key, default=None):
        return self._extras.get(key, default)


class _FakeWriter(AbstractWriter):
    def __init__(self):
        self.written = bytearray()

    async def write(self, data: bytes) -> None:
        self.written += data


class _FakeReader(AbstractReader):
    """AbstractReader backed by a byte buffer; raises IncompleteReadError on
    empty reads (signals EOF to the handler keep-alive loop)."""

    def __init__(self, data: bytes):
        self._buf = bytearray(data)

    async def read(self, n: int) -> bytes:
        chunk = bytes(self._buf[:n])
        del self._buf[:n]
        return chunk

    async def readuntil(self, sep: bytes) -> bytes:
        idx = self._buf.find(sep)
        if idx == -1:
            # EOF sentinel: return just the separator (keep-alive break signal)
            chunk, self._buf = bytes(self._buf), bytearray()
            return chunk + sep
        chunk = bytes(self._buf[:idx + len(sep)])
        del self._buf[:idx + len(sep)]
        return chunk

    async def readexactly(self, n: int) -> bytes:
        if len(self._buf) < n:
            raise IncompleteReadError(bytes(self._buf))
        chunk = bytes(self._buf[:n])
        del self._buf[:n]
        return chunk


def _http_get(path: str = '/', host: str = 'localhost:8000') -> bytes:
    return f'GET {path} HTTP/1.1\r\nHost: {host}\r\n\r\n'.encode()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_writer():
    return _FakeWriter()


@pytest.fixture
def mock_reader():
    """Single GET request, then EOF (keep-alive terminates cleanly)."""
    return _FakeReader(_http_get())


@pytest.fixture
def mock_keep_alive_reader():
    """Two GET requests back-to-back, then EOF."""
    return _FakeReader(_http_get() + _http_get())


@pytest.fixture
def mock_app():
    return AsyncMock()


# ---------------------------------------------------------------------------
# Helper: aggregator mock that calls through on_before_handler
# ---------------------------------------------------------------------------

def _call_through_aggregator():
    """AsyncMock(spec=EventAggregator) whose on_before_handler calls call_next."""
    aggregator = AsyncMock(spec=EventAggregator)

    async def _before(scope, receive, send, *, call_next):
        await call_next(scope, receive, send)

    aggregator.on_before_handler = AsyncMock(side_effect=_before)
    return aggregator


# ---------------------------------------------------------------------------
# Test 1: single request → all four lifecycle events fire in order
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_request_lifecycle_events(mock_reader, mock_writer, mock_app) -> None:
    aggregator = AsyncMock(spec=EventAggregator)
    actor = HTTP1Actor(mock_reader, mock_writer, mock_app, aggregator)
    await actor.run()

    aggregator.on_request_received.assert_called_once()
    aggregator.on_before_handler.assert_called_once()
    aggregator.on_after_handler.assert_called_once_with(ANY, exception=None)
    aggregator.on_request_completed.assert_called_once()
    aggregator.on_error.assert_not_called()


# ---------------------------------------------------------------------------
# Test 2: app exception → error fires, after_handler carries exception,
#         request_completed does NOT fire
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_app_exception_fires_error(mock_reader, mock_writer) -> None:
    boom = RuntimeError("app error")

    async def bad_app(scope, receive, send):
        raise boom

    # Use a call-through aggregator so bad_app is actually invoked via call_next
    aggregator = _call_through_aggregator()
    actor = HTTP1Actor(mock_reader, mock_writer, bad_app, aggregator)
    await actor.run()  # isolate strategy: exception is swallowed after re-emitting

    aggregator.on_error.assert_called_once_with(ANY, boom)
    call_args = aggregator.on_after_handler.call_args
    assert call_args.kwargs["exception"] is boom
    aggregator.on_request_completed.assert_not_called()


# ---------------------------------------------------------------------------
# Test 3: keep-alive — two sequential requests on the same connection
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_keep_alive_two_requests(mock_keep_alive_reader, mock_writer, mock_app) -> None:
    aggregator = AsyncMock(spec=EventAggregator)
    actor = HTTP1Actor(mock_keep_alive_reader, mock_writer, mock_app, aggregator)
    await actor.run()
    assert aggregator.on_request_completed.call_count == 2


# ---------------------------------------------------------------------------
# Test 4: request_disconnected fires when aggregator reports it
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Test 5: _parse() — Connection header must NOT corrupt scope['scheme']
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_parse_connection_header_does_not_corrupt_scheme(mock_writer) -> None:
    """Regression: Connection: keep-alive must not set scope['scheme'] = 'keep-alive'."""
    request = b'GET / HTTP/1.1\r\nHost: localhost:8000\r\nConnection: keep-alive\r\n\r\n'
    reader = _FakeReader(request)

    received_scope = {}

    async def capture_app(scope, receive, send):
        received_scope.update(scope)
        await send({'type': 'http.response.start', 'status': 200, 'headers': []})
        await send({'type': 'http.response.body', 'body': b''})

    aggregator = _call_through_aggregator()
    actor = HTTP1Actor(reader, mock_writer, capture_app, aggregator)
    await actor.run()

    assert received_scope['scheme'] == 'http'


# ---------------------------------------------------------------------------
# Test 6: _parse() — Host header without port must not crash
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_parse_host_without_port(mock_writer) -> None:
    """Regression: Host: localhost (no port) must not raise ValueError."""
    request = b'GET / HTTP/1.1\r\nHost: localhost\r\n\r\n'
    reader = _FakeReader(request)

    received_scope = {}

    async def capture_app(scope, receive, send):
        received_scope.update(scope)
        await send({'type': 'http.response.start', 'status': 200, 'headers': []})
        await send({'type': 'http.response.body', 'body': b''})

    aggregator = _call_through_aggregator()
    actor = HTTP1Actor(reader, mock_writer, capture_app, aggregator)
    await actor.run()  # must not raise

    assert received_scope['server'] == ['localhost', 80]


# ---------------------------------------------------------------------------
# Test 7: request_disconnected fires when aggregator reports it
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_request_disconnected_on_eof(mock_reader, mock_writer) -> None:
    """Aggregator.on_request_disconnected fires when the app polls receive() and
    gets back http.disconnect (HTTP1Recipient returns it on the second call once
    the body has been consumed)."""
    disconnected_called = False

    async def app_that_detects_disconnect(scope, receive, send):
        await receive()  # consumes the body (http.request)
        await receive()  # HTTP1Recipient returns http.disconnect on second call

    aggregator = _call_through_aggregator()

    async def _on_disconnect(*args, **kwargs):
        nonlocal disconnected_called
        disconnected_called = True

    aggregator.on_request_disconnected = AsyncMock(side_effect=_on_disconnect)

    actor = HTTP1Actor(mock_reader, mock_writer, app_that_detects_disconnect, aggregator)
    await actor.run()
    assert disconnected_called


# ---------------------------------------------------------------------------
# Helpers shared by the tests below
# ---------------------------------------------------------------------------

def _http_request(method='GET', path='/', version='HTTP/1.1',
                  headers: dict | None = None) -> bytes:
    if headers is None:
        headers = {'Host': 'localhost:8000'}
    lines = [f'{method} {path} {version}']
    for k, v in headers.items():
        lines.append(f'{k}: {v}')
    lines += ['', '']
    return '\r\n'.join(lines).encode()


def _ws_request(path='/ws', host='localhost:8000') -> bytes:
    return _http_request(
        method='GET', path=path,
        headers={
            'Host': host,
            'Upgrade': 'websocket',
            'Connection': 'Upgrade',
            'Sec-WebSocket-Key': 'dGhlIHNhbXBsZSBub25jZQ==',
            'Sec-WebSocket-Version': '13',
        }
    )


async def _noop_app(scope, receive, send):
    pass


def _make_actor(raw: bytes, app=None, *,
                peername=('127.0.0.1', 54321),
                sockname=('0.0.0.0', 8000),
                ssl=False):
    if app is None:
        app = _noop_app
    writer = _FakeWriter()
    first_line, rest = raw.split(b'\r\n', 1)
    reader = _FakeReader(rest)
    actor = HTTP1Actor(
        reader, writer, app, None,
        request=first_line + b'\r\n',
        peername=peername, sockname=sockname, ssl=ssl,
    )
    return actor, writer


# ---------------------------------------------------------------------------
# scope['client'], scope['server'], scope['scheme'] population
# ---------------------------------------------------------------------------

class TestScopePopulation:
    """HTTP1Actor must fill client, server, and scheme from peername/sockname/ssl."""

    @pytest.mark.asyncio
    async def test_client_is_set_from_peername(self):
        raw = _http_request()
        captured = {}

        async def capture_app(scope, receive, send):
            captured.update(scope)

        actor, _writer = _make_actor(raw, capture_app, peername=('192.168.1.10', 54321))
        await actor.run()

        assert captured['client'] == ['192.168.1.10', 54321]

    @pytest.mark.asyncio
    async def test_server_falls_back_to_sockname_when_no_host_header(self):
        # A Host-less request is only legal on HTTP/1.0 (RFC 9112 §3.2 —
        # HTTP/1.1 without Host is now rejected 400), so the sockname
        # fallback is exercised through a 1.0 request.
        raw = _http_request(version='HTTP/1.0', headers={})
        captured = {}

        async def capture_app(scope, receive, send):
            captured.update(scope)

        actor, _writer = _make_actor(raw, capture_app, sockname=('0.0.0.0', 9000))
        await actor.run()

        assert captured['server'] is not None
        assert captured['server'][1] == 9000

    @pytest.mark.asyncio
    async def test_server_from_host_header_takes_priority_over_sockname(self):
        raw = _http_request(headers={'Host': 'example.com:8080'})
        captured = {}

        async def capture_app(scope, receive, send):
            captured.update(scope)

        actor, _writer = _make_actor(raw, capture_app, sockname=('0.0.0.0', 9999))
        await actor.run()

        assert captured['server'] == ['example.com', 8080]

    @pytest.mark.asyncio
    async def test_scheme_is_http_for_plain_connection(self):
        raw = _http_request()
        captured = {}

        async def capture_app(scope, receive, send):
            captured.update(scope)

        actor, _writer = _make_actor(raw, capture_app, ssl=False)
        await actor.run()

        assert captured['scheme'] == 'http'

    @pytest.mark.asyncio
    async def test_scheme_is_https_for_tls_connection(self):
        raw = _http_request()
        captured = {}

        async def capture_app(scope, receive, send):
            captured.update(scope)

        actor, _writer = _make_actor(raw, capture_app, ssl=True)
        await actor.run()

        assert captured['scheme'] == 'https'

    @pytest.mark.asyncio
    async def test_scheme_is_wss_for_tls_websocket(self):
        raw = _ws_request(path='/ws')
        first_line, rest = raw.split(b'\r\n', 1)
        actor = HTTP1Actor(
            _FakeReader(rest), _FakeWriter(), _noop_app, None,
            request=first_line + b'\r\n',
            peername=('127.0.0.1', 54321),
            sockname=('0.0.0.0', 8000),
            ssl=True,
        )
        with patch.object(WebSocketActor, 'run', new=AsyncMock()):
            await actor.run()

        test_scope = actor._parse(raw)
        actor._fill_connection_info(test_scope)
        assert test_scope.get('scheme') == 'wss'

    @pytest.mark.asyncio
    async def test_cleartext_advertises_pathsend_extension(self):
        """Sprint 31 — cleartext H1 scope advertises the
        ``http.response.pathsend`` ASGI extension so the static-file
        middleware can hand the file path to the sender (zero-copy
        via ``loop.sendfile``)."""
        raw = _http_request()
        captured = {}

        async def capture_app(scope, receive, send):
            captured.update(scope)

        actor, _writer = _make_actor(raw, capture_app, ssl=False)
        await actor.run()

        assert 'extensions' in captured
        assert 'http.response.pathsend' in captured['extensions']

    @pytest.mark.asyncio
    async def test_tls_does_not_advertise_pathsend_extension(self):
        """TLS transports can't sendfile (kernel doesn't see plaintext),
        so the extension MUST NOT be advertised — otherwise middleware
        would route requests through a path that immediately falls
        back, wasting one stat syscall per request."""
        raw = _http_request()
        captured = {}

        async def capture_app(scope, receive, send):
            captured.update(scope)

        actor, _writer = _make_actor(raw, capture_app, ssl=True)
        await actor.run()

        assert 'http.response.pathsend' not in captured.get('extensions', {})


# ---------------------------------------------------------------------------
# _fill_connection_info — direct unit tests
# ---------------------------------------------------------------------------

class TestFillConnectionInfo:
    """HTTP1Actor._fill_connection_info must populate scope from constructor args."""

    def _make_actor(self, peername=None, sockname=None, ssl=False):
        return HTTP1Actor(
            _FakeReader(b''), _FakeWriter(), _noop_app, None,
            peername=peername, sockname=sockname, ssl=ssl,
        )

    def test_peername_sets_client(self):
        actor = self._make_actor(peername=('10.0.0.1', 9999))
        scope = {'type': 'http', 'server': None}
        actor._fill_connection_info(scope)
        assert scope['client'] == ['10.0.0.1', 9999]

    def test_sockname_sets_server_when_no_host(self):
        actor = self._make_actor(sockname=('0.0.0.0', 7777))
        scope = {'type': 'http', 'server': None}
        actor._fill_connection_info(scope)
        assert scope['server'] == ['0.0.0.0', 7777]

    def test_host_header_not_overwritten_by_sockname(self):
        actor = self._make_actor(sockname=('0.0.0.0', 7777))
        scope = {'type': 'http', 'server': ['myhost', 80]}
        actor._fill_connection_info(scope)
        assert scope['server'] == ['myhost', 80]

    def test_ssl_true_sets_https(self):
        actor = self._make_actor(ssl=True)
        scope = {'type': 'http', 'scheme': 'http', 'server': None}
        actor._fill_connection_info(scope)
        assert scope['scheme'] == 'https'

    def test_ssl_false_leaves_scheme(self):
        actor = self._make_actor(ssl=False)
        scope = {'type': 'http', 'scheme': 'http', 'server': None}
        actor._fill_connection_info(scope)
        assert scope['scheme'] == 'http'

    def test_ssl_true_websocket_sets_wss(self):
        actor = self._make_actor(ssl=True)
        scope = {'type': 'websocket', 'scheme': 'ws', 'server': None}
        actor._fill_connection_info(scope)
        assert scope['scheme'] == 'wss'


# ---------------------------------------------------------------------------
# Access logging
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestAccessLogging:
    """HTTP1Actor must emit one access log entry per completed request."""

    async def test_access_log_emitted_per_request(self, caplog):
        import logging
        raw = _http_request(method='GET', path='/status')

        async def app(scope, receive, send):
            await send({'type': 'http.response.start', 'status': 200, 'headers': []})
            await send({'type': 'http.response.body', 'body': b'', 'more_body': False})

        actor, writer = _make_actor(raw, app)

        with caplog.at_level(logging.INFO):
            await actor.run()

        assert caplog.records, 'Expected at least one log record per request'
        messages = ' '.join(r.message for r in caplog.records)
        assert 'GET' in messages and '/status' in messages

    async def test_access_log_includes_status_code(self, caplog):
        import logging
        raw = _http_request(method='GET', path='/')

        async def app(scope, receive, send):
            await send({'type': 'http.response.start', 'status': 404, 'headers': []})
            await send({'type': 'http.response.body', 'body': b'', 'more_body': False})

        actor, writer = _make_actor(raw, app)

        with caplog.at_level(logging.INFO):
            await actor.run()

        messages = ' '.join(r.message for r in caplog.records)
        assert '404' in messages

    async def test_websocket_access_log_emitted(self, caplog):
        import logging
        raw = _ws_request(path='/chat')
        first_line, rest = raw.split(b'\r\n', 1)
        actor = HTTP1Actor(
            _FakeReader(rest), _FakeWriter(), _noop_app, None,
            request=first_line + b'\r\n',
            peername=('127.0.0.1', 54321),
            sockname=('0.0.0.0', 8000),
        )

        with patch.object(WebSocketActor, 'run', new=AsyncMock()):
            with caplog.at_level(logging.INFO):
                await actor.run()

        access_records = [r for r in caplog.records if r.name == 'blackbull.access']
        assert access_records, 'Expected one access log entry for WebSocket session'
        msg = access_records[0].message
        assert '/chat' in msg and '101' in msg

    async def test_websocket_access_log_includes_close_code(self):
        from blackbull.server.websocket_actor import WebSocketActor
        from blackbull.server.constants import WSCloseCode
        from unittest.mock import AsyncMock, MagicMock

        events = [
            {'type': 'websocket.connect'},
            {'type': 'websocket.disconnect', 'code': 1001},
        ]
        idx = 0

        async def fake_receive():
            nonlocal idx
            ev = events[idx % len(events)]
            idx += 1
            return ev

        async def fake_app(scope, receive, send):
            await receive()  # connect
            await receive()  # disconnect

        writer = MagicMock()
        writer.write = AsyncMock()
        writer.close = AsyncMock()
        aggregator = MagicMock()
        aggregator.on_error = AsyncMock()
        aggregator.on_websocket_disconnected = AsyncMock()
        aggregator.on_websocket_message = AsyncMock()
        aggregator.on_websocket_connected = AsyncMock()

        from blackbull.server.recipient import RecipientFactory
        from blackbull.server.sender import SenderFactory

        # Minimal scope with _ws_send_101 so accept can proceed
        scope = {
            'type': 'websocket', 'path': '/ws', 'client': ['127.0.0.1', 0],
        }

        actor = WebSocketActor.__new__(WebSocketActor)
        actor._scope = scope
        actor._app = fake_app
        actor._aggregator = aggregator
        actor._writer = writer
        actor._disconnect_code = WSCloseCode.ABNORMAL
        actor._ws_receive = fake_receive
        actor._ws_send = AsyncMock()

        # Simulate disconnect code capture via _receive
        event = await actor._receive()  # connect
        assert actor._disconnect_code == WSCloseCode.ABNORMAL
        event = await actor._receive()  # disconnect with code 1001
        assert actor._disconnect_code == 1001

    async def test_http2_access_log_emitted_per_stream(self, caplog):
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

        access_records = [r for r in caplog.records if r.name == 'blackbull.access']
        assert access_records, 'Expected one access log entry for HTTP/2 stream'
        assert '/h2' in access_records[0].message
