import asyncio
from base64 import b64encode
from contextlib import asynccontextmanager
from hashlib import sha1
from types import SimpleNamespace

import pytest

from blackbull.client import HandshakeError, WebSocketClient
from blackbull.server.sender import AsyncioWriter
from tests.harness import Wire, peer


@asynccontextmanager
async def _origin(changes=(), *, status=b'HTTP/1.1 101 Switching Protocols',
                  respond=True, interim=b''):
    observed = SimpleNamespace(request=asyncio.Event(), closed=asyncio.Event(), data=None)

    async def handle(reader, writer):
        wire = Wire(reader, writer)
        request = await wire.recv_until()
        key = next(line.split(b':', 1)[1].strip() for line in request.split(b'\r\n')
                   if line.lower().startswith(b'sec-websocket-key:'))
        fields = {
            b'Upgrade': [b'websocket'],
            b'Connection': [b'Upgrade'],
            b'Sec-WebSocket-Accept': [b64encode(sha1(
                key + b'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').digest())],
        }
        for name, values in dict(changes).items():
            fields[name] = fields[name] * 2 if values is None else values
        observed.request.set()
        if respond:
            await wire.send(interim + status + b'\r\n' + b''.join(
                name + b': ' + value + b'\r\n'
                for name, values in fields.items() for value in values
            ) + b'\r\n\x81\x02ok\x88\x02\x03\xe8')
        observed.data = await wire.recv_all()
        observed.closed.set()

    async with peer(handle) as port:
        observed.port = port
        yield observed


@pytest.mark.asyncio
@pytest.mark.parametrize('changes,offered,chosen', [
    ({}, (), None),
    ({b'Upgrade': [b' \tWeBsOcKeT\t '],
      b'Connection': [b'keep-alive', b' \tUpGrAdE\t ']}, (), None),
    ({b'Connection': [b'keep-alive, upgrade']}, (), None),
    ({b'Sec-WebSocket-Protocol': [b'chat']}, (b'chat', b'other'), b'chat'),
    ({b'Sec-WebSocket-Protocol': [b' \tchat\t ']}, (b'chat',), b'chat'),
    ({}, (b'chat',), None),
    ({b'Content-Length': [b'0']}, (), None),
])
async def test_valid_upgrade_preserves_negotiation_and_buffered_frames(changes, offered, chosen):
    async with _origin(changes) as origin:
        async with WebSocketClient('127.0.0.1', origin.port) as client:
            session = await client.connect('/ws', subprotocols=iter(offered))
            assert session.subprotocol == chosen
            event = await session.receive()
            assert event['type'] == 'websocket.receive' and event['text'] == 'ok'
            assert (await session.receive())['type'] == 'websocket.disconnect'
        await asyncio.wait_for(origin.closed.wait(), 1)


@pytest.mark.asyncio
async def test_session_owns_transport_after_upgrade():
    async with _origin() as origin:
        async with WebSocketClient('127.0.0.1', origin.port) as client:
            session = await client.connect('/ws')
            with pytest.raises(HandshakeError):
                await client.connect('/second-upgrade')
            assert (await session.receive())['text'] == 'ok'
            await session.close()
            await session.close()
            await asyncio.wait_for(origin.closed.wait(), 1)
            assert b'GET /second-upgrade' not in origin.data


@pytest.mark.asyncio
async def test_cancelled_close_write_releases_transport(monkeypatch):
    writing = asyncio.Event()
    original = AsyncioWriter.write

    async def blocked_close(self, data):
        if data[0] == 0x88:
            writing.set()
            await asyncio.Future()
        await original(self, data)

    monkeypatch.setattr(AsyncioWriter, 'write', blocked_close)
    async with _origin() as origin:
        async with WebSocketClient('127.0.0.1', origin.port) as client:
            session = await client.connect('/ws')
            closing = asyncio.create_task(session.close())
            await asyncio.wait_for(writing.wait(), 1)
            closing.cancel()
            with pytest.raises(asyncio.CancelledError):
                await closing
            await asyncio.wait_for(origin.closed.wait(), 1)
            await session.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('changes', [
    {b'Upgrade': []},
    {b'Upgrade': [b'']},
    {b'Upgrade': [b'h2c']},
    {b'Upgrade': [b'websocket, h2c']},
    {b'Upgrade': [b'websocket', b'websocket']},
    {b'Connection': []},
    {b'Connection': [b'']},
    {b'Connection': [b'close']},
    {b'Connection': [b'upgrade;']},
    {b'Connection': [b'upgrade, bad option']},
    {b'Sec-WebSocket-Accept': []},
    {b'Sec-WebSocket-Accept': [b'wrong']},
    {b'Sec-WebSocket-Accept': [b'wrong', b'wrong']},
    {b'Sec-WebSocket-Accept': None},
    {b'Sec-WebSocket-Protocol': [b'chat']},
    {b'Sec-WebSocket-Protocol': [b'']},
    {b'Sec-WebSocket-Protocol': [b'chat, other']},
    {b'Sec-WebSocket-Protocol': [b'chat', b'chat']},
    {b'Sec-WebSocket-Extensions': [b'permessage-deflate']},
    {b'Sec-WebSocket-Extensions': [b'']},
    {b'Sec-WebSocket-Extensions': [b'permessage-deflate', b'permessage-deflate']},
    {b'Content-Length': [b'5']},
    {b'Transfer-Encoding': [b'chunked']},
    {b'Content-Length': [b'invalid']},
    {b'Content-Length': [b'0', b'5']},
])
async def test_invalid_upgrade_closes_before_returning_to_caller(changes):
    async with _origin(changes) as origin:
        async with WebSocketClient('127.0.0.1', origin.port) as client:
            with pytest.raises(HandshakeError):
                await client.connect('/ws')
            await asyncio.wait_for(origin.closed.wait(), 1)
            with pytest.raises(HandshakeError):
                await client.connect('/ws')
            assert origin.data == b''


@pytest.mark.asyncio
@pytest.mark.parametrize('selected', [
    [b'Chat'], [b'other'], [b'chat, other'], [b'chat/other'], [b'chat', b'chat'],
])
async def test_selected_subprotocol_must_be_one_offered_token(selected):
    async with _origin({b'Sec-WebSocket-Protocol': selected}) as origin:
        async with WebSocketClient('127.0.0.1', origin.port) as client:
            with pytest.raises(HandshakeError):
                await client.connect('/ws', subprotocols=[b'chat'])


@pytest.mark.asyncio
@pytest.mark.parametrize('status,changes', [
    (b'HTTP/1.0 101 Switching Protocols', {}),
    (b'HTTP/1.1 403 Forbidden', {b'Content-Length': [b'0']}),
])
async def test_rejected_status_or_version_retires_connection(status, changes):
    async with _origin(changes, status=status) as origin:
        async with WebSocketClient('127.0.0.1', origin.port) as client:
            with pytest.raises(HandshakeError):
                await client.connect('/ws')
            await asyncio.wait_for(origin.closed.wait(), 1)


@pytest.mark.asyncio
async def test_rejection_does_not_wait_for_http_body():
    async with _origin({b'Content-Length': [b'100000']},
                       status=b'HTTP/1.1 403 Forbidden') as origin:
        async with WebSocketClient('127.0.0.1', origin.port) as client:
            with pytest.raises(HandshakeError):
                await asyncio.wait_for(client.connect('/ws', response_timeout=None), 1)
            await asyncio.wait_for(origin.closed.wait(), 1)


@pytest.mark.asyncio
@pytest.mark.parametrize('fields,accepted', [(b'', True),
    (b'Content-Length: 1\r\n', False), (b'Transfer-Encoding: chunked\r\n', False)])
async def test_interim_head_before_upgrade(fields, accepted):
    async with _origin(interim=b'HTTP/1.1 103 Early Hints\r\n' + fields + b'\r\n') as origin:
        async with WebSocketClient('127.0.0.1', origin.port) as client:
            if accepted:
                session = await client.connect('/ws')
                assert (await session.receive())['text'] == 'ok'
                assert (await session.receive())['type'] == 'websocket.disconnect'
            else:
                with pytest.raises(HandshakeError):
                    await client.connect('/ws')


@pytest.mark.asyncio
@pytest.mark.parametrize('cancel,respond', [(False, False), (True, False)])
async def test_interrupted_handshake_retires_connection(cancel, respond):
    async with _origin(status=b'HTTP/1.1 403 Forbidden', respond=respond) as origin:
        async with WebSocketClient('127.0.0.1', origin.port) as client:
            task = asyncio.create_task(client.connect('/ws', response_timeout=.1))
            await asyncio.wait_for(origin.request.wait(), 1)
            if cancel:
                task.cancel()
            with pytest.raises(asyncio.CancelledError if cancel else TimeoutError):
                await task
            await asyncio.wait_for(origin.closed.wait(), 1)
            with pytest.raises(HandshakeError):
                await client.connect('/ws')
            assert origin.data == b''
