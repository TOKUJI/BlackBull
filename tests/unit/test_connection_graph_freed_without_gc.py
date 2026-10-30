"""A finished connection is freed by reference counting, not the cyclic GC.

Under connection churn every connection's object graph (protocol, reader,
actors, senders, coroutine frames) becomes garbage once the peer goes away.
If a reference cycle keeps it alive, the cost moves to the cyclic collector,
which then runs every few hundred connections and walks all of it.  A peer
reset made that worse: the stored error's traceback pinned every frame it
unwound through.

Checked in a subprocess: the test harness itself (assertion rewriting, event
loop fixtures) holds frames, which would hide the property under test.
"""
import subprocess
import sys
import textwrap

import pytest

_PROBE = textwrap.dedent('''
    import asyncio, gc, socket, struct, sys, weakref
    from blackbull import BlackBull
    from blackbull.server import ASGIServer
    from blackbull.server import server as server_mod

    app = BlackBull()

    @app.route(path='/')
    async def root():
        return 'ok'

    seen = []
    _init = server_mod._ServedConnection.__init__
    def _tracking_init(self, *a, **kw):
        _init(self, *a, **kw)
        seen.append(weakref.ref(self))
    server_mod._ServedConnection.__init__ = _tracking_init

    async def one(port, reset):
        r, w = await asyncio.open_connection('127.0.0.1', port)
        w.write(b'GET / HTTP/1.1\\r\\nHost: x\\r\\n\\r\\n')
        await w.drain()
        await r.readuntil(b'ok')
        if reset:
            w.get_extra_info('socket').setsockopt(
                socket.SOL_SOCKET, socket.SO_LINGER, struct.pack('ii', 1, 0))
            w.transport.abort()
        else:
            w.close()
        await asyncio.sleep(0.1)

    async def main(reset):
        s = socket.socket(); s.bind(('127.0.0.1', 0))
        port = s.getsockname()[1]; s.close()
        task = asyncio.create_task(ASGIServer(app).run(port=port))
        for _ in range(100):
            try:
                await one(port, False)
                break
            except OSError:
                await asyncio.sleep(0.02)
        gc.collect(); seen.clear(); gc.disable()
        await one(port, reset)
        await asyncio.sleep(0.1)
        alive = sum(ref() is not None for ref in seen)
        print('accepted', len(seen), 'alive', alive)
        task.cancel()
        sys.exit(0 if seen and not alive else 1)

    asyncio.run(main(sys.argv[1] == 'rst'))
''')


@pytest.mark.parametrize('close', ['fin', 'rst'])
def test_finished_connection_is_freed_by_refcount(close):
    proc = subprocess.run([sys.executable, '-c', _PROBE, close],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, (
        'connection graph survived reference counting (a cycle keeps it '
        f'alive): {proc.stdout}{proc.stderr[-2000:]}')
