"""One process, several HTTP ports — the shape a multi-listener deployment needs.

BlackBull already serves HTTP/1.1, HTTP/2 and WebSocket from one listener, and
already binds extra ports with a per-port TLS choice for non-ASGI protocols.
What it could not do was put an *HTTP* listener on one of those extra ports, so
a deployment that must expose cleartext and TLS at once had to run one process
per port — and each of those processes carries the full worker count.

Measured before this existed, on HttpArena's async-db profile: BlackBull ran
4 x N worker processes to aiohttp's N, lost 20 % throughput between N=16 and
N=64 where aiohttp was flat, and held 2.5 GiB against aiohttp's 254 MiB.
"""
from __future__ import annotations

import asyncio
import ssl
from pathlib import Path

import pytest

from blackbull import BlackBull, Response
from blackbull.server.server import Server

pytestmark = pytest.mark.asyncio

CERT = str(Path(__file__).parent.parent / 'cert.pem')
KEY = str(Path(__file__).parent.parent / 'key.pem')


def _app():
    app = BlackBull()

    @app.route(path='/')
    async def index(conn):
        return Response(b'ok', content_type='text/plain')

    return app


async def _get(port: int, *, tls: bool) -> str:
    ctx = None
    if tls:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection('127.0.0.1', port, ssl=ctx), timeout=5)
    writer.write(b'GET / HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n')
    await writer.drain()
    data = await asyncio.wait_for(reader.read(-1), timeout=5)
    writer.close()
    return data.decode(errors='replace')


async def _serve(server, *ports):
    """Start the server and wait until every port actually answers.

    A fixed sleep is not enough under beartype instrumentation, where the whole
    suite runs slower — the readiness has to be observed, not assumed.
    """
    task = asyncio.ensure_future(server.run())
    for port in ports:
        for _ in range(200):
            if task.done():                      # surface a startup failure
                await task
            try:
                _r, w = await asyncio.wait_for(
                    asyncio.open_connection('127.0.0.1', port), timeout=0.5)
                w.close()
                break
            except (OSError, asyncio.TimeoutError):
                await asyncio.sleep(0.05)
        else:
            raise AssertionError(f'port {port} never accepted a connection')
    return task


async def _stop(task):
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def test_a_second_cleartext_http_port_serves_the_same_app():
    app = _app()
    app.add_http_port(18311)
    server = Server(app)
    server.open_socket(18310)
    task = await _serve(server, 18310, 18311)
    try:
        for port in (18310, 18311):
            assert '200 OK' in await _get(port, tls=False), port
    finally:
        await _stop(task)


async def test_cleartext_and_tls_http_ports_coexist_in_one_process():
    """The HttpArena shape: a TLS main listener plus cleartext and TLS extras.

    This is what forced one process per port — a deployment exposing both had
    no way to say so, and the per-port TLS choice existed only for non-ASGI
    bindings.
    """
    app = _app()
    app.add_http_port(18321)                 # cleartext, alongside a TLS main
    app.add_http_port(18322, tls=True)       # TLS, same certificate
    server = Server(app, certfile=CERT, keyfile=KEY)
    server.open_socket(18320)
    task = await _serve(server, 18320, 18321, 18322)
    try:
        assert '200 OK' in await _get(18320, tls=True), 'main TLS listener'
        assert '200 OK' in await _get(18321, tls=False), 'cleartext extra'
        assert '200 OK' in await _get(18322, tls=True), 'TLS extra'
    finally:
        await _stop(task)


async def test_a_raw_protocol_port_still_bypasses_http_detection():
    """The existing contract, unbroken: a binding *with* a handler owns its
    port outright and never sees HTTP parsing."""
    seen: list[bytes] = []

    app = _app()

    @app.raw_handler('echo', port=18331)
    async def echo(reader, writer, ctx):
        data = await reader.read(64)
        seen.append(data)
        await writer.write(b'echoed:' + data)

    server = Server(app)
    server.open_socket(18330)
    # Probe only the HTTP port: a readiness connection to the raw port would
    # be a connection the handler under test legitimately sees.
    task = await _serve(server, 18330)
    try:
        reader, writer = await asyncio.open_connection('127.0.0.1', 18331)
        writer.write(b'not-http')
        await writer.drain()
        got = await asyncio.wait_for(reader.read(64), timeout=5)
        writer.close()
    finally:
        await _stop(task)

    assert got == b'echoed:not-http', got
    assert seen == [b'not-http']


class _FakeBinding:
    def __init__(self, name, serves_http):
        self.name = name
        self.serves_http = serves_http


async def test_extra_http_ports_reach_every_worker_but_stateful_ones_do_not():
    """The assignment rule, which a single-process test cannot see.

    Port-bound listeners used to go to worker 0 alone, because the only kind
    that existed was a stateful broker that must have one owner.  An HTTP
    listener has no such constraint, and sending it to worker 0 only would
    leave the extra port answered by one process while the rest sat idle —
    exactly the shape this feature exists to remove.
    """
    from blackbull.server.multiworker import MultiWorkerServer

    http = (['sock-http'], _FakeBinding('http:8081', True))
    mqtt = (['sock-mqtt'], _FakeBinding('mqtt', False))

    pool = MultiWorkerServer(None, [], None, workers=3,
                             protocol_sockets=[http, mqtt])

    assert pool._http_sockets == [http]
    assert pool._protocol_sockets == [mqtt]

    def assigned(worker_id):
        got = list(pool._http_sockets)
        if worker_id == 0:
            got += pool._protocol_sockets
        return [b.name for _s, b in got]

    assert assigned(0) == ['http:8081', 'mqtt']
    assert assigned(1) == ['http:8081']
    assert assigned(2) == ['http:8081']
