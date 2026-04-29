"""Integration tests for the websocket_message Level B event.

The event fires once per fully reassembled WebSocket message, eagerly —
before the application handler calls receive().
"""
import asyncio
import pytest
from blackbull import BlackBull, Event
from blackbull.utils import Scheme
from blackbull.server.recipient import WebSocketRecipient, AsyncioReader


# ---------------------------------------------------------------------------
# Frame builders
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Fake transport
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
            raise asyncio.IncompleteReadError(bytes(self._buf), None)
        chunk = bytes(self._buf[:idx + len(sep)])
        del self._buf[:idx + len(sep)]
        return chunk

    async def read(self, n: int) -> bytes:
        chunk = bytes(self._buf[:n])
        del self._buf[:n]
        return chunk


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


# ---------------------------------------------------------------------------
# Test harness helpers
# ---------------------------------------------------------------------------

def _make_scope(path: str) -> dict:
    return {
        'type': 'websocket',
        'path': path,
        'headers': [],
        'query_string': b'',
    }


async def _drive_ws_client(app, path, *, send_text=None, send_bytes=None):
    """Run app against one WebSocket message exchange."""
    if send_text is not None:
        payload, opcode = send_text.encode('utf-8'), 0x1
    else:
        payload, opcode = send_bytes, 0x2
    raw = _make_client_frame(payload, opcode=opcode) + _make_client_frame(b'', opcode=0x8)
    scope = _make_scope(path)
    receive = WebSocketRecipient(
        AsyncioReader(_FakeReader(raw)),
        _FakeWriter(),
        dispatcher=app._dispatcher,
        scope=scope,
    )
    async def send(event, status=None, headers=None):
        pass
    await app(scope, receive, send)


async def _drive_ws_client_fragments(app, path, *, fragments):
    """Run app against a fragmented WebSocket text message."""
    first, *rest = fragments
    raw = _make_client_frame(first.encode(), opcode=0x1, fin=False)
    for i, part in enumerate(rest):
        raw += _make_client_frame(part.encode(), opcode=0x0, fin=(i == len(rest) - 1))
    raw += _make_client_frame(b'', opcode=0x8)
    scope = _make_scope(path)
    receive = WebSocketRecipient(
        AsyncioReader(_FakeReader(raw)),
        _FakeWriter(),
        dispatcher=app._dispatcher,
        scope=scope,
    )
    async def send(event, status=None, headers=None):
        pass
    await app(scope, receive, send)


async def _drive_ws_client_multi(app, path, *, messages):
    """Run app against multiple WebSocket text messages."""
    raw = b''.join(_make_client_frame(m.encode(), opcode=0x1) for m in messages)
    raw += _make_client_frame(b'', opcode=0x8)
    scope = _make_scope(path)
    receive = WebSocketRecipient(
        AsyncioReader(_FakeReader(raw)),
        _FakeWriter(),
        dispatcher=app._dispatcher,
        scope=scope,
    )
    async def send(event, status=None, headers=None):
        pass
    await app(scope, receive, send)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_websocket_message_event_fires_for_text_frame():
    """A text WebSocket message produces one websocket_message event."""
    app = BlackBull()
    received_events: list[Event] = []
    fired = asyncio.Event()

    @app.on('websocket_message')
    async def observer(event: Event):
        received_events.append(event)
        fired.set()

    @app.route(path='/ws', scheme=Scheme.websocket)
    async def ws_handler(scope, receive, send):
        await receive()
        await send({'type': 'websocket.accept'})
        await receive()
        await send({'type': 'websocket.close'})

    await _drive_ws_client(app, '/ws', send_text='hello')

    await asyncio.wait_for(fired.wait(), timeout=2.0)
    assert len(received_events) == 1
    e = received_events[0]
    assert e.name == 'websocket_message'
    assert e.detail['text'] == 'hello'
    assert e.detail['bytes'] is None
    assert e.detail['scope']['path'] == '/ws'


@pytest.mark.asyncio
async def test_websocket_message_event_fires_for_binary_frame():
    """A binary WebSocket message produces one websocket_message event."""
    app = BlackBull()
    received_events: list[Event] = []
    fired = asyncio.Event()

    @app.on('websocket_message')
    async def observer(event: Event):
        received_events.append(event)
        fired.set()

    @app.route(path='/ws', scheme=Scheme.websocket)
    async def ws_handler(scope, receive, send):
        await receive()
        await send({'type': 'websocket.accept'})
        await receive()
        await send({'type': 'websocket.close'})

    await _drive_ws_client(app, '/ws', send_bytes=b'\x00\x01\x02')

    await asyncio.wait_for(fired.wait(), timeout=2.0)
    assert received_events[0].detail['bytes'] == b'\x00\x01\x02'
    assert received_events[0].detail['text'] is None


@pytest.mark.asyncio
async def test_websocket_message_event_fires_once_per_fragmented_message():
    """A fragmented message produces exactly one websocket_message event."""
    app = BlackBull()
    received_events: list[Event] = []
    barrier = asyncio.Event()

    @app.on('websocket_message')
    async def observer(event: Event):
        received_events.append(event)
        barrier.set()

    @app.route(path='/ws', scheme=Scheme.websocket)
    async def ws_handler(scope, receive, send):
        await receive()
        await send({'type': 'websocket.accept'})
        await receive()
        await send({'type': 'websocket.close'})

    await _drive_ws_client_fragments(app, '/ws', fragments=['hel', 'lo', ''])

    await asyncio.wait_for(barrier.wait(), timeout=2.0)
    assert len(received_events) == 1
    assert received_events[0].detail['text'] == 'hello'


@pytest.mark.asyncio
async def test_websocket_message_event_fires_for_each_message_on_same_connection():
    """Multiple messages on one connection produce multiple events in order."""
    app = BlackBull()
    received_events: list[Event] = []
    target_count = 3
    done = asyncio.Event()

    @app.on('websocket_message')
    async def observer(event: Event):
        received_events.append(event)
        if len(received_events) >= target_count:
            done.set()

    @app.route(path='/ws', scheme=Scheme.websocket)
    async def ws_handler(scope, receive, send):
        await receive()
        await send({'type': 'websocket.accept'})
        for _ in range(target_count):
            await receive()
        await send({'type': 'websocket.close'})

    await _drive_ws_client_multi(app, '/ws', messages=['first', 'second', 'third'])

    await asyncio.wait_for(done.wait(), timeout=2.0)
    assert [e.detail['text'] for e in received_events] == ['first', 'second', 'third']


@pytest.mark.asyncio
async def test_websocket_message_event_fires_even_if_handler_does_not_consume():
    """The event fires when the server reads the message, not when the
    handler calls receive()."""
    app = BlackBull()
    received_events: list[Event] = []
    observed = asyncio.Event()

    @app.on('websocket_message')
    async def observer(event: Event):
        received_events.append(event)
        observed.set()

    @app.route(path='/ws', scheme=Scheme.websocket)
    async def ws_handler(scope, receive, send):
        await receive()
        await send({'type': 'websocket.accept'})
        await asyncio.sleep(0.05)
        await send({'type': 'websocket.close'})

    await _drive_ws_client(app, '/ws', send_text='ignored-by-handler')

    await asyncio.wait_for(observed.wait(), timeout=2.0)
    assert received_events[0].detail['text'] == 'ignored-by-handler'
