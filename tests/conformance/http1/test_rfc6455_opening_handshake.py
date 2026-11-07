"""RFC 6455 §4.2.1: which HTTP/1.1 opening handshakes switch protocols."""
import pytest

from blackbull.client.http1 import HTTP1Client
from blackbull.fault_injection import H1CSendRawBytes, ReadResponse, Scenario

from tests.harness import served

pytestmark = pytest.mark.asyncio

KEY = b'Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ=='
VERSION = b'Sec-WebSocket-Version: 13'


def _handshake(request_line=b'GET /ws HTTP/1.1', connection=b'Connection: Upgrade',
               fields=(KEY, VERSION)):
    lines = [request_line, b'Host: x', b'Upgrade: websocket',
             *([connection] if connection else []), *fields]
    return b'\r\n'.join(lines) + b'\r\n\r\n'


REFUSED = {
    'post': _handshake(b'POST /ws HTTP/1.1'),
    'put': _handshake(b'PUT /ws HTTP/1.1'),
    'head': _handshake(b'HEAD /ws HTTP/1.1'),
    'lowercase-get': _handshake(b'get /ws HTTP/1.1'),
    'http-1.0': _handshake(b'GET /ws HTTP/1.0'),
    'no-connection': _handshake(connection=None),
    'connection-keep-alive': _handshake(connection=b'Connection: keep-alive'),
    'duplicate-key': _handshake(fields=(KEY, b'Sec-WebSocket-Key: AAAAAAAAAAAAAAAAAAAAAA==',
                                        VERSION)),
    'duplicate-version': _handshake(fields=(KEY, VERSION, b'Sec-WebSocket-Version: 8')),
}


async def _status(raw: bytes) -> tuple[int | None, list]:
    called = []

    async def app(conn, receive, send):
        called.append(conn)
        await receive()
        await send({'type': 'websocket.accept'})

    async with served(app) as s:
        async with HTTP1Client('127.0.0.1', s.server.port, connect_timeout=2.0) as client:
            result = await client.execute_scenario(Scenario(steps=(
                H1CSendRawBytes(raw), ReadResponse())))
    return (result.response and result.response.status), called


@pytest.mark.parametrize('raw', REFUSED.values(), ids=REFUSED.keys())
async def test_a_nonconforming_handshake_is_refused_before_101(raw):
    status, called = await _status(raw)
    assert (status, called) == (400, [])


@pytest.mark.parametrize('connection', [b'Connection: Upgrade',
                                        b'Connection: keep-alive, upgrade'])
async def test_a_conforming_handshake_switches_protocols(connection):
    status, called = await _status(_handshake(connection=connection))
    assert (status, len(called)) == (101, 1)
