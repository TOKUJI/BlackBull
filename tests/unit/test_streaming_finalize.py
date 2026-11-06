"""A streamed response whose send fails, or whose peer goes, closes its
generator (BLA-641, BLA-630)."""
import asyncio
import contextlib
import logging

import pytest

from blackbull import BlackBull
from blackbull.response import EventSourceResponse, StreamingResponse


def _tracked():
    state = {'produced': 0, 'finalized': 0}

    async def gen():
        try:
            for _ in range(200):
                state['produced'] += 1
                yield b'x' * 16384
        finally:
            state['finalized'] += 1
    return state, gen


@pytest.mark.asyncio
@pytest.mark.parametrize('wrap', [StreamingResponse, EventSourceResponse])
@pytest.mark.parametrize('failure', [ConnectionResetError, asyncio.CancelledError])
async def test_a_failing_send_finalizes_the_generator(wrap, failure):
    state, gen = _tracked()
    sent = 0

    async def send(message):
        nonlocal sent
        sent += 1
        if sent == 3:
            raise failure()

    with pytest.raises(failure):
        await wrap(gen())(None, None, send)
    assert state == {'produced': 2, 'finalized': 1}


@pytest.mark.asyncio
async def test_a_failing_cleanup_does_not_mask_the_send_failure(caplog):
    async def gen():
        try:
            yield b'x'
            yield b'y'
        finally:
            raise RuntimeError('cleanup failed')

    async def send(message):
        if message.body == b'x':
            raise ConnectionResetError()

    with pytest.raises(ConnectionResetError):
        await StreamingResponse(gen())(None, None, send)
    assert 'cleanup failed' in caplog.text


@pytest.mark.asyncio
async def test_an_iterator_with_a_sync_aclose_still_completes():
    class Chunks:
        def __init__(self):
            self._left = [b'a', b'b']

        def __aiter__(self):
            return self

        async def __anext__(self):
            if not self._left:
                raise StopAsyncIteration
            return self._left.pop(0)

        def aclose(self):
            pass

    bodies = []

    async def send(message):
        bodies.append(message.body)

    await StreamingResponse(Chunks())(None, None, send)
    assert bodies[1:] == [b'a', b'b', b'']


@pytest.mark.asyncio
async def test_h2_flow_control_stall_resets_and_finalizes(monkeypatch):
    from blackbull.client.http2 import HTTP2Client
    from blackbull.env import reset_settings_cache
    from blackbull.fault_injection.scenario_h2_client import (
        ScenarioH2Client, SendFrame, SendHeaders, SendPreface, WaitForServerFrame,
    )
    from blackbull.protocol.frame_types import FrameTypes
    from blackbull.testing.native import NativeTestServer

    monkeypatch.setenv('BB_WRITE_TIMEOUT', '0.3')
    reset_settings_cache()
    state, gen = _tracked()
    app = BlackBull()

    @app.route(path='/stream')
    async def stream(conn):
        return StreamingResponse(gen(), media_type='application/octet-stream')

    try:
        async with NativeTestServer(app) as server:
            async with HTTP2Client('127.0.0.1', server.port, scenario_mode=True) as client:
                result = await asyncio.wait_for(client.execute_scenario(ScenarioH2Client(steps=(
                    SendPreface(),
                    # SETTINGS_INITIAL_WINDOW_SIZE = 1024, and never a WINDOW_UPDATE.
                    SendFrame(FrameTypes.SETTINGS, data=b'\x00\x04' + (1024).to_bytes(4, 'big')),
                    SendHeaders(pseudo=((':method', 'GET'), (':scheme', 'http'),
                                        (':authority', 'x'), (':path', '/stream')),
                                end_stream=True),
                    WaitForServerFrame(match={'type': 'RST_STREAM', 'error_code': 8},
                                       timeout=5.0)))), timeout=15.0)
                assert result.response is not None, 'no RST_STREAM(CANCEL) observed'
                for _ in range(100):
                    if state['finalized']:
                        break
                    await asyncio.sleep(0.02)
                # Checked while the server runs: stopping it finalizes anyway.
                assert state['finalized'] == 1
    finally:
        monkeypatch.delenv('BB_WRITE_TIMEOUT')
        reset_settings_cache()


def _slow_stream_app(state):
    app = BlackBull()

    @app.route(path='/long')
    async def long(conn):
        async def gen():
            try:
                for _ in range(100):
                    state['produced'] += 1
                    yield b'x' * 10
                    await asyncio.sleep(0.05)
            finally:
                state['finalized'] += 1
        return StreamingResponse(gen(), media_type='text/plain')

    @app.on('scope_completed')
    async def ended(event):
        state['ended'] += 1
    return app


async def _abort_mid_stream(client_scenario):
    """Abort mid-stream; the generator, the request and the connection must
    all end while the server runs, so its stop then drains at once."""
    from tests.harness import served

    state = {'produced': 0, 'finalized': 0, 'ended': 0}
    loop = asyncio.get_running_loop()
    async with served(_slow_stream_app(state)) as s:
        result = await asyncio.wait_for(client_scenario(s.server.port), timeout=10.0)
        assert result.aborted
        for _ in range(100):
            if state['finalized'] and state['ended']:
                break
            await asyncio.sleep(0.02)
        assert state['finalized'] == 1 and state['ended'] == 1
        assert state['produced'] < 30
        stopping = loop.time()
    assert loop.time() - stopping < 1.0, 'a connection outlived its request'


@pytest.mark.asyncio
async def test_h1_peer_abort_finalizes_the_stream():
    from blackbull.client.http1 import HTTP1Client
    from blackbull.fault_injection import Abort, H1CSendRawBytes, Scenario, Sleep

    async def scenario(port):
        async with HTTP1Client('127.0.0.1', port) as client:
            return await client.execute_scenario(Scenario(steps=(
                H1CSendRawBytes(b'GET /long HTTP/1.1\r\nHost: x\r\n\r\n'),
                Sleep(0.3),
                Abort())))
    await _abort_mid_stream(scenario)


@pytest.mark.asyncio
async def test_h2_connection_abort_finalizes_the_stream():
    from blackbull.client.http2 import HTTP2Client
    from blackbull.fault_injection import Abort, Sleep
    from blackbull.fault_injection.scenario_h2_client import (
        ScenarioH2Client, SendFrame, SendHeaders, SendPreface,
    )
    from blackbull.protocol.frame_types import FrameTypes

    async def scenario(port):
        async with HTTP2Client('127.0.0.1', port, scenario_mode=True) as client:
            return await client.execute_scenario(ScenarioH2Client(steps=(
                SendPreface(),
                SendFrame(FrameTypes.SETTINGS),
                SendHeaders(pseudo=((':method', 'GET'), (':scheme', 'http'),
                                    (':authority', 'x'), (':path', '/long')),
                            end_stream=True),
                Sleep(0.3),
                Abort())))
    await _abort_mid_stream(scenario)


@pytest.mark.asyncio
async def test_grpc_stream_to_a_gone_peer_ends_quietly(caplog):
    from blackbull.connection import Connection
    from blackbull.grpc import GrpcServiceRegistry, encode_message
    from blackbull.request import ClientDisconnected

    state = {'finalized': 0}
    registry = GrpcServiceRegistry()

    @registry.method('/s.S/Stream')
    async def stream(request, context):
        try:
            for _ in range(100):
                yield b'm'
                await asyncio.sleep(0)
        finally:
            state['finalized'] += 1

    sends = 0

    async def send(event):
        nonlocal sends
        sends += 1
        if sends > 2:
            raise ClientDisconnected()

    sent = False

    async def receive():
        nonlocal sent
        body, sent = (b'' if sent else encode_message(b'')), True
        return {'type': 'http.request', 'body': body, 'more_body': False}

    app = BlackBull()
    app.enable_grpc(registry)
    conn = Connection.from_scope({'type': 'http', 'method': 'POST', 'path': '/s.S/Stream',
                                  'scheme': 'http', 'http_version': '2',
                                  'headers': [(b'content-type', b'application/grpc')]})
    with contextlib.suppress(ClientDisconnected):   # the server's RequestActor absorbs it
        await app(conn, receive, send)
    assert state['finalized'] == 1
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


@pytest.mark.asyncio
@pytest.mark.parametrize('path, reported', [
    ('/boom', ValueError), ('/missing', type(None)), ('/grpc', type(None)),
])
async def test_an_error_answer_to_a_gone_peer_is_an_ordinary_close(path, reported):
    """An error answer that finds the peer gone ends the scope quietly and
    still reports what went wrong, not the lost connection."""
    from blackbull.connection import Connection
    from blackbull.grpc import GrpcServiceRegistry
    from blackbull.request import ClientDisconnected

    app = BlackBull()
    app.enable_grpc(GrpcServiceRegistry())   # /grpc: UNIMPLEMENTED
    seen = []

    @app.route(path='/boom')
    async def boom(conn):
        raise ValueError('handler failed')

    @app.on('scope_completed', blocking=True)
    async def ended(event):
        seen.append(event.detail['exception'])

    async def send(*args, **kwargs):
        raise ClientDisconnected()

    headers = [(b'content-type', b'application/grpc')] if path == '/grpc' else []
    conn = Connection.from_scope({'type': 'http', 'method': 'POST' if path == '/grpc' else 'GET',
                                  'path': path, 'scheme': 'http', 'http_version': '2',
                                  'headers': headers})
    await app(conn, None, send)
    assert [type(e) for e in seen] == [reported]
