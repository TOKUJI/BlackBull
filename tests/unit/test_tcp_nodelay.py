import asyncio
from contextlib import ExitStack
import json
import os
import socket

import pytest

from blackbull import BlackBull
from blackbull.server.connection_protocol import ConnectionProtocol
from blackbull.server.listener import InheritedFd, Listener, Tcp, Unix
from tests.harness import served


@pytest.mark.parametrize('loop', ['selector', 'uvloop'])
@pytest.mark.parametrize('host,origin', [
    ('127.0.0.1', 'new'), ('::1', 'new'),
    ('127.0.0.1', 'fd'), ('::1', 'fd'),
    ('127.0.0.1', 'reload'), ('::1', 'reload'),
    ('127.0.0.1', 'tls'), (None, 'unix'),
])
def test_accepted_socket_options(loop, host, origin, monkeypatch, tmp_path):
    factory = (asyncio.SelectorEventLoop if loop == 'selector'
               else pytest.importorskip('uvloop').new_event_loop)
    if host == '::1' and not socket.has_ipv6:
        pytest.skip('IPv6 unavailable')
    if origin == 'unix' and not hasattr(socket, 'AF_UNIX'):
        pytest.skip('UNIX sockets unavailable')
    if (origin in {'fd', 'reload'} and loop == 'selector'
            and not hasattr(socket, 'SO_PROTOCOL')):
        pytest.skip('Adopted socket protocol inference requires SO_PROTOCOL')

    observations = []
    connection_made = ConnectionProtocol.connection_made

    def observe(proto, transport):
        sock = transport.get_extra_info('socket')
        observations.append({
            'family': int(sock.family),
            'proto': sock.proto,
            'nodelay': (None if sock.family == socket.AF_UNIX else
                        sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY)),
        })
        connection_made(proto, transport)

    monkeypatch.setattr(ConnectionProtocol, 'connection_made', observe)
    monkeypatch.delenv('BB_INHERIT_FDS', raising=False)
    monkeypatch.delenv('LISTEN_PID', raising=False)
    monkeypatch.delenv('LISTEN_FDS', raising=False)
    app = BlackBull()

    @app.route(path='/')
    def options():
        return observations[-1]

    async def exchange():
        with ExitStack() as stack:
            if origin in {'fd', 'reload'}:
                family = socket.AF_INET6 if host == '::1' else socket.AF_INET
                source = stack.enter_context(socket.socket(family, socket.SOCK_STREAM))
                source.bind((host, 0))
                source.listen()
                fd = os.dup(source.fileno())
                if origin == 'reload':
                    monkeypatch.setenv('BB_INHERIT_FDS', str(fd))
                    listeners = None
                else:
                    listeners = [Listener(InheritedFd(fd))]
            elif origin == 'unix':
                listeners = [Listener(Unix(str(tmp_path / 'server.sock')))]
            else:
                listeners = [Listener(Tcp(0, host))]
            kwargs = {} if origin == 'tls' else {'listeners': listeners}
            async with served(app, tls=origin == 'tls', **kwargs) as server:
                if origin in {'fd', 'reload'}:
                    assert server.server.port == source.getsockname()[1]
                wire = await server.wire(host=host or '127.0.0.1')
                await wire.send(b'GET / HTTP/1.1\r\nHost: localhost\r\n\r\n')
                response = await wire.response()
                assert response.status == 200
                result = json.loads(response.body)
                print(json.dumps({'loop': loop, 'origin': origin, 'host': host, **result}))
                family = (socket.AF_UNIX if origin == 'unix' else
                          socket.AF_INET6 if host == '::1' else socket.AF_INET)
                assert result['family'] == family
                assert result['nodelay'] == (None if origin == 'unix' else 1)

    async def bounded():
        async with asyncio.timeout(15):
            await exchange()

    with asyncio.Runner(loop_factory=factory) as runner:
        runner.run(bounded())
