"""The WebSocket send channel is native, like the HTTP one.

The `WebSocket` object was described as the "native form", but it was a facade:
`accept()` / `send_text()` / `send_bytes()` / `close()` all built `websocket.*`
ASGI dicts and pushed them down the *same* channel the raw
`(conn, receive, send)` form uses.  The handler never saw them — that is the
object's point — but middleware, the actor, and the sender all did, and the
sender had no native arm at all (HTTP gained `case NativeResponse():` in
Sprint 93; WS never did).

So the tests here assert two things, and the second is what makes the first
safe: the channel carries `NativeWSMessage`, and the bytes those messages put
on the wire are the same bytes the dicts produced.
"""
import pytest

from blackbull.native import NativeWSMessage
from blackbull.server.sender import AbstractWriter, WebSocketSender
from blackbull.server.ws_codec import WSOpcode, read_frame_header


class _CollectingWriter(AbstractWriter):
    def __init__(self):
        self.out = bytearray()

    async def write(self, data: bytes) -> None:
        self.out += data

    async def writelines(self, parts) -> None:
        for part in parts:
            self.out += part

    async def close(self) -> None:
        pass


class _Bytes:
    """Minimal reader over a bytes buffer, for decoding what was written."""

    def __init__(self, data: bytes):
        self._d = bytes(data)
        self._i = 0

    async def readexactly(self, n: int) -> bytes:
        out = self._d[self._i:self._i + n]
        self._i += n
        return out

    async def read(self, n: int) -> bytes:
        return await self.readexactly(n)


async def _decode_one(raw: bytes):
    """Return ``(opcode, payload)`` of the single frame in *raw*."""
    reader = _Bytes(raw)
    h = await read_frame_header(reader)
    payload = await reader.readexactly(h.length)
    return h.opcode, payload


# ---------------------------------------------------------------------------
# The sender grows a native arm
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_native_text_message_writes_a_text_frame():
    writer = _CollectingWriter()
    sender = WebSocketSender(writer)

    await sender(NativeWSMessage.text_message('hello'))

    opcode, payload = await _decode_one(bytes(writer.out))
    assert opcode == WSOpcode.TEXT
    assert payload == b'hello'


@pytest.mark.asyncio
async def test_native_binary_message_writes_a_binary_frame():
    writer = _CollectingWriter()
    sender = WebSocketSender(writer)

    await sender(NativeWSMessage.binary_message(b'\x00\xff'))

    opcode, payload = await _decode_one(bytes(writer.out))
    assert opcode == WSOpcode.BINARY
    assert payload == b'\x00\xff'


@pytest.mark.asyncio
async def test_native_close_writes_a_close_frame_with_the_code():
    writer = _CollectingWriter()
    sender = WebSocketSender(writer)

    await sender(NativeWSMessage.close(4001))

    opcode, payload = await _decode_one(bytes(writer.out))
    assert opcode == WSOpcode.CLOSE
    assert int.from_bytes(payload[:2], 'big') == 4001


@pytest.mark.asyncio
async def test_native_accept_writes_nothing():
    """The 101/200 handshake reply is the actor's job, not the sender's."""
    writer = _CollectingWriter()
    sender = WebSocketSender(writer)

    await sender(NativeWSMessage.accept('chat'))

    assert bytes(writer.out) == b''


@pytest.mark.asyncio
async def test_native_and_dict_put_identical_bytes_on_the_wire():
    """The compat surface must not drift from the native one."""
    cases = [
        (NativeWSMessage.text_message('hi'),
         {'type': 'websocket.send', 'text': 'hi'}),
        (NativeWSMessage.binary_message(b'xy'),
         {'type': 'websocket.send', 'bytes': b'xy'}),
        (NativeWSMessage.close(1001),
         {'type': 'websocket.close', 'code': 1001}),
    ]
    for native, event in cases:
        nw, dw = _CollectingWriter(), _CollectingWriter()
        await WebSocketSender(nw)(native)
        await WebSocketSender(dw)(event)
        assert bytes(nw.out) == bytes(dw.out), (
            f'{native.kind} diverged: native={bytes(nw.out)!r} '
            f'dict={bytes(dw.out)!r}')


# ---------------------------------------------------------------------------
# to_asgi() is the boundary conversion, and it round-trips
# ---------------------------------------------------------------------------

def test_to_asgi_reproduces_the_wire_events():
    assert NativeWSMessage.text_message('hi').to_asgi() == [
        {'type': 'websocket.send', 'text': 'hi'}]
    assert NativeWSMessage.binary_message(b'xy').to_asgi() == [
        {'type': 'websocket.send', 'bytes': b'xy'}]
    assert NativeWSMessage.accept('chat').to_asgi() == [
        {'type': 'websocket.accept', 'subprotocol': 'chat'}]
    assert NativeWSMessage.close(1001).to_asgi() == [
        {'type': 'websocket.close', 'code': 1001}]


def test_accept_headers_ride_the_boundary_conversion():
    msg = NativeWSMessage.accept('chat', [(b'x-a', b'1')])
    assert msg.to_asgi() == [{'type': 'websocket.accept',
                              'subprotocol': 'chat',
                              'headers': [(b'x-a', b'1')]}]


def test_close_reason_is_omitted_when_empty():
    """ASGI treats a missing reason and an empty one alike; don't invent keys."""
    assert 'reason' not in NativeWSMessage.close(1000).to_asgi()[0]
    assert NativeWSMessage.close(1000, 'bye').to_asgi()[0]['reason'] == 'bye'
