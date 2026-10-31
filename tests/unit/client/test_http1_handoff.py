import asyncio

import pytest

from blackbull.client import HTTP1Client, ProtocolError, ConnectionError
from tests.harness import Wire, peer


@pytest.mark.asyncio
@pytest.mark.parametrize('api', ['request', 'stream', 'read_response'])
@pytest.mark.parametrize('status', [200, 204, 299])
@pytest.mark.parametrize('version,connection', [
    (b'HTTP/1.0', b''),
    (b'HTTP/1.0', b'Connection: close\r\n'),
    (b'HTTP/1.1', b'Connection: close\r\n'),
])
async def test_connect_transfers_nonpersistent_transport(api, status, version, connection):
    closed = asyncio.Event()

    async def handle(reader, writer):
        wire = Wire(reader, writer)
        await wire.recv_until()
        await wire.send(version + f' {status} Connected\r\n'.encode() + connection
                        + b'Content-Length: 999\r\nTransfer-Encoding: chunked\r\n'
                        + b'\r\nprefetched')
        assert await wire.recv_exactly(4) == b'ping'
        await wire.send(b'pong')
        assert await wire.recv_all() == b''
        closed.set()

    async with peer(handle) as port:
        async with HTTP1Client('127.0.0.1', port) as client:
            headers = [(b'connection', b'close')]
            if api == 'request':
                assert (await client.request('CONNECT', 'origin:443', headers=headers)).status == status
            elif api == 'stream':
                assert [data async for data in client.stream(
                    'CONNECT', 'origin:443', headers=headers)] == []
            else:
                await client.send_raw(b'CONNECT origin:443 HTTP/1.1\r\nHost: origin\r\n\r\n')
                assert (await client.read_response(request_method='CONNECT')).status == status
            session = client.handoff()
            with pytest.raises(ConnectionError):
                client.handoff()
        async with session:
            assert await session.read(10) == b'prefetched'
            await session.write(b'ping')
            assert await session.read(4) == b'pong'
        await asyncio.wait_for(closed.wait(), 1)


@pytest.mark.asyncio
@pytest.mark.parametrize('api', ['request', 'stream'])
@pytest.mark.parametrize('response', [
    b'HTTP/1.0 101 Switching\r\nUpgrade: example\r\nConnection: upgrade\r\n\r\n',
    b'HTTP/1.1 101 Switching\r\nConnection: upgrade\r\n\r\n',
    b'HTTP/1.1 101 Switching\r\nUpgrade: other\r\nConnection: upgrade\r\n\r\n',
    b'HTTP/1.1 101 Switching\r\nUpgrade: example\r\n\r\n',
])
async def test_unnegotiated_upgrade_closes_connection(api, response):
    closed = asyncio.Event()

    async def handle(reader, writer):
        wire = Wire(reader, writer)
        await wire.recv_until()
        await wire.send(response)
        assert await wire.recv_all() == b''
        closed.set()

    async with peer(handle) as port:
        async with HTTP1Client('127.0.0.1', port) as client:
            headers = [(b'connection', b'upgrade'), (b'upgrade', b'example')]
            with pytest.raises(ProtocolError):
                if api == 'request':
                    await client.request('GET', '/', headers=headers)
                else:
                    _ = [data async for data in client.stream('GET', '/', headers=headers)]
            await asyncio.wait_for(closed.wait(), 1)
            with pytest.raises(ConnectionError):
                client.handoff()


@pytest.mark.asyncio
@pytest.mark.parametrize('offered,selected,accepted', [
    (b'example', b'EXAMPLE', True),
    (b'example/V1', b'Example/V1', True),
    (b'example/V1', b'example/v1', False),
    (b'example', b'example/1', False),
    (b'example, layer/1', b'layer/1, example', True),
    (b'example', b'example/', False),
    (b'example', b'example/1/2', False),
    (b'', b'example', False),
])
@pytest.mark.parametrize('api', ['request', 'read_response'])
async def test_upgrade_protocol_selection_and_handoff(api, offered, selected, accepted):
    closed = asyncio.Event()

    async def handle(reader, writer):
        wire = Wire(reader, writer)
        await wire.recv_until()
        await wire.send(b'HTTP/1.1 101 Switching Protocols\r\nConnection: upgrade\r\n'
                        + b'Upgrade: ' + selected + b'\r\n\r\nhello')
        if accepted:
            assert await wire.recv_exactly(5) == b'world'
        assert await wire.recv_all() == b''
        closed.set()

    async with peer(handle) as port:
        async with HTTP1Client('127.0.0.1', port) as client:
            headers = [(b'upgrade', offered), (b'connection', b'upgrade')]
            if api == 'request':
                response = client.request('GET', '/', headers=headers)
            else:
                await client.send_raw(b'GET / HTTP/1.1\r\nHost: origin\r\n'
                                      + b'Connection: upgrade\r\nUpgrade: '
                                      + offered + b'\r\n\r\n')
                response = client.read_response(request_method='GET', request_headers=headers)
            if accepted:
                assert (await response).status == 101
                session = client.handoff()
            else:
                with pytest.raises(ProtocolError):
                    await response
        if accepted:
            async with session:
                assert await session.read(5) == b'hello'
                await session.write(b'world')
        await asyncio.wait_for(closed.wait(), 1)
