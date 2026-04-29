"""Tests for the request_completed Level B event.

Affected-tests baseline (must not regress after implementation):
- test_server_dispatch.py::TestAccessLogging::test_access_log_emitted_per_request
- test_server_dispatch.py::TestAccessLogging::test_access_log_includes_status_code
- test_server_dispatch.py::TestAccessLogging::test_websocket_access_log_emitted
- test_server_dispatch.py::TestAccessLogging::test_websocket_access_log_includes_close_code
- test_server_dispatch.py::TestAccessLogging::test_http2_access_log_emitted_per_stream
"""
import asyncio
import pytest
from blackbull import BlackBull, StreamingResponse
from blackbull.event import Event
from blackbull.server.server import HTTP11Handler, AccessLogRecord, _run_with_log


# ---------------------------------------------------------------------------
# Shared test infrastructure
# ---------------------------------------------------------------------------

class _FakeTransport:
    def __init__(self, peername=None, sockname=None, ssl_object=None):
        self._extras = {
            'peername': peername,
            'sockname': sockname,
            'ssl_object': ssl_object,
        }

    def get_extra_info(self, key, default=None):
        return self._extras.get(key, default)


class _FakeWriter:
    def __init__(self, peername=('127.0.0.1', 54321), sockname=('0.0.0.0', 8000)):
        self.written = bytearray()
        self.transport = _FakeTransport(peername=peername, sockname=sockname)

    def write(self, data: bytes):
        self.written += data

    async def drain(self):
        pass

    def close(self):
        pass

    async def wait_closed(self):
        pass


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


def _raw_request(method: str = 'GET', path: str = '/',
                 version: str = 'HTTP/1.1', host: str = 'localhost:8000') -> bytes:
    lines = [f'{method} {path} {version}', f'Host: {host}', '', '']
    return '\r\n'.join(lines).encode()


async def _run_request(app, raw: bytes) -> None:
    """Run a single HTTP/1.1 request through HTTP11Handler and return."""
    writer = _FakeWriter()
    handler = HTTP11Handler(app, _FakeReader(b''), writer, raw[:1])
    handler.request = raw
    await handler.run()


# ---------------------------------------------------------------------------
# Field consistency: scope vs AccessLogRecord fields
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_scope_and_record_fields_agree_on_method_path_and_version():
    """detail['scope'] and the flattened record fields must agree."""
    app = BlackBull()
    captured: list[Event] = []
    seen = asyncio.Event()

    @app.on('request_completed')
    async def observer(event: Event):
        captured.append(event)
        seen.set()

    @app.route(path='/x')
    async def handler(scope, receive, send):
        await send({'type': 'http.response.start', 'status': 200, 'headers': []})
        await send({'type': 'http.response.body', 'body': b'ok', 'more_body': False})

    await _run_request(app, _raw_request(method='GET', path='/x'))
    await asyncio.wait_for(seen.wait(), timeout=2.0)

    d = captured[0].detail
    assert d['method'] == d['scope']['method']
    assert d['path'] == d['scope']['path']
    if 'http_version' in d['scope']:
        assert d['http_version'] == d['scope']['http_version']


@pytest.mark.asyncio
async def test_status_and_response_bytes_reflect_actual_response():
    """status and response_bytes in detail match what the server sent."""
    body = b'hello world'
    app = BlackBull()
    captured: list[Event] = []
    seen = asyncio.Event()

    @app.on('request_completed')
    async def observer(event: Event):
        captured.append(event)
        seen.set()

    @app.route(path='/data')
    async def handler(scope, receive, send):
        await send({'type': 'http.response.start', 'status': 200, 'headers': []})
        await send({'type': 'http.response.body', 'body': body, 'more_body': False})

    await _run_request(app, _raw_request(path='/data'))
    await asyncio.wait_for(seen.wait(), timeout=2.0)

    d = captured[0].detail
    assert d['status'] == 200
    assert d['response_bytes'] == len(body)


# ---------------------------------------------------------------------------
# Firing conditions
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_event_fires_once_per_request():
    """Exactly one event fires per HTTP request."""
    target_count = 3
    app = BlackBull()
    captured: list[Event] = []
    done = asyncio.Event()

    @app.on('request_completed')
    async def observer(event: Event):
        captured.append(event)
        if len(captured) >= target_count:
            done.set()

    @app.route(path='/r')
    async def handler(scope, receive, send):
        await send({'type': 'http.response.start', 'status': 200, 'headers': []})
        await send({'type': 'http.response.body', 'body': b'r', 'more_body': False})

    for _ in range(target_count):
        await _run_request(app, _raw_request(path='/r'))

    await asyncio.wait_for(done.wait(), timeout=2.0)
    assert len(captured) == target_count


@pytest.mark.asyncio
async def test_event_fires_for_handler_exception():
    """An unhandled exception in the handler still produces the event with status 500."""
    app = BlackBull()
    captured: list[Event] = []
    seen = asyncio.Event()

    @app.on('request_completed')
    async def observer(event: Event):
        captured.append(event)
        seen.set()

    @app.route(path='/boom')
    async def handler(scope, receive, send):
        raise RuntimeError('boom')

    await _run_request(app, _raw_request(path='/boom'))
    await asyncio.wait_for(seen.wait(), timeout=2.0)

    d = captured[0].detail
    assert d['status'] == 500
    assert d['path'] == '/boom'


@pytest.mark.asyncio
async def test_event_fires_for_404():
    """A request to an unregistered path produces the event with status 404."""
    app = BlackBull()
    captured: list[Event] = []
    seen = asyncio.Event()

    @app.on('request_completed')
    async def observer(event: Event):
        captured.append(event)
        seen.set()

    await _run_request(app, _raw_request(path='/does-not-exist'))
    await asyncio.wait_for(seen.wait(), timeout=2.0)

    d = captured[0].detail
    assert d['status'] == 404
    assert d['path'] == '/does-not-exist'


@pytest.mark.asyncio
async def test_duration_is_nonnegative_float():
    """duration_ms in detail is a non-negative float."""
    app = BlackBull()
    captured: list[Event] = []
    seen = asyncio.Event()

    @app.on('request_completed')
    async def observer(event: Event):
        captured.append(event)
        seen.set()

    @app.route(path='/timed')
    async def handler(scope, receive, send):
        await send({'type': 'http.response.start', 'status': 200, 'headers': []})
        await send({'type': 'http.response.body', 'body': b'ok', 'more_body': False})

    await _run_request(app, _raw_request(path='/timed'))
    await asyncio.wait_for(seen.wait(), timeout=2.0)

    d = captured[0].detail
    assert isinstance(d['duration_ms'], float)
    assert d['duration_ms'] >= 0.0


# ---------------------------------------------------------------------------
# HTTP/2
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_event_fires_per_stream_on_http2():
    """On HTTP/2, the event fires once per stream via _run_with_log."""
    from blackbull.event import EventDispatcher

    dispatcher = EventDispatcher()
    captured: list[Event] = []
    seen = asyncio.Event()

    async def observer(event: Event) -> None:
        captured.append(event)
        seen.set()

    dispatcher.on('request_completed', observer)

    async def app(scope, receive, send):
        pass

    async def noop_send(event, *args, **kwargs):
        pass

    record = AccessLogRecord(
        client_ip='10.0.0.1', method='GET', path='/h2', http_version='2',
        status=200, response_bytes=42,
    )
    scope = {
        'type': 'http', 'method': 'GET', 'path': '/h2',
        'http_version': '2', 'client': ['10.0.0.1', 0],
    }

    async def fake_receive():
        return {'type': 'http.request', 'body': b'', 'more_body': False}

    await _run_with_log(app(scope, fake_receive, noop_send), record,
                        dispatcher=dispatcher, scope=scope)
    await asyncio.wait_for(seen.wait(), timeout=2.0)

    d = captured[0].detail
    assert d['path'] == '/h2'
    assert d['method'] == 'GET'


# ---------------------------------------------------------------------------
# Streaming responses
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_event_fires_after_streaming_response_completes():
    """For streaming responses, the event fires after more_body=False."""
    chunks = [b'chunk0', b'chunk1', b'chunk2']
    total_bytes = sum(len(c) for c in chunks)

    app = BlackBull()
    captured: list[Event] = []
    seen = asyncio.Event()

    @app.on('request_completed')
    async def observer(event: Event):
        captured.append(event)
        seen.set()

    async def _body_gen():
        for chunk in chunks:
            yield chunk

    @app.route(path='/stream')
    async def handler(scope, receive, send):
        await StreamingResponse(_body_gen())(scope, receive, send)

    await _run_request(app, _raw_request(path='/stream'))
    await asyncio.wait_for(seen.wait(), timeout=2.0)

    d = captured[0].detail
    assert d['status'] == 200
    assert d['response_bytes'] == total_bytes
