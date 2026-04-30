"""Tests for the websocket_disconnected Level B event.

Affected-tests baseline (must not regress after implementation):
- test_websocket_connected_event.py::* (websocket_connected must still fire)
- test_websocket_message_event.py::* (websocket_message must still fire)
"""
import asyncio
import pytest
from blackbull import BlackBull
from blackbull.event import Event
from blackbull.utils import Scheme
from blackbull.server.server import HTTP11Handler, WebSocketHandler
from blackbull.server.headers import Headers


# ---------------------------------------------------------------------------
# Shared test infrastructure  (mirrors test_websocket_connected_event.py)
# ---------------------------------------------------------------------------

class _FakeReader:
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
    """Run a WebSocket session via WebSocketHandler.run().

    Uses _FakeReader(b'') so _read_loop hits IncompleteReadError and
    emits websocket_disconnected with code 1006.
    """
    scope = _make_ws_scope(path)
    reader = _FakeReader(b'')
    writer = _FakeWriter()
    handler = WebSocketHandler(app, reader, writer, scope)
    await handler.run()


async def _drive_ws_session_with_close(app, path: str) -> None:
    """Run a WebSocket session ending with a proper client close frame (code 1000)."""
    scope = _make_ws_scope(path)
    close_frame = _make_client_frame(b'', opcode=0x8)
    reader = _FakeReader(close_frame)
    writer = _FakeWriter()
    handler = WebSocketHandler(app, reader, writer, scope)
    await handler.run()


# ---------------------------------------------------------------------------
# 1. Fires on disconnect
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_websocket_disconnected_fires():
    """websocket_disconnected fires when the connection closes."""
    app = BlackBull()
    captured: list[Event] = []
    seen = asyncio.Event()

    @app.on('websocket_disconnected')
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
    assert captured[0].name == 'websocket_disconnected'


# ---------------------------------------------------------------------------
# 2. Detail shape
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_websocket_disconnected_detail_shape():
    """detail carries scope, connection_id, client_ip, path, and code."""
    app = BlackBull()
    captured: list[Event] = []
    seen = asyncio.Event()

    @app.on('websocket_disconnected')
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
    assert isinstance(d['connection_id'], str)
    assert isinstance(d['client_ip'], str)
    assert d['path'] == '/ws'
    assert isinstance(d['code'], int)


# ---------------------------------------------------------------------------
# 3. connection_id matches websocket_connected
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_websocket_disconnected_connection_id_matches_connected():
    """detail['connection_id'] equals the id from the websocket_connected event."""
    app = BlackBull()
    connected_ids: list[str] = []
    disconnected_ids: list[str] = []
    disconnected_seen = asyncio.Event()

    @app.on('websocket_connected')
    async def on_connected(event: Event):
        connected_ids.append(event.detail['connection_id'])

    @app.on('websocket_disconnected')
    async def on_disconnected(event: Event):
        disconnected_ids.append(event.detail['connection_id'])
        disconnected_seen.set()

    @app.route(path='/ws', scheme=Scheme.websocket)
    async def ws_handler(scope, receive, send):
        await receive()
        await send({'type': 'websocket.accept'})
        await send({'type': 'websocket.close'})

    await _drive_ws_session(app, '/ws')

    await asyncio.wait_for(disconnected_seen.wait(), timeout=2.0)
    await asyncio.sleep(0.2)
    assert len(connected_ids) == 1
    assert len(disconnected_ids) == 1
    assert connected_ids[0] == disconnected_ids[0]
    assert len(connected_ids[0]) > 0


# ---------------------------------------------------------------------------
# 4. Exactly-once
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_websocket_disconnected_exactly_once():
    """A single connection close produces exactly one event — no duplicates."""
    app = BlackBull()
    count = 0
    seen = asyncio.Event()

    @app.on('websocket_disconnected')
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
# 5. Not fired for HTTP requests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_websocket_disconnected_not_fired_for_http():
    """An ordinary HTTP request must not fire websocket_disconnected."""
    app = BlackBull()
    fired: list[Event] = []

    @app.on('websocket_disconnected')
    async def observer(event: Event):
        fired.append(event)

    @app.route(path='/hello')
    async def handler(scope, receive, send):
        await send({'type': 'http.response.start', 'status': 200, 'headers': []})
        await send({'type': 'http.response.body', 'body': b'ok', 'more_body': False})

    raw = b'GET /hello HTTP/1.1\r\nHost: localhost:8000\r\n\r\n'

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
