from __future__ import annotations

import gzip
import struct

from hpack import Decoder, Encoder
import pytest

from blackbull import BlackBull
from blackbull.grpc import GrpcServiceRegistry, decode_messages, encode_message
from blackbull.protocol.frame_types import FrameTypes
from tests.conformance.http2._harness import (
    _make_h2_frame, h2_connect, h2_section, h2_terminal,
)
from tests.harness import served


@pytest.fixture(params=['unary', 'server-streaming', 'client-streaming', 'bidi'])
def rpc(request):
    client_streaming = request.param in ('client-streaming', 'bidi')
    seen = []

    async def unary(message, context):
        if client_streaming:
            async for item in message:
                seen.append(item)
        else:
            seen.append(message)
        return b'ok'

    async def streaming(message, context):
        yield await unary(message, context)

    registry = GrpcServiceRegistry()
    registry.add_method('/svc/M',
                        streaming if request.param in ('server-streaming', 'bidi') else unary,
                        client_streaming=client_streaming)
    app = BlackBull()
    app.enable_grpc(registry)
    return app, seen, client_streaming


async def call(wire, encoder, decoder, chunks, *, sid=1, encoding=b'gzip', end_stream=True):
    fields = [(b':method', b'POST'), (b':scheme', b'http'),
              (b':authority', b'localhost'), (b':path', b'/svc/M'),
              (b'content-type', b'application/grpc'), (b'te', b'trailers')]
    if encoding is not None:
        fields.append((b'grpc-encoding', encoding))
    await wire.send(h2_section(encoder.encode(fields), sid=sid, end_stream=False))
    for index, chunk in enumerate(chunks):
        await wire.send(_make_h2_frame(
            FrameTypes.DATA, int(end_stream and index == len(chunks) - 1), sid, chunk))
    fields, body = await h2_terminal(wire, sid, decoder)
    assert isinstance(fields, list), (fields, body)
    return dict(fields)[b'grpc-status'], body


@pytest.mark.asyncio
@pytest.mark.parametrize('force_asgi', [False, True])
@pytest.mark.parametrize('flag', [0, 1, 2, 255])
@pytest.mark.parametrize('split', [None, 1, 2, 3, 4, 5, 6])
async def test_compression_flags(rpc, monkeypatch, force_asgi, flag, split):
    monkeypatch.setenv('BB_FORCE_ASGI_SCOPE', str(int(force_asgi)))
    app, seen, _ = rpc
    payload = gzip.compress(b'hello') if flag else b'hello'
    framed = struct.pack('>BI', flag, len(payload)) + payload
    chunks = [framed] if split is None else [framed[:split], framed[split:]]
    async with served(app) as server:
        wire = await h2_connect(server)
        encoder, decoder = Encoder(), Decoder()
        status, body = await call(wire, encoder, decoder, chunks)
        assert status == (b'0' if flag <= 1 else b'13')
        assert seen == ([b'hello'] if flag <= 1 else [])
        assert decode_messages(body) == ([(False, b'ok')] if flag <= 1 else [])
        status, body = await call(
            wire, encoder, decoder, [encode_message(b'next')], sid=3)
        assert status == b'0'
        assert decode_messages(body) == [(False, b'ok')]
        assert seen == ([b'hello', b'next'] if flag <= 1 else [b'next'])


@pytest.mark.asyncio
@pytest.mark.parametrize('force_asgi', [False, True])
@pytest.mark.parametrize('flag', [2, 255])
@pytest.mark.parametrize('encoding', [None, b'identity', b'gzip', b'br'])
@pytest.mark.parametrize('split', [1, 5])
async def test_invalid_flag_rejected_without_waiting_for_body(
        rpc, monkeypatch, force_asgi, flag, encoding, split):
    monkeypatch.setenv('BB_FORCE_ASGI_SCOPE', str(int(force_asgi)))
    app, seen, _ = rpc
    prefix = struct.pack('>BI', flag, 25)
    chunks = [prefix[:split]] if split == 5 else [prefix[:split], prefix[split:]]
    async with served(app) as server:
        wire = await h2_connect(server)
        status, body = await call(
            wire, Encoder(), Decoder(), chunks, encoding=encoding, end_stream=False)
    assert status == b'13'
    assert body == b''
    assert seen == []


@pytest.mark.asyncio
@pytest.mark.parametrize('force_asgi', [False, True])
@pytest.mark.parametrize('layout', ['coalesced', 'separate', 'split'])
async def test_invalid_flag_after_valid_message(rpc, monkeypatch, force_asgi, layout):
    monkeypatch.setenv('BB_FORCE_ASGI_SCOPE', str(int(force_asgi)))
    app, seen, client_streaming = rpc
    first = encode_message(b'first')
    invalid = struct.pack('>BI', 2, 25)
    chunks = {'coalesced': [first + invalid],
              'separate': [first, invalid],
              'split': [first + invalid[:1], invalid[1:]]}[layout]
    async with served(app) as server:
        wire = await h2_connect(server)
        encoder, decoder = Encoder(), Decoder()
        status, body = await call(wire, encoder, decoder, chunks, end_stream=False)
        assert status == b'13'
        assert body == b''
        assert seen == ([b'first'] if client_streaming else [])
        status, _ = await call(wire, encoder, decoder, [encode_message(b'next')], sid=3)
        assert status == b'0'
        assert seen == ([b'first', b'next'] if client_streaming else [b'next'])
