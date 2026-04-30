"""Tests for the websocket_connected Level B event.

Affected-tests baseline (must not regress after implementation):
- test_websocket_message_event.py::* (websocket_message must still fire)
- test_request_disconnected_event.py::* (request_disconnected unaffected)
- test_request_completed_event.py::* (request_completed unaffected)
"""
import asyncio
import pytest
from blackbull import BlackBull
from blackbull.event import Event
from blackbull.utils import Scheme
from blackbull.server.server import HTTP11Handler, WebSocketHandler
from blackbull.server.headers import Headers


# ---------------------------------------------------------------------------
# Shared test infrastructure
# ---------------------------------------------------------------------------

class _FakeReader:
    """Replay a pre-built byte string through the asyncio StreamReader API."""

    def __init__(self, data: bytes):
        self._buf = bytearray(data)

    async def readexactly(self, n: int) -> bytes:
        if len(self._buf) < n:
            raise asyncio.IncompleteReadError(bytes(self._buf), n)
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

    async def read(self, n: int = -1) -> bytes:
        if n < 0:
            chunk, self._buf = bytes(self._buf), bytearray()
            return chunk
        chunk = bytes(self._buf[:n])
        del self._buf[:n]
        return chunk

    async def readline(self) -> bytes:
        return await self.readuntil(b'\n')


class _FakeWriter:
    def __init__(self):
        self.written = bytearray()

    def write(self, data: bytes) -> None:
        self.written += data

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        pass

    async def wait_closed(self) -> None:
        pass


def _make_client_frame(payload: bytes, opcode: int = 0x1, fin: bool = True) -> bytes:
    """Build a masked WebSocket frame (clients must mask per RFC 6455 §5.1)."""
    mask = b'\xde\xad\xbe\xef'
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    length = len(payload)
    first = (0x80 if fin else 0x00) | opcode
    header = bytes([first])
    if length < 126:
        header += bytes([0x80 | length])
    elif length < 65536:
        header += bytes([0x80 | 126]) + length.to_bytes(2, 'big')
    else:
        header += bytes([0x80 | 127]) + length.to_bytes(8, 'big')
    return header + mask + masked


def _make_ws_scope(path: str) -> dict:
    return {
        'type': 'websocket',
        'asgi': {'version': '3.0', 'spec_version': '2.0'},
        'http_version': '1.1',
        'method': 'GET',
        'scheme': 'ws',
        'path': path,
        'raw_path': path.encode(),
        'query_string': b'',
        'root_path': '',
        'headers': Headers([
            (b'host', b'localhost:8000'),
            (b'upgrade', b'websocket'),
            (b'connection', b'upgrade'),
            (b'sec-websocket-key', b'dGhlIHNhbXBsZSBub25jZQ=='),
            (b'sec-websocket-version', b'13'),
        ]),
        'client': ['127.0.0.1', 54321],
        'server': ['localhost', 8000],
        'state': {},
    }


async def _drive_ws_session(app, path: str) -> None:
    """Run a WebSocket session via WebSocketHandler.run() (no client messages)."""
    scope = _make_ws_scope(path)
    reader = _FakeReader(b'')
    writer = _FakeWriter()
    handler = WebSocketHandler(app, reader, writer, scope)
    await handler.run()


async def _drive_ws_session_with_message(app, path: str, *, message: str) -> None:
    """Run a WebSocket session with one text message from the client."""
    scope = _make_ws_scope(path)
    payload = message.encode('utf-8')
    raw = _make_client_frame(payload, opcode=0x1) + _make_client_frame(b'', opcode=0x8)
    reader = _FakeReader(raw)
    writer = _FakeWriter()
    handler = WebSocketHandler(app, reader, writer, scope)
    await handler.run()


# ---------------------------------------------------------------------------
# Firing conditions
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_websocket_connected_fires_after_accept():
    """websocket_connected fires after the app sends websocket.accept."""
    app = BlackBull()
    captured: list[Event] = []
    seen = asyncio.Event()

    @app.on('websocket_connected')
    async def observer(event: Event):
        captured.append(event)
        seen.set()

    @app.route(path='/ws', scheme=Scheme.websocket)
    async def ws_handler(scope, receive, send):
        await receive()  # websocket.connect
        await send({'type': 'websocket.accept'})
        await send({'type': 'websocket.close'})

    await _drive_ws_session(app, '/ws')

    await asyncio.wait_for(seen.wait(), timeout=2.0)
    await asyncio.sleep(0.2)
    assert len(captured) == 1
    assert captured[0].name == 'websocket_connected'


# ---------------------------------------------------------------------------
# Detail shape
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_websocket_connected_detail_shape():
    """detail carries the expected identification fields."""
    app = BlackBull()
    captured: list[Event] = []
    seen = asyncio.Event()

    @app.on('websocket_connected')
    async def observer(event: Event):
        captured.append(event)
        seen.set()

    @app.route(path='/ws', scheme=Scheme.websocket)
    async def ws_handler(scope, receive, send):
        await receive()
        await send({'type': 'websocket.accept'})
        await send({'type': 'websocket.close'})

    await _drive_ws_session(app, '/ws')

    await asyncio.wait_for(seen.wait(), timeout=2.0)
    await asyncio.sleep(0.2)
    assert len(captured) == 1

    d = captured[0].detail
    assert 'scope' in d
    assert isinstance(d['connection_id'], str) and len(d['connection_id']) > 0
    assert isinstance(d['client_ip'], str)
    assert d['path'] == '/ws'
    assert d['subprotocol'] is None  # no subprotocol in accept


# ---------------------------------------------------------------------------
# Subprotocol propagation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_websocket_connected_subprotocol():
    """detail['subprotocol'] reflects the subprotocol from websocket.accept."""
    app = BlackBull()
    captured: list[Event] = []
    seen = asyncio.Event()

    @app.on('websocket_connected')
    async def observer(event: Event):
        captured.append(event)
        seen.set()

    @app.route(path='/ws', scheme=Scheme.websocket)
    async def ws_handler(scope, receive, send):
        await receive()
        await send({'type': 'websocket.accept', 'subprotocol': 'chat'})
        await send({'type': 'websocket.close'})

    await _drive_ws_session(app, '/ws')

    await asyncio.wait_for(seen.wait(), timeout=2.0)
    await asyncio.sleep(0.2)
    assert len(captured) == 1
    assert captured[0].detail['subprotocol'] == 'chat'


# ---------------------------------------------------------------------------
# Exactly-once semantics
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_websocket_connected_exactly_once():
    """A single connection produces exactly one event — no duplicates."""
    app = BlackBull()
    count = 0
    seen = asyncio.Event()

    @app.on('websocket_connected')
    async def observer(event: Event):
        nonlocal count
        count += 1
        seen.set()

    @app.route(path='/ws', scheme=Scheme.websocket)
    async def ws_handler(scope, receive, send):
        await receive()
        await send({'type': 'websocket.accept'})
        await send({'type': 'websocket.close'})

    await _drive_ws_session(app, '/ws')

    await asyncio.wait_for(seen.wait(), timeout=2.0)
    await asyncio.sleep(0.2)
    assert count == 1


# ---------------------------------------------------------------------------
# connection_id cross-event consistency
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_connection_id_available_in_scope_during_message():
    """websocket_connected writes _connection_id into scope; websocket_message
    observers see the same ID."""
    app = BlackBull()
    connected_ids: list[str] = []
    message_ids: list[str] = []
    message_seen = asyncio.Event()

    @app.on('websocket_connected')
    async def on_connected(event: Event):
        connected_ids.append(event.detail['connection_id'])

    @app.on('websocket_message')
    async def on_message(event: Event):
        cid = event.detail['scope'].get('_connection_id', '')
        message_ids.append(cid)
        message_seen.set()

    @app.route(path='/ws', scheme=Scheme.websocket)
    async def ws_handler(scope, receive, send):
        await receive()               # websocket.connect
        await send({'type': 'websocket.accept'})
        await receive()               # websocket.receive (message)
        await send({'type': 'websocket.close'})

    await _drive_ws_session_with_message(app, '/ws', message='hello')

    await asyncio.wait_for(message_seen.wait(), timeout=2.0)
    await asyncio.sleep(0.2)
    assert len(connected_ids) == 1
    assert len(message_ids) == 1
    assert connected_ids[0] == message_ids[0]
    assert len(connected_ids[0]) > 0


# ---------------------------------------------------------------------------
# Negative: HTTP requests must not fire websocket_connected
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_websocket_connected_not_fired_for_http():
    """An ordinary HTTP request must not fire websocket_connected."""
    app = BlackBull()
    fired: list[Event] = []

    @app.on('websocket_connected')
    async def observer(event: Event):
        fired.append(event)

    @app.route(path='/hello')
    async def handler(scope, receive, send):
        await send({'type': 'http.response.start', 'status': 200, 'headers': []})
        await send({'type': 'http.response.body', 'body': b'ok', 'more_body': False})

    raw = b'GET /hello HTTP/1.1\r\nHost: localhost:8000\r\n\r\n'
    writer = _FakeWriter()

    class _FakeReaderWithTransport(_FakeReader):
        pass

    class _WriterWithTransport:
        class _Transport:
            def get_extra_info(self, key, default=None):
                return None
        transport = _Transport()

        def __init__(self):
            self.written = bytearray()

        def write(self, data: bytes) -> None:
            self.written += data

        async def drain(self) -> None:
            pass

        def close(self) -> None:
            pass

        async def wait_closed(self) -> None:
            pass

    w = _WriterWithTransport()
    handler_inst = HTTP11Handler(app, _FakeReader(b''), w, raw[:1])
    handler_inst.request = raw
    await handler_inst.run()

    await asyncio.sleep(0.2)
    assert len(fired) == 0
