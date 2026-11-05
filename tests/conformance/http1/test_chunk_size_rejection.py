import pytest

from blackbull import BlackBull
from tests.harness import served


@pytest.mark.asyncio
@pytest.mark.parametrize('force_asgi', ['0', '1'])
@pytest.mark.parametrize('pipelined', [False, True])
@pytest.mark.parametrize('size,expected', [
    (b'-1', 400),
    (b'0x10', 400),
    (b'100', None),
    (b'F' * 16, None),
    (b'F' * 40, None),
])
async def test_chunk_syntax_and_incomplete_body_are_distinct(
        monkeypatch, force_asgi, pipelined, size, expected):
    monkeypatch.setenv('BB_FORCE_ASGI_SCOPE', force_asgi)
    app = BlackBull()
    calls = []

    @app.route(path='/upload', methods=['POST'])
    async def upload(body: bytes):
        calls.append('upload')
        return body

    @app.route(path='/sentinel')
    async def sentinel():
        calls.append('sentinel')
        return b'ok'

    async with served(app) as server:
        wire = await server.wire()
        follow = b'GET /sentinel HTTP/1.1\r\nHost: test\r\n\r\n' if pipelined else b''
        await wire.send(b'POST /upload HTTP/1.1\r\nHost: test\r\n'
                        b'Transfer-Encoding: chunked\r\n\r\n' + size + b'\r\n' + follow)
        wire.half_close()
        if expected is None:
            assert await wire.recv_all() == b''
        else:
            assert (await wire.response()).status == expected
            assert await wire.closed()
        assert calls == []

        healthy = await server.wire()
        await healthy.send(b'GET /sentinel HTTP/1.1\r\nHost: test\r\n\r\n')
        response = await healthy.response()
        assert response.status == 200
        assert response.body == b'ok'
        assert calls == ['sentinel']
