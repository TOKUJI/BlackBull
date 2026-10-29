import asyncio

from hpack import Decoder, Encoder, HPACKError
import pytest

from blackbull import BlackBull
from blackbull.protocol.frame_types import ErrorCodes, FrameTypes
from tests.conformance.http2._harness import (
    _make_h2_frame as frame, h2_section as section,
    h2_connect as connect, h2_terminal as terminal,
)
from tests.harness import served


FIELDS = [(b':method', b'GET'), (b':scheme', b'http'),
          (b':authority', b'localhost'), (b':path', b'/')]
STATE = (b'x-state', b'inserted-by-rejected-section')


def serve(handler):
    app = BlackBull()
    app.route(path='/')(handler)
    return served(app)


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
        expected = ErrorCodes.PROTOCOL_ERROR if door == 'push' and split else ErrorCodes.ENHANCE_YOUR_CALM
        assert await terminal(wire, 1, Decoder()) == (7, expected)
        assert await wire.closed()


@pytest.mark.asyncio
@pytest.mark.parametrize('complete', [False, True])
@pytest.mark.parametrize('trailer', [False, True])
async def test_encoded_accumulation_limit_closes_without_reading_more(
        monkeypatch, caplog, complete, trailer):
    monkeypatch.setenv('BB_HEADER_MAX_TOTAL', '256')
    peer = Encoder()
    async with serve(echo) as server:
        wire = await connect(server)
        if trailer:
            await wire.send(section(peer.encode(FIELDS), end_stream=False))
        block = peer.encode(([] if trailer else FIELDS) + [(b'x-large', b'~' * 400)])
        assert len(block) > 256
        await wire.send(frame(FrameTypes.HEADERS, 1, 1, block[:1])
                        + frame(FrameTypes.CONTINUATION, 4 if complete else 0, 1, block[1:]))
        assert await terminal(wire, 1, Decoder()) == (7, ErrorCodes.ENHANCE_YOUR_CALM)
        assert await wire.closed()
    records = [r for r in caplog.records if getattr(r, 'cap', None) == 'header_max_total']
    assert len(records) == 1
    assert records[0].limit == 256 and records[0].requested == len(block)


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
