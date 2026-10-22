import asyncio
from contextlib import ExitStack
import json
import os
import socket
import sys
import uuid

from hpack import Decoder, Encoder
import pytest

from blackbull import BlackBull
from blackbull.server.listener import InheritedFd, Listener, Tcp, Unix
from tests.conformance.http2._harness import h2_connect, h2_section, h2_terminal
from tests.harness import served


@pytest.mark.parametrize('loop', ['selector', 'uvloop'])
@pytest.mark.parametrize('protocol', ['h10', 'h1', 'h2'])
@pytest.mark.parametrize('lane', ['native', 'asgi'])
@pytest.mark.parametrize('address', [
    'abstract', 'abstract-reload', 'abstract-nonutf8', 'abstract-peer',
    'unix', 'ipv4', 'ipv6',
])
def test_application_connection_addresses(loop, protocol, lane, address, monkeypatch, tmp_path):
    if address.startswith('abstract') and sys.platform != 'linux':
        pytest.skip('Abstract UNIX namespace requires Linux')
    if address == 'ipv6' and not socket.has_ipv6:
        pytest.skip('IPv6 unavailable')
    factory = (asyncio.SelectorEventLoop if loop == 'selector'
               else pytest.importorskip('uvloop').new_event_loop)
    monkeypatch.setenv('BB_FORCE_ASGI_SCOPE', '1' if lane == 'asgi' else '0')
    for name in ('BB_INHERIT_FDS', 'LISTEN_PID', 'LISTEN_FDS'):
        monkeypatch.delenv(name, raising=False)
    app = BlackBull()

    @app.route(path='/')
    def addresses(conn):
        return {'server': conn.server, 'client': conn.client}

    async def asgi(scope, receive, send):
        body = json.dumps({'server': scope['server'], 'client': scope['client']}).encode()
        await send({'type': 'http.response.start', 'status': 200,
                    'headers': [(b'content-length', str(len(body)).encode())]})
        await send({'type': 'http.response.body', 'body': body})

    async def exchange():
        with ExitStack() as stack:
            host = '::1' if address == 'ipv6' else '127.0.0.1'
            local_path = None
            if address.startswith('abstract'):
                name = ('bb-' + uuid.uuid4().hex).encode()
                if address == 'abstract-nonutf8':
                    name += b'\xff'
                source = stack.enter_context(socket.socket(socket.AF_UNIX, socket.SOCK_STREAM))
                source.bind(b'\0' + name)
                source.listen()
                fd = os.dup(source.fileno())
                if address == 'abstract-reload':
                    monkeypatch.setenv('BB_INHERIT_FDS', str(fd))
                    listeners = None
                else:
                    listeners = [Listener(InheritedFd(fd))]
                expected_server = ['@' + name.decode('utf-8', 'backslashreplace'), None]
                if address == 'abstract-peer':
                    local_path = b'\0peer-' + name + b'\xff'
            elif address == 'unix':
                path = str(tmp_path / 'server.sock')
                listeners = [Listener(Unix(path))]
                expected_server = [path, None]
            else:
                listeners = [Listener(Tcp(0, host))]
                expected_server = None
            async with served(app if lane == 'native' else asgi, listeners=listeners) as server:
                if address.startswith('abstract'):
                    assert server.server.unix_path == os.fsdecode(b'\0' + name)
                    assert server.server.port is None
                if expected_server is None:
                    expected_server = [host, server.server.port]
                if protocol != 'h2':
                    wire = await server.wire(host=host, local_path=local_path)
                    request = (b'GET / HTTP/1.0\r\n\r\n' if protocol == 'h10' else
                               b'GET / HTTP/1.1\r\nHost: localhost\r\n\r\n')
                    await wire.send(request)
                    response = await wire.response()
                    assert response.status == 200
                    body = response.body
                else:
                    wire = await h2_connect(server, host=host, local_path=local_path)
                    block = Encoder().encode([
                        (b':method', b'GET'), (b':scheme', b'http'),
                        (b':authority', b'localhost'), (b':path', b'/'),
                    ])
                    await wire.send(h2_section(block))
                    fields, body = await h2_terminal(wire, 1, Decoder())
                    assert (b':status', b'200') in fields
                result = json.loads(body)
                assert result['server'] == (['localhost', 80] if protocol == 'h1'
                                            else expected_server)
                if address in ('ipv4', 'ipv6'):
                    assert result['client'] == list(wire.writer.get_extra_info('sockname')[:2])
                else:
                    expected_client = (['@' + local_path[1:].decode('utf-8', 'backslashreplace'), None]
                                       if local_path is not None else ['', None])
                    assert result['client'] == expected_client

    async def bounded():
        async with asyncio.timeout(15):
            await exchange()

    with asyncio.Runner(loop_factory=factory) as runner:
        runner.run(bounded())
