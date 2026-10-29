import asyncio
import logging

from hpack import Decoder, Encoder
import pytest

from blackbull.protocol.frame_types import ErrorCodes, FrameTypes
from tests.conformance.http2._harness import (
    _make_h2_frame, h2_section, h2_terminal,
)
from tests.harness import read_h2_frame, served


FIELDS = [(b':method', b'GET'), (b':scheme', b'http'),
          (b':path', b'/'), (b':authority', b'localhost')]


async def _connect(server):
    wire = await server.wire()
    await wire.send(b'PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n'
                    + _make_h2_frame(FrameTypes.SETTINGS))
    settings = {}
    while True:
        kind, flags, _, payload = await read_h2_frame(wire.reader)
        if kind == 4:
            if flags & 1:
                return wire, settings
            settings.update((int.from_bytes(payload[i:i + 2], 'big'),
                             int.from_bytes(payload[i + 2:i + 6], 'big'))
                            for i in range(0, len(payload), 6))
            await wire.send(_make_h2_frame(FrameTypes.SETTINGS, 1))


def _app(seen, entered=None):
    async def app(conn, receive, send):
        seen.append(conn)
        if entered is not None:
            entered.set()
        while (await receive()).get('more_body', False):
            pass
        await send({'type': 'http.response.start', 'status': 200, 'headers': []})
        await send({'type': 'http.response.body', 'body': b'ok'})
    return app


@pytest.mark.asyncio
@pytest.mark.parametrize('limit,advertised', [(None, 65536), (256, 256),
    (131072, 131072), (0, None), (2**32, 2**32 - 1)])
async def test_advertises_the_configured_decode_limit(monkeypatch, limit, advertised):
    if limit is None:
        monkeypatch.delenv('BB_HEADER_MAX_TOTAL', raising=False)
    else:
        monkeypatch.setenv('BB_HEADER_MAX_TOTAL', str(limit))
    async with served(_app([])) as server:
        _, settings = await _connect(server)
        assert settings.get(6) == advertised


@pytest.mark.asyncio
@pytest.mark.parametrize('force_asgi', [False, True])
@pytest.mark.parametrize('split', [False, True])
@pytest.mark.parametrize('excess', [0, 1])
async def test_decode_boundary_matches_the_advertisement(monkeypatch, caplog,
                                                         force_asgi, split, excess):
    monkeypatch.setenv('BB_FORCE_ASGI_SCOPE', str(int(force_asgi)))
    monkeypatch.setenv('BB_HEADER_MAX_TOTAL', '256')
    caplog.set_level(logging.WARNING, logger='blackbull.caps')
    fields = FIELDS + [(b'x', b'a' * (256 + excess - 32 - 1
        - sum(len(k) + len(v) + 32 for k, v in FIELDS)))]
    assert sum(len(k) + len(v) + 32 for k, v in fields) == 256 + excess
    block = Encoder().encode(fields)
    assert len(block) < 256
    seen = []
    async with served(_app(seen)) as server:
        wire, settings = await _connect(server)
        await wire.send(h2_section(block, split=split))
        result = await h2_terminal(wire, 1, Decoder())
        if excess:
            assert result == (7, ErrorCodes.ENHANCE_YOUR_CALM)
            assert await wire.closed()
            assert not seen
        else:
            assert (b':status', b'200') in result[0] and result[1] == b'ok'
            assert len(seen) == 1
        assert settings.get(6) == 256
    records = [r for r in caplog.records if getattr(r, 'cap', None) == 'header_max_total']
    assert len(records) == excess
    if excess:
        assert records[0].limit == 256 and records[0].requested >= 257
        assert records[0].protocol == 'http2'


@pytest.mark.asyncio
@pytest.mark.parametrize('limit', [65536, 131072, 0])
@pytest.mark.parametrize('split', [False, True])
@pytest.mark.parametrize('trailer', [False, True])
async def test_compressed_expansion_moves_with_the_limit(monkeypatch, caplog, limit,
                                                        split, trailer):
    monkeypatch.setenv('BB_HEADER_MAX_TOTAL', str(limit))
    caplog.set_level(logging.WARNING, logger='blackbull.caps')
    peer = Encoder()
    initial = peer.encode(FIELDS) if trailer else None
    fields = ([] if trailer else FIELDS) + [(b'x-big', b'a' * 2000)] * 40
    decoded = sum(len(k) + len(v) + 32 for k, v in fields)
    assert 65536 < decoded < 131072
    block = peer.encode(fields)
    assert len(block) < 16384
    seen = []
    entered = asyncio.Event()
    async with served(_app(seen, entered)) as server:
        wire, _ = await _connect(server)
        if trailer:
            await wire.send(h2_section(initial, end_stream=False))
            await asyncio.wait_for(entered.wait(), 1)
        await wire.send(h2_section(block, split=split))
        result = await h2_terminal(wire, 1, Decoder())
        if limit == 65536:
            assert result == (7, ErrorCodes.ENHANCE_YOUR_CALM)
            assert await wire.closed()
            assert len(seen) == int(trailer)
        else:
            assert result[1] == b'ok' and len(seen) == 1


@pytest.mark.asyncio
async def test_valid_table_update_and_dynamic_reference_keep_sibling_stream(monkeypatch, caplog):
    monkeypatch.setenv('BB_HEADER_MAX_TOTAL', '256')
    caplog.set_level(logging.WARNING, logger='blackbull.caps')
    peer, decoder = Encoder(), Decoder()
    peer.header_table_size = 128
    fields = FIELDS + [(b'x-state', b'kept')]
    first = peer.encode(fields)
    second = peer.encode(fields)
    assert len(second) < len(first)
    seen = []
    async with served(_app(seen)) as server:
        wire, _ = await _connect(server)
        for sid, block in [(1, first), (3, second)]:
            await wire.send(h2_section(block, sid=sid))
            result = await h2_terminal(wire, sid, decoder)
            assert result[1] == b'ok'
        assert len(seen) == 2
    assert not [r for r in caplog.records if getattr(r, 'cap', None) == 'header_max_total']


@pytest.mark.asyncio
@pytest.mark.parametrize('split', [False, True])
@pytest.mark.parametrize('payload', [b'\x80', b'\x3f\xe2\x1f'])
async def test_bad_hpack_remains_compression_error(monkeypatch, caplog, split, payload):
    monkeypatch.setenv('BB_HEADER_MAX_TOTAL', '256')
    caplog.set_level(logging.WARNING, logger='blackbull.caps')
    seen = []
    async with served(_app(seen)) as server:
        wire, _ = await _connect(server)
        await wire.send(h2_section(payload, split=split))
        assert await h2_terminal(wire, 1, Decoder()) == (7, ErrorCodes.COMPRESSION_ERROR)
        assert await wire.closed() and not seen
    assert not [r for r in caplog.records if getattr(r, 'cap', None) == 'header_max_total']
