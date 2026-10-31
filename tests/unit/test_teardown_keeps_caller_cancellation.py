"""A teardown that waits for work it cancelled itself must not swallow a
cancellation aimed at its own caller.

Each case holds the teardown inside that wait, cancels the caller, and expects
``CancelledError`` out of it; the control is the same teardown left alone,
which returns normally.
"""
import asyncio
import os

import pytest

from blackbull import BlackBull
from blackbull.client import HTTP2Client
from blackbull.client.exceptions import ConnectionError
from blackbull.mqtt import MQTTExtension
from blackbull.server.recipient import RecipientFactory
from blackbull.server.sender import AbstractWriter
from blackbull.testing import NativeTestServer
from blackbull.testing.grpc import GrpcTestServer
from tests.harness import paced, served

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(30)]


async def _cancelled_inside(teardown, settle: float):
    """Run *teardown*, cancel it after *settle* seconds, and return what it ended with."""
    task = asyncio.create_task(teardown)
    await asyncio.sleep(settle)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        return 'cancelled'
    return 'returned'


def _ping_app():
    app = BlackBull()

    @app.route(path='/')
    async def root():
        return b'ok'
    return app


@pytest.mark.parametrize('cancel', [True, False], ids=['cancelled', 'control'])
async def test_http2_client_exit(cancel):
    entered, hold = asyncio.Event(), asyncio.Event()
    app = BlackBull()

    @app.route(path='/')
    async def root():
        entered.set()
        await hold.wait()
        return b'ok'

    async with served(app) as server:
        before = len(os.listdir('/dev/fd'))
        client = HTTP2Client('127.0.0.1', server.server.port)
        await client.__aenter__()
        in_flight = asyncio.create_task(client.request('GET', '/'))
        await entered.wait()
        try:
            exit_ = client.__aexit__(None, None, None)
            if cancel:
                # One step reaches the wait for the receive loop it just cancelled.
                assert await _cancelled_inside(exit_, 0) == 'cancelled'
            else:
                await exit_
            # Teardown finished either way: the request in flight is failed and
            # the connection closed, on both ends.
            with pytest.raises(ConnectionError):
                await asyncio.wait_for(in_flight, 5)
        finally:
            hold.set()
        await asyncio.sleep(0.3)
        assert len(os.listdir('/dev/fd')) == before


@pytest.mark.parametrize('cancel', [True, False], ids=['cancelled', 'control'])
async def test_native_test_server_exit(cancel):
    entered, hold = asyncio.Event(), asyncio.Event()
    app = BlackBull()

    @app.route(path='/')
    async def root():
        entered.set()
        await hold.wait()
        return b'ok'

    server = NativeTestServer(app, timeout=5.0)
    await server.__aenter__()
    _reader, writer = await asyncio.open_connection('127.0.0.1', server.port)
    writer.write(b'GET / HTTP/1.1\r\nHost: x\r\n\r\n')
    await entered.wait()
    try:
        if not cancel:
            hold.set()
            await server.__aexit__(None, None, None)
            return
        # The drain waits on the request held open by the handler.
        assert await _cancelled_inside(server.__aexit__(None, None, None), 0.2) == 'cancelled'
    finally:
        hold.set()
        writer.close()


async def _slow_shutdown_app(scope, receive, send):
    if scope['type'] != 'lifespan':
        return
    assert (await receive())['type'] == 'lifespan.startup'
    await send({'type': 'lifespan.startup.complete'})
    assert (await receive())['type'] == 'lifespan.shutdown'
    # A shutdown that finishes its work even when cancelled, as a drain does.
    loop = asyncio.get_running_loop()
    end = loop.time() + 2.0
    while (left := end - loop.time()) > 0:
        try:
            await asyncio.sleep(left)
        except asyncio.CancelledError:
            pass
    await send({'type': 'lifespan.shutdown.complete'})


@pytest.mark.parametrize('cancel', [True, False], ids=['cancelled', 'control'])
async def test_grpc_test_server_exit(cancel):
    server = GrpcTestServer(_slow_shutdown_app)
    await server.__aenter__()
    if not cancel:
        await server.__aexit__(None, None, None)
        return
    # The serve task it cancelled is still in the application's shutdown; the
    # caller leaves at once rather than waiting the shutdown out.
    started = asyncio.get_running_loop().time()
    assert await _cancelled_inside(server.__aexit__(None, None, None), 0.2) == 'cancelled'
    assert asyncio.get_running_loop().time() - started < 1.0


class _Sink(AbstractWriter):
    async def write(self, data) -> None:
        pass

    async def writelines(self, parts) -> None:
        pass

    async def drain(self) -> None:
        pass


@pytest.mark.parametrize('cancel', [True, False], ids=['cancelled', 'control'])
async def test_websocket_recipient_shutdown(cancel):
    # A positive queue depth starts the read-ahead task, which shutdown cancels and waits for.
    recipient = RecipientFactory.websocket(paced([(30.0, b'')]), _Sink(), ws_queue_depth=4)
    assert (await recipient())['type'] == 'websocket.connect'
    reading = asyncio.create_task(recipient())
    await asyncio.sleep(0.05)
    try:
        if not cancel:
            await recipient.shutdown()
            return
        assert await _cancelled_inside(recipient.shutdown(), 0) == 'cancelled'
    finally:
        reading.cancel()
        await asyncio.gather(reading, return_exceptions=True)


@pytest.mark.parametrize('cancel', [True, False], ids=['cancelled', 'control'])
async def test_mqtt_extension_shutdown(cancel):
    extension = MQTTExtension(port=0)
    await extension.startup(None)
    if not cancel:
        await extension.shutdown(None)
        return
    # One step reaches the wait for the broker and tap tasks it just cancelled.
    assert await _cancelled_inside(extension.shutdown(None), 0) == 'cancelled'
