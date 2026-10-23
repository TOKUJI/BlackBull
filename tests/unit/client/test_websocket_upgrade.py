import asyncio
from base64 import b64encode
from contextlib import asynccontextmanager
from hashlib import sha1
from types import SimpleNamespace

import pytest

from blackbull.client import HandshakeError, WebSocketClient
from tests.harness import Wire, peer


@asynccontextmanager
async def _origin(changes=(), *, status=b'HTTP/1.1 101 Switching Protocols',
                  respond=True):
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
            await wire.send(status + b'\r\n' + b''.join(
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
@pytest.mark.parametrize('cancel,respond', [(False, False), (True, False), (False, True)])
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
