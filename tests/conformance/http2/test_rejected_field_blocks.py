import asyncio

from hpack import Decoder, Encoder, HPACKError
import pytest

from blackbull import BlackBull
from blackbull.protocol.frame_types import ErrorCodes, FrameTypes
from tests.conformance.http2._harness import _make_h2_frame as frame
from tests.harness import read_h2_frame, served


FIELDS = [(b':method', b'GET'), (b':scheme', b'http'),
          (b':authority', b'localhost'), (b':path', b'/')]
STATE = (b'x-state', b'inserted-by-rejected-section')


def serve(handler):
    app = BlackBull()
    app.route(path='/')(handler)
    return served(app)


def section(block, *, sid=1, split=False, end_stream=True, self_dependent=False):
    flags = int(end_stream) | (0x20 if self_dependent else 0)
    prefix = sid.to_bytes(4, 'big') + b'\x00' if self_dependent else b''
    if not split:
        return frame(FrameTypes.HEADERS, flags | 4, sid, prefix + block)
    cut = len(block) // 2
    return (frame(FrameTypes.HEADERS, flags, sid, prefix + block[:cut])
            + frame(FrameTypes.CONTINUATION, 4, sid, block[cut:]))


async def connect(server):
    wire = await server.wire()
    await wire.send(b'PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n'
                    + frame(FrameTypes.SETTINGS))
    while True:
        kind, flags, _, _ = await read_h2_frame(wire.reader)
        if kind == 4:
            if flags & 1:
                return wire
            await wire.send(frame(FrameTypes.SETTINGS, 1))


async def terminal(wire, sid, decoder):
    body = bytearray()
    fields = []
    while True:
        kind, flags, stream, payload = await read_h2_frame(wire.reader)
        if kind == 7:
            return kind, int.from_bytes(payload[4:8], 'big')
        if kind == 1:
            decoded = decoder.decode(payload, raw=True)
            if stream == sid:
                fields.extend(decoded)
        if stream != sid:
            continue
        if kind == 3:
            return kind, int.from_bytes(payload, 'big')
        if kind == 0:
            body.extend(payload)
        if kind in (0, 1) and flags & 1:
            return fields, bytes(body)


async def echo(conn, receive, send):
    while (event := await receive()).get('more_body', False):
        pass
    if event['type'] == 'http.disconnect':
        return
    await send({'type': 'http.response.start', 'status': 200, 'headers': []})
    await send({'type': 'http.response.body', 'body': conn.headers.get(b'x-state', b'')})


@pytest.mark.asyncio
@pytest.mark.parametrize('force_asgi', [False, True])
@pytest.mark.parametrize('split', [False, True])
@pytest.mark.parametrize('door', ['initial', 'trailer', 'half-closed'])
async def test_rejected_section_preserves_sibling_dynamic_table(
        monkeypatch, force_asgi, split, door):
    monkeypatch.setenv('BB_FORCE_ASGI_SCOPE', str(int(force_asgi)))
    entered = asyncio.Event()

    async def app(conn, receive, send):
        if door == 'half-closed' and not conn.headers.get(b'x-state'):
            entered.set()
            await asyncio.Event().wait()
        else:
            entered.set()
            await echo(conn, receive, send)

    peer, decoder = Encoder(), Decoder()
    async with serve(app) as server:
        wire = await connect(server)
        if door != 'initial':
            await wire.send(section(peer.encode(FIELDS), end_stream=door == 'half-closed'))
            await asyncio.wait_for(entered.wait(), 2)
        rejected = peer.encode((FIELDS if door == 'initial' else []) + [STATE])
        await wire.send(section(rejected, split=split,
                                self_dependent=door != 'half-closed'))
        assert await terminal(wire, 1, decoder) == (
            3, ErrorCodes.STREAM_CLOSED if door == 'half-closed' else ErrorCodes.PROTOCOL_ERROR)
        following = peer.encode(FIELDS + [STATE])
        with pytest.raises(HPACKError):
            Decoder().decode(following)
        await wire.send(section(following, sid=3))
        result = await terminal(wire, 3, decoder)
        assert result[1] == STATE[1], result
        assert (b':status', b'200') in result[0]


@pytest.mark.asyncio
async def test_rejected_half_closed_section_still_requires_continuation():
    entered = asyncio.Event()

    async def app(conn, receive, send):
        entered.set()
        await receive()
        await receive()

    peer = Encoder()
    async with serve(app) as server:
        wire = await connect(server)
        await wire.send(section(peer.encode(FIELDS)))
        await asyncio.wait_for(entered.wait(), 2)
        await wire.send(frame(FrameTypes.HEADERS, 1, 1, b'\x00')
                        + section(peer.encode(FIELDS), sid=3))
        assert await terminal(wire, 3, Decoder()) == (7, ErrorCodes.PROTOCOL_ERROR)
        assert await wire.closed()


@pytest.mark.asyncio
@pytest.mark.parametrize('split', [False, True])
async def test_self_dependency_does_not_hide_compression_error(split):
    async with serve(echo) as server:
        wire = await connect(server)
        await wire.send(section(b'\x80', split=split, self_dependent=True))
        assert await terminal(wire, 1, Decoder()) == (7, ErrorCodes.COMPRESSION_ERROR)
        assert await wire.closed()


@pytest.mark.asyncio
@pytest.mark.parametrize('split', [False, True])
@pytest.mark.parametrize('door', ['initial', 'trailer', 'push'])
async def test_decode_limit_is_connection_fatal(split, door):
    peer = Encoder()
    block = peer.encode((FIELDS if door == 'initial' else [])
                        + [(b'x-big', b'0' * 2000)] * 40)
    assert len(block) < 16384
    async with serve(echo) as server:
        wire = await connect(server)
        if door == 'trailer':
            await wire.send(section(Encoder().encode(FIELDS), end_stream=False))
        if door == 'push':
            raw = frame(FrameTypes.PUSH_PROMISE, 0 if split else 4, 1,
                        (2).to_bytes(4, 'big') + block)
        else:
            raw = section(block, split=split)
        await wire.send(raw)
        # A client push is forbidden before its incomplete block is decoded.
        expected = ErrorCodes.PROTOCOL_ERROR if door == 'push' and split else ErrorCodes.COMPRESSION_ERROR
        assert await terminal(wire, 1, Decoder()) == (7, expected)
        assert await wire.closed()


@pytest.mark.asyncio
@pytest.mark.parametrize('complete', [False, True])
@pytest.mark.parametrize('trailer', [False, True])
async def test_encoded_accumulation_limit_closes_without_reading_more(
        monkeypatch, complete, trailer):
    monkeypatch.setenv('BB_HEADER_MAX_TOTAL', '64')
    peer = Encoder()
    async with serve(echo) as server:
        wire = await connect(server)
        if trailer:
            await wire.send(section(peer.encode(FIELDS), end_stream=False))
        block = peer.encode(([] if trailer else FIELDS) + [(b'x-large', b'~' * 100)])
        assert len(block) > 64
        await wire.send(frame(FrameTypes.HEADERS, 1, 1, block[:1])
                        + frame(FrameTypes.CONTINUATION, 4 if complete else 0, 1, block[1:]))
        assert await terminal(wire, 1, Decoder()) == (7, ErrorCodes.ENHANCE_YOUR_CALM)
        assert await wire.closed()


@pytest.mark.asyncio
@pytest.mark.parametrize('split', [False, True])
async def test_concurrency_refusal_preserves_dynamic_table(monkeypatch, split):
    monkeypatch.setenv('BB_H2_MAX_CONCURRENT_STREAMS', '1')
    entered, release = asyncio.Event(), asyncio.Event()

    async def app(conn, receive, send):
        if not conn.headers.get(b'x-state'):
            entered.set()
            await release.wait()
        await echo(conn, receive, send)

    peer, decoder = Encoder(), Decoder()
    async with serve(app) as server:
        wire = await connect(server)
        await wire.send(section(peer.encode(FIELDS)))
        await asyncio.wait_for(entered.wait(), 2)
        await wire.send(section(peer.encode(FIELDS + [STATE]), sid=3, split=split))
        assert await terminal(wire, 3, decoder) == (3, ErrorCodes.REFUSED_STREAM)
        release.set()
        assert (await terminal(wire, 1, decoder))[1] == b''
        await wire.send(section(peer.encode(FIELDS + [STATE]), sid=5))
        result = await terminal(wire, 5, decoder)
        assert result[1] == STATE[1], result
        assert (b':status', b'200') in result[0]
