"""A finished connection is freed by reference counting, not by the cyclic GC.

Checked in a subprocess with the GC disabled: the test harness's own frames
would otherwise keep the objects under test alive.
"""
import asyncio
import socket
import struct
import subprocess
import sys
import textwrap

import pytest
from hpack import Encoder

from blackbull import BlackBull
from tests.conformance.http2._harness import h2_connect, h2_section
from tests.harness import read_h2_frame, served

_PROBE = textwrap.dedent('''
    import asyncio, gc, logging, socket, struct, sys
    from hpack import Encoder
    from blackbull import BlackBull
    from blackbull.utils import Scheme
    from blackbull.server import Server
    from blackbull.server.listener import Listener, Tcp

    protocol, close, when = sys.argv[1:4]
    logging.disable(logging.CRITICAL)
    WINDOW = (1 << 31) - 1
    app = BlackBull()

    @app.route(path='/')
    async def root():
        return b'x' * (4 << 20) if when == 'during' else b'ok'

    @app.route(path='/ws', scheme=Scheme.websocket)
    async def echo(conn, receive, send):
        await receive()
        await send({'type': 'websocket.accept'})
        event = await receive()
        await send({'type': 'websocket.send', 'text': event['text']})

    def frame(kind, flags, sid, payload=b''):
        return (len(payload).to_bytes(3, 'big') + bytes([kind, flags])
                + sid.to_bytes(4, 'big') + payload)

    async def one(port):
        sock = socket.socket()
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 65536)
        sock.setblocking(False)
        await asyncio.get_running_loop().sock_connect(sock, ('127.0.0.1', port))
        reader, writer = await asyncio.open_connection(sock=sock)
        if protocol == 'http1':
            writer.write(b'GET / HTTP/1.1\\r\\nHost: x\\r\\n\\r\\n')
        elif protocol == 'ws':
            writer.write(b'GET /ws HTTP/1.1\\r\\nHost: x\\r\\nUpgrade: websocket\\r\\n'
                         b'Connection: Upgrade\\r\\nSec-WebSocket-Version: 13\\r\\n'
                         b'Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\\r\\n\\r\\n')
            await reader.readuntil(b'\\r\\n\\r\\n')
            mask = b'\\x01\\x02\\x03\\x04'
            writer.write(b'\\x81\\x82' + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(b'ok')))
        else:
            settings = (4).to_bytes(2, 'big') + WINDOW.to_bytes(4, 'big')
            block = Encoder().encode([(b':method', b'GET'), (b':path', b'/'),
                                      (b':scheme', b'http'), (b':authority', b'x')])
            writer.write(b'PRI * HTTP/2.0\\r\\n\\r\\nSM\\r\\n\\r\\n' + frame(4, 0, 0, settings)
                         + frame(8, 0, 0, (WINDOW - 65535).to_bytes(4, 'big'))
                         + frame(1, 5, 1, block))
        data = b''
        while len(data) < 30000 if when == 'during' else b'ok' not in data:
            data += await reader.read(65536)
        if when == 'after':
            await asyncio.sleep(0.05)
        if close == 'rst':
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack('ii', 1, 0))
            writer.transport.abort()
        else:
            writer.close()
        await asyncio.sleep(0.3)

    async def main():
        server = Server(app, listeners=[Listener(Tcp(0, '127.0.0.1'))])
        server.open_socket()
        running = asyncio.create_task(server.run())
        await one(server.port)
        gc.collect()
        gc.disable()
        gc.set_debug(gc.DEBUG_SAVEALL)
        await one(server.port)
        gc.collect()
        left = sorted({
            type(o).__qualname__ if type(o).__module__.startswith('blackbull')
            else f'frame {o.f_code.co_qualname}'
            for o in gc.garbage
            if type(o).__module__.startswith('blackbull')
            or (type(o).__name__ == 'frame' and '/blackbull/' in o.f_code.co_filename)})
        gc.set_debug(0)
        gc.garbage.clear()
        gc.enable()
        await server.stop(drain_timeout=1.0)
        await running
        print(left)
        sys.exit(1 if left else 0)

    asyncio.run(main())
''')


@pytest.mark.parametrize('protocol,when', [
    ('http1', 'after'), ('http1', 'during'), ('http2', 'after'), ('http2', 'during'),
    ('ws', 'after')])
@pytest.mark.parametrize('close', ['fin', 'rst'])
def test_a_finished_connection_is_freed_by_refcount(protocol, close, when):
    proc = subprocess.run([sys.executable, '-c', _PROBE, protocol, close, when],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, (
        f'left for the cyclic GC: {proc.stdout.strip()} {proc.stderr[-2000:]}')


@pytest.mark.asyncio
@pytest.mark.parametrize('protocol', ['http1', 'http2'])
async def test_an_error_observer_still_gets_the_reset_with_its_traceback(protocol):
    app = BlackBull()
    seen = []

    @app.route(path='/')
    async def root():
        return b'ok'

    @app.on('error')
    async def observer(event):
        await asyncio.sleep(0.05)       # runs after the connection has ended
        exc = event.detail['exception']
        leaves = exc.exceptions if isinstance(exc, ExceptionGroup) else (exc,)
        seen.extend((type(e).__name__, e.__traceback__ is not None) for e in leaves)

    async with served(app) as server:
        if protocol == 'http1':
            wire = await server.wire()
            await wire.send(b'GET / HTTP/1.1\r\nHost: x\r\n\r\n')
            await wire.response()
        else:
            wire = await h2_connect(server)
            await wire.send(h2_section(Encoder().encode(
                [(b':method', b'GET'), (b':path', b'/'), (b':scheme', b'http'),
                 (b':authority', b'x')])))
            while not ((frame := await read_h2_frame(wire.reader))[2] == 1
                       and frame[0] in (0, 1) and frame[1] & 1):
                pass
        wire.writer.get_extra_info('socket').setsockopt(
            socket.SOL_SOCKET, socket.SO_LINGER, struct.pack('ii', 1, 0))
        wire.writer.transport.abort()
        await asyncio.sleep(0.3)
    assert seen == [('ConnectionResetError', True)]
