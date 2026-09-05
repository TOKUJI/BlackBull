from __future__ import annotations

import asyncio
import logging

import pytest

from blackbull.client.http2 import HTTP2Client, _PendingResponse
from blackbull.env import reset_settings_cache
from blackbull.protocol.frame_types import ErrorCodes, FrameTypes


@pytest.fixture(autouse=True)
def _fresh_settings():
    reset_settings_cache()
    yield
    reset_settings_cache()


class _Clock:
    def __init__(self):
        self.now = 0.0

    def time(self):
        return self.now


def _client() -> HTTP2Client:
    c = HTTP2Client('localhost', 1)
    c._writer = object()
    c.sent = []

    async def capture(frame):
        c.sent.append(frame)

    c._control_sender = capture
    return c


async def _final_headers(c: HTTP2Client, stream_id: int) -> None:
    frame = c._factory.create(FrameTypes.HEADERS, 4, stream_id)
    frame.pseudo_headers[':status'] = '200'
    await c._on_response_headers(frame)


async def _data(c, stream_id, payload, *, end_stream=False):
    frame = c._factory.create(
        FrameTypes.DATA, 1 if end_stream else 0, stream_id, data=payload)
    await c._on_response_data(frame)


@pytest.mark.asyncio
async def test_h2_rate_floor_is_off_by_default(monkeypatch):
    monkeypatch.setenv('BB_CLIENT_BODY_TIMEOUT', '0')
    import blackbull.client.http2 as http2
    clock = _Clock()
    monkeypatch.setattr(http2, '_monotonic', clock.time)
    c = _client()
    future = asyncio.get_running_loop().create_future()
    c._responses[1] = _PendingResponse(future=future)
    await _final_headers(c, 1)
    await _data(c, 1, b'x')
    clock.now = 10.0
    await _data(c, 1, b'x')
    clock.now = 20.0
    await _data(c, 1, b'', end_stream=True)
    assert future.done()
    assert future.exception() is None
    assert (await asyncio.shield(future)).body == b'xx'


@pytest.mark.asyncio
async def test_h2_nonempty_slow_drip_fails_with_client_rate_cap(
        monkeypatch, caplog):
    monkeypatch.setenv('BB_CLIENT_BODY_TIMEOUT', '0')
    monkeypatch.setenv('BB_CLIENT_MIN_BODY_RATE', '10')
    monkeypatch.setenv('BB_CLIENT_MIN_BODY_RATE_GRACE', '1')
    import blackbull.client.http2 as http2
    clock = _Clock()
    monkeypatch.setattr(http2, '_monotonic', clock.time)
    caplog.set_level(logging.WARNING, logger='blackbull.caps')
    caplog.clear()
    c = _client()
    future = asyncio.get_running_loop().create_future()
    c._responses[1] = _PendingResponse(future=future)
    await _final_headers(c, 1)
    await _data(c, 1, b'1234567890')
    clock.now = 2.0
    await _data(c, 1, b'x')
    error = future.exception()
    assert isinstance(error, TimeoutError)
    assert 'BB_CLIENT_MIN_BODY_RATE=10.0 B/s' in str(error)
    records = [r for r in caplog.records
               if getattr(r, 'cap', None) == 'client_min_body_rate']
    assert len(records) == 1
    assert records[0].protocol == 'http2'
    assert records[0].limit == 10.0
    assert records[0].requested < records[0].limit
    resets = [f for f in c.sent if f.FrameType() == FrameTypes.RST_STREAM]
    assert len(resets) == 1
    assert resets[0].stream_id == 1
    assert resets[0].error_code == ErrorCodes.CANCEL


@pytest.mark.asyncio
async def test_h2_rate_floor_charges_empty_data_and_resets_only_one_stream(
        monkeypatch):
    monkeypatch.setenv('BB_CLIENT_BODY_TIMEOUT', '0')
    monkeypatch.setenv('BB_CLIENT_MIN_BODY_RATE', '10')
    monkeypatch.setenv('BB_CLIENT_MIN_BODY_RATE_GRACE', '1')
    import blackbull.client.http2 as http2
    clock = _Clock()
    monkeypatch.setattr(http2, '_monotonic', clock.time)
    c = _client()
    f1 = asyncio.get_running_loop().create_future()
    f2 = asyncio.get_running_loop().create_future()
    c._responses[1] = _PendingResponse(future=f1)
    c._responses[3] = _PendingResponse(future=f2)
    await _final_headers(c, 1)
    await _final_headers(c, 3)
    await _data(c, 1, b'1234567890')
    await _data(c, 3, b'other')
    clock.now = 0.1
    await _data(c, 3, b'end', end_stream=True)
    assert (await f2).body == b'otherend'
    clock.now = 1.1
    await _data(c, 1, b'')
    assert f1.done()
    assert isinstance(f1.exception(), TimeoutError)
    resets = [f for f in c.sent if f.FrameType() == FrameTypes.RST_STREAM]
    assert len(resets) == 1
    assert resets[0].stream_id == 1
    assert resets[0].error_code == ErrorCodes.CANCEL


@pytest.mark.asyncio
async def test_h2_rate_floor_fast_payload_rolls_the_window(monkeypatch):
    monkeypatch.setenv('BB_CLIENT_BODY_TIMEOUT', '0')
    monkeypatch.setenv('BB_CLIENT_MIN_BODY_RATE', '10')
    monkeypatch.setenv('BB_CLIENT_MIN_BODY_RATE_GRACE', '1')
    import blackbull.client.http2 as http2
    clock = _Clock()
    monkeypatch.setattr(http2, '_monotonic', clock.time)
    c = _client()
    future = asyncio.get_running_loop().create_future()
    c._responses[1] = _PendingResponse(future=future)
    await _final_headers(c, 1)
    await _data(c, 1, b'a' * 20)
    clock.now = 1.1
    await _data(c, 1, b'a' * 20)
    clock.now = 2.2
    await _data(c, 1, b'a' * 20, end_stream=True)
    result = await future
    assert result.body == b'a' * 60


@pytest.mark.asyncio
async def test_h2_rate_floor_excludes_think_time_before_first_payload(monkeypatch):
    monkeypatch.setenv('BB_CLIENT_BODY_TIMEOUT', '0')
    monkeypatch.setenv('BB_CLIENT_MIN_BODY_RATE', '10')
    monkeypatch.setenv('BB_CLIENT_MIN_BODY_RATE_GRACE', '1')
    import blackbull.client.http2 as http2
    clock = _Clock()
    monkeypatch.setattr(http2, '_monotonic', clock.time)
    c = _client()
    future = asyncio.get_running_loop().create_future()
    c._responses[1] = _PendingResponse(future=future)
    await _final_headers(c, 1)
    clock.now = 100.0
    await _data(c, 1, b'first')
    clock.now = 100.5
    await _data(c, 1, b'second', end_stream=True)
    assert (await future).body == b'firstsecond'


@pytest.mark.asyncio
async def test_h2_padding_does_not_pay_the_rate_floor(monkeypatch, caplog):
    monkeypatch.setenv('BB_CLIENT_BODY_TIMEOUT', '0')
    monkeypatch.setenv('BB_CLIENT_MIN_BODY_RATE', '10')
    monkeypatch.setenv('BB_CLIENT_MIN_BODY_RATE_GRACE', '1')
    import blackbull.client.http2 as http2
    clock = _Clock()
    monkeypatch.setattr(http2, '_monotonic', clock.time)
    caplog.set_level(logging.WARNING, logger='blackbull.caps')
    caplog.clear()
    c = _client()
    future = asyncio.get_running_loop().create_future()
    c._responses[1] = _PendingResponse(future=future)
    await _final_headers(c, 1)
    await _data(c, 1, b'first')
    padded = c._factory.create(FrameTypes.DATA, 0x8, 1,
                               data=bytes([255]) + b'\x00' * 255)
    assert padded.payload == b''
    clock.now = 1.1
    await c._on_response_data(padded)
    assert isinstance(future.exception(), TimeoutError)
    record = [r for r in caplog.records
              if getattr(r, 'cap', None) == 'client_min_body_rate'][-1]
    assert len([r for r in caplog.records
                if getattr(r, 'cap', None) == 'client_min_body_rate']) == 1
    assert record.protocol == 'http2'


@pytest.mark.asyncio
@pytest.mark.parametrize('terminal', ['data', 'trailers'])
async def test_h2_rate_floor_settles_terminal_wait(monkeypatch, terminal):
    monkeypatch.setenv('BB_CLIENT_BODY_TIMEOUT', '0')
    monkeypatch.setenv('BB_CLIENT_MIN_BODY_RATE', '10')
    monkeypatch.setenv('BB_CLIENT_MIN_BODY_RATE_GRACE', '1')
    import blackbull.client.http2 as http2
    clock = _Clock()
    monkeypatch.setattr(http2, '_monotonic', clock.time)
    c = _client()
    future = asyncio.get_running_loop().create_future()
    c._responses[1] = _PendingResponse(future=future)
    await _final_headers(c, 1)
    await _data(c, 1, b'first')
    clock.now = 1.1
    if terminal == 'data':
        await _data(c, 1, b'', end_stream=True)
    else:
        frame = c._factory.create(FrameTypes.HEADERS, 5, 1)
        frame.headers.append(('grpc-status', '0'))
        await c._on_response_headers(frame)
    assert isinstance(future.exception(), TimeoutError)


@pytest.mark.asyncio
async def test_h2_rate_refusal_credits_connection_and_preserves_other_stream(
        monkeypatch):
    monkeypatch.setenv('BB_CLIENT_BODY_TIMEOUT', '0')
    monkeypatch.setenv('BB_CLIENT_MIN_BODY_RATE', '10')
    monkeypatch.setenv('BB_CLIENT_MIN_BODY_RATE_GRACE', '1')
    import blackbull.client.http2 as http2
    clock = _Clock()
    monkeypatch.setattr(http2, '_monotonic', clock.time)
    c = _client()
    bad = asyncio.get_running_loop().create_future()
    good = asyncio.get_running_loop().create_future()
    c._responses[1] = _PendingResponse(future=bad)
    c._responses[3] = _PendingResponse(future=good)
    await _final_headers(c, 1)
    await _final_headers(c, 3)
    await _data(c, 1, b'x')
    clock.now = 1.1
    c._unacked_conn = 32767
    await _data(c, 1, b'x')
    assert isinstance(bad.exception(), TimeoutError)
    assert any(frame.FrameType() == FrameTypes.WINDOW_UPDATE
               and frame.stream_id == 0 for frame in c.sent)
    resets = [frame for frame in c.sent
              if frame.FrameType() == FrameTypes.RST_STREAM]
    assert len(resets) == 1
    assert resets[0].stream_id == 1
    assert resets[0].error_code == ErrorCodes.CANCEL
    await _data(c, 3, b'ok', end_stream=True)
    assert (await good).body == b'ok'
