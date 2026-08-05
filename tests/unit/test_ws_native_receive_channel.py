"""The WebSocket receive channel is native: a message is `str` or `bytes`.

The send half went native first; this is the other half of the same channel.
`WebSocketRecipient` built a `websocket.receive` dict for every message, which
the `WebSocket` object then took apart one frame later to hand the application
the `str | bytes` it had a moment earlier — a dict created and destroyed
entirely inside the framework, on every message, for the form most handlers
use.

The shape mirrors the HTTP body channel (`next_chunk`), because it is the same
problem: the payload is the value, and everything else is the channel's state.

    message = await recipient.next_message()   # str → text, bytes → binary
    raise WebSocketDisconnect(code, reason)    # the peer is gone
    raise ProtocolError                        # RFC 6455 violation, unchanged

`str` versus `bytes` *is* the text/binary discriminator — the object's public
contract already says so — so no tag is needed beside the payload.

`receive()` still returns the ASGI dicts, minted per call for the raw
`(conn, receive, send)` compat form and the external host.  The compat tests
here are the ones that keep the two encodings from drifting.
"""
import pytest

from blackbull.server.recipient import (
    AbstractReader, ProtocolError, WebSocketRecipient,
)
from blackbull.server.sender import AbstractWriter
from blackbull.server.ws_codec import WSOpcode, encode_frame
from blackbull.websocket import WebSocketDisconnect


class _Wire(AbstractReader):
    """Serves pre-encoded client frames, then EOF."""

    def __init__(self, *frames: bytes):
        self._d = bytearray(b''.join(frames))

    async def readexactly(self, n: int) -> bytes:
        if len(self._d) < n:
            from blackbull.server.recipient import IncompleteReadError
            out = bytes(self._d)
            self._d.clear()
            raise IncompleteReadError(out)
        out = bytes(self._d[:n])
        del self._d[:n]
        return out

    async def read(self, n: int) -> bytes:
        return await self.readexactly(n)

    async def readuntil(self, sep: bytes) -> bytes:
        raise NotImplementedError


class _NullWriter(AbstractWriter):
    def __init__(self):
        self.out = bytearray()

    async def write(self, data: bytes) -> None:
        self.out += data

    async def writelines(self, parts) -> None:
        for p in parts:
            self.out += p

    async def close(self) -> None:
        pass


def _client(payload: bytes, opcode: WSOpcode) -> bytes:
    """One masked client frame, the way a real peer sends it."""
    return encode_frame(payload, opcode=opcode, mask=True)


def _recipient(*frames: bytes) -> WebSocketRecipient:
    return WebSocketRecipient(_Wire(*frames), _NullWriter())


# ---------------------------------------------------------------------------
# The native channel
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_text_message_is_a_str():
    r = _recipient(_client(b'hello', WSOpcode.TEXT))
    await r.await_connect()
    assert await r.next_message() == 'hello'


@pytest.mark.asyncio
async def test_binary_message_is_bytes():
    r = _recipient(_client(b'\x00\xff', WSOpcode.BINARY))
    await r.await_connect()
    assert await r.next_message() == b'\x00\xff'


@pytest.mark.asyncio
async def test_the_type_is_the_discriminator():
    """No tag beside the payload: `str` is text, `bytes` is binary."""
    r = _recipient(_client(b'a', WSOpcode.TEXT),
                   _client(b'b', WSOpcode.BINARY))
    await r.await_connect()
    first, second = await r.next_message(), await r.next_message()
    assert isinstance(first, str) and first == 'a'
    assert isinstance(second, bytes) and second == b'b'


def _fragment(payload: bytes, opcode: WSOpcode, *, fin: bool) -> bytes:
    """One masked client frame with FIN under our control.

    ``encode_frame`` always sets FIN, so a fragmented message has to be built
    here: FIN is the top bit of the first octet, and the mask bit plus a
    zero mask key carry the client-side masking the server requires.
    """
    first = (0x80 if fin else 0x00) | int(opcode)
    return bytes([first, 0x80 | len(payload)]) + b'\x00\x00\x00\x00' + payload


@pytest.mark.asyncio
async def test_fragments_arrive_as_one_message():
    """Reassembly is the recipient's job in both encodings (RFC 6455 §5.4)."""
    r = _recipient(
        _fragment(b'he', WSOpcode.TEXT, fin=False),
        _fragment(b'llo', WSOpcode.CONTINUATION, fin=True),
    )
    await r.await_connect()
    assert await r.next_message() == 'hello'


@pytest.mark.asyncio
async def test_close_raises_with_the_peers_code():
    r = _recipient(_client((4002).to_bytes(2, 'big'), WSOpcode.CLOSE))
    await r.await_connect()
    with pytest.raises(WebSocketDisconnect) as exc:
        await r.next_message()
    assert exc.value.code == 4002


@pytest.mark.asyncio
async def test_eof_raises_abnormal():
    r = _recipient()          # nothing on the wire at all
    await r.await_connect()
    with pytest.raises(WebSocketDisconnect) as exc:
        await r.next_message()
    assert exc.value.code == 1006


@pytest.mark.asyncio
async def test_reading_past_the_close_keeps_raising():
    r = _recipient(_client((1000).to_bytes(2, 'big'), WSOpcode.CLOSE))
    await r.await_connect()
    for _ in range(2):
        with pytest.raises(WebSocketDisconnect):
            await r.next_message()


@pytest.mark.asyncio
async def test_protocol_violation_propagates():
    """An unmasked client frame is a violation; the app must see it."""
    r = _recipient(encode_frame(b'x', opcode=WSOpcode.TEXT, mask=False))
    await r.await_connect()
    with pytest.raises(ProtocolError):
        await r.next_message()


@pytest.mark.asyncio
async def test_invalid_utf8_is_a_1007_violation():
    r = _recipient(_client(b'\xff\xfe', WSOpcode.TEXT))
    await r.await_connect()
    with pytest.raises(ProtocolError) as exc:
        await r.next_message()
    assert exc.value.close_code == 1007


# ---------------------------------------------------------------------------
# The ASGI compat surface is unchanged
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_asgi_event_sequence_unchanged():
    r = _recipient(_client(b'hi', WSOpcode.TEXT),
                   _client(b'\x01', WSOpcode.BINARY),
                   _client((1000).to_bytes(2, 'big'), WSOpcode.CLOSE))
    assert await r() == {'type': 'websocket.connect'}
    assert await r() == {'type': 'websocket.receive', 'text': 'hi',
                         'bytes': None}
    assert await r() == {'type': 'websocket.receive', 'text': None,
                         'bytes': b'\x01'}
    assert await r() == {'type': 'websocket.disconnect', 'code': 1000}


@pytest.mark.asyncio
async def test_asgi_receive_past_the_terminal_event_keeps_answering():
    r = _recipient(_client((1001).to_bytes(2, 'big'), WSOpcode.CLOSE))
    await r()                                     # connect
    assert await r() == {'type': 'websocket.disconnect', 'code': 1001}
    assert await r() == {'type': 'websocket.disconnect', 'code': 1001}


@pytest.mark.asyncio
async def test_both_channels_carry_the_same_messages():
    frames = (_client(b'one', WSOpcode.TEXT), _client(b'two', WSOpcode.BINARY))

    native = _recipient(*frames)
    await native.await_connect()
    from_native = [await native.next_message(), await native.next_message()]

    asgi = _recipient(*frames)
    await asgi()
    events = [await asgi(), await asgi()]
    from_asgi = [e['text'] if e['text'] is not None else e['bytes']
                 for e in events]

    assert from_native == from_asgi == ['one', b'two']


# ---------------------------------------------------------------------------
# The Level B event detail keeps its documented shape
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_websocket_message_event_detail_is_unchanged():
    """`detail['text']` / `detail['bytes']` are the documented keys."""
    seen = []

    async def on_message(message):
        seen.append(message)

    r = WebSocketRecipient(
        _Wire(_client(b'hi', WSOpcode.TEXT), _client(b'\x02', WSOpcode.BINARY)),
        _NullWriter(), on_message=on_message)
    await r.await_connect()
    await r.next_message()
    await r.next_message()

    assert seen == ['hi', b'\x02'], (
        f'the read-time adapter got {seen!r}')
