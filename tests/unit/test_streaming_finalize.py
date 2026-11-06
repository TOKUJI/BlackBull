"""A streamed response whose send fails closes its generator (BLA-641)."""
import asyncio

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
