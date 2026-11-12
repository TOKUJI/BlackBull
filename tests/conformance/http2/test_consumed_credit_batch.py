import asyncio

import pytest
from hpack import Encoder

from blackbull import BlackBull
from blackbull.protocol.frame_types import FrameTypes
from tests.conformance.http2._harness import (
    _make_h2_frame, h2_connect, h2_section,
)
from tests.harness import read_h2_frame, served


@pytest.mark.asyncio
@pytest.mark.parametrize('tls', [False, True])
@pytest.mark.parametrize('compat', ['0', '1'])
async def test_finished_requests_return_connection_credit_once(tls, compat, monkeypatch):
    monkeypatch.setenv('BB_FORCE_ASGI_SCOPE', compat)
    app = BlackBull()
    admitted = 0
    ready = asyncio.Event()

    @app.route(methods='POST', path='/')
    async def upload(conn):
        nonlocal admitted
        admitted += 1
        if admitted == 8:
            ready.set()
        await ready.wait()
        assert await conn.body() == b'payload'
        return b'ok'

    async with served(app, tls=tls) as server:
        wire = await h2_connect(server, alpn=['h2'])
        encoder = Encoder()
        requests = []
        for sid in range(1, 16, 2):
            requests.extend((h2_section(encoder.encode([
                (b':method', b'POST'), (b':scheme', b'https' if tls else b'http'),
                (b':authority', b'localhost'), (b':path', b'/'),
            ]), sid=sid, end_stream=False),
                _make_h2_frame(FrameTypes.DATA, 1, sid, b'payload')))
        await wire.send(b''.join(requests))
        ended = set()
        updates = []
        while len(ended) < 8 or sum(n for sid, n in updates if sid == 0) < 56:
            kind, flags, sid, payload = await read_h2_frame(wire.reader)
            assert kind not in (3, 7)
            if kind == 8:
                updates.append((sid, int.from_bytes(payload, 'big')))
            elif kind in (0, 1) and flags & 1:
                ended.add(sid)
        assert updates == [(0, 56)]
