import asyncio
import time

import pytest

from blackbull.connection import Connection
from blackbull.grpc import GrpcServiceRegistry, encode_message
from blackbull.grpc.asgi import serve_grpc
from blackbull.native import NativeResponse


SHAPES = ('unary', 'server-streaming', 'client-streaming', 'bidi')


async def _call(shape, stage, *, timeout=b'20m', native=False):
    registry = GrpcServiceRegistry()
    events = []
    entered = []
    closed = []
    remaining = []
    reads = 0
    parked = asyncio.Event()
    started_handler = asyncio.Event()

    async def receive():
        nonlocal reads
        reads += 1
        if stage == 'input' or (stage == 'partial-input' and reads > 1):
            await parked.wait()
        if stage == 'partial-input':
            return {'type': 'http.request', 'body': encode_message(b'x')[:3],
                    'more_body': True}
        if stage == 'budget':
            await asyncio.sleep(0.035)
        return {'type': 'http.request', 'body': encode_message(b'x'),
                'more_body': False}

    eof = False

    async def next_chunk():
        nonlocal eof
        if eof:
            return None
        event = await receive()
        eof = not event['more_body']
        return event['body']

    if native:
        receive.next_chunk = next_chunk

    async def work(request, context):
        entered.append(True)
        started_handler.set()
        remaining.append(context.time_remaining())
        try:
            if shape in ('client-streaming', 'bidi'):
                async for _ in request:
                    pass
            if stage in ('handler', 'budget', 'cancel-cleanup', 'cancel'):
                await parked.wait()
            if stage == 'metadata':
                await context.send_initial_metadata([(b'x-test', b'yes')])
            if stage == 'cpu':
                time.sleep(0.035)
            if stage == 'handler-timeout':
                raise TimeoutError('handler bug')
            if stage == 'slow-success':
                await asyncio.sleep(0.035)
            return b'ok'
        finally:
            closed.append(True)
            if stage == 'handler':
                assert context.time_remaining() == 0
            if stage == 'cancel-cleanup':
                await parked.wait()

    async def unary(request, context):
        return await work(request, context)

    async def streaming(request, context):
        try:
            if stage == 'idle-send':
                yield b'first'
                await parked.wait()
            else:
                yield await work(request, context)
        finally:
            closed.append('generator')
            if stage == 'close':
                await parked.wait()

    registry.add_method('/svc/M', streaming if shape in ('server-streaming', 'bidi')
                        else unary, client_streaming=shape in ('client-streaming', 'bidi'))

    async def send(event):
        expanded = event.to_asgi() if isinstance(event, NativeResponse) else [event]
        if stage in ('send', 'idle-send') and any(
                e['type'] == 'http.response.body' and e.get('body') for e in expanded):
            await parked.wait()
        if stage == 'trailers' and any(
                (b'grpc-status', b'0') in e.get('headers', []) for e in expanded):
            await parked.wait()
        if stage == 'all-send':
            await parked.wait()
        if stage == 'metadata' and any(e['type'] == 'http.response.start' for e in expanded):
            await parked.wait()
        events.extend(expanded)

    headers = [(b'content-type', b'application/grpc')]
    if timeout is not None:
        headers.append((b'grpc-timeout', timeout))
    started = asyncio.get_running_loop().time()
    before = asyncio.all_tasks()
    try:
        async with asyncio.timeout(0.5):
            call = serve_grpc(registry, Connection.from_scope({'path': '/svc/M', 'headers': headers}),
                              receive, send)
            if stage == 'cancel':
                task = asyncio.create_task(call)
                await started_handler.wait()
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                await call
    except TimeoutError as exc:
        if stage != 'all-send' or 'notification blocked' not in str(exc):
            raise
    elapsed = asyncio.get_running_loop().time() - started
    assert not (asyncio.all_tasks() - before), 'RPC left a background task'
    statuses = [v for e in events for k, v in e.get('headers', []) if k == b'grpc-status']
    return statuses, entered, closed, remaining, elapsed


@pytest.mark.asyncio
@pytest.mark.parametrize('shape', SHAPES)
@pytest.mark.parametrize('native', [False, True])
@pytest.mark.parametrize('stage', ['input', 'partial-input', 'handler', 'send', 'trailers', 'metadata'])
async def test_deadline_covers_whole_rpc(shape, native, stage):
    statuses, entered, closed, remaining, elapsed = await _call(shape, stage, native=native)
    assert statuses == [b'4']
    assert elapsed < 0.3
    if stage in ('input', 'partial-input') and shape in ('unary', 'server-streaming'):
        assert not entered
    if entered:
        assert closed


@pytest.mark.asyncio
@pytest.mark.parametrize('shape', SHAPES)
async def test_input_time_is_not_regranted_to_handler(shape):
    statuses, _, _, _, elapsed = await _call(shape, 'budget', timeout=b'50m')
    assert statuses == [b'4']
    assert elapsed < 0.075


@pytest.mark.asyncio
@pytest.mark.parametrize('shape', SHAPES)
async def test_handler_timeout_error_is_internal(shape):
    statuses, *_ = await _call(shape, 'handler-timeout', timeout=b'1S')
    assert statuses == [b'13']


@pytest.mark.asyncio
@pytest.mark.parametrize('shape', SHAPES)
@pytest.mark.parametrize('timeout', [None, b'1S'])
async def test_success_and_no_deadline(shape, timeout):
    statuses, _, closed, remaining, _ = await _call(shape, 'slow-success', timeout=timeout)
    assert statuses == [b'0']
    assert closed
    assert remaining[0] is None if timeout is None else 0 < remaining[0] <= 1


@pytest.mark.asyncio
@pytest.mark.parametrize('shape', ['server-streaming', 'bidi'])
@pytest.mark.parametrize('stage', ['idle-send', 'close'])
async def test_stream_cleanup_and_idle_send_are_bounded(shape, stage):
    statuses, _, closed, _, elapsed = await _call(shape, stage)
    assert statuses == [b'4']
    assert 'generator' in closed
    assert elapsed < 0.3


@pytest.mark.asyncio
@pytest.mark.parametrize('shape', SHAPES)
async def test_deadline_notification_cannot_wait_forever(shape):
    statuses, *_, elapsed = await _call(shape, 'all-send')
    assert not statuses
    assert elapsed < 0.3


@pytest.mark.asyncio
@pytest.mark.parametrize('shape', SHAPES)
@pytest.mark.parametrize('stage', ['cpu', 'cancel-cleanup'])
async def test_non_yielding_work_and_cancellation_cleanup(shape, stage):
    statuses, _, closed, _, elapsed = await _call(shape, stage)
    assert statuses == [b'4']
    assert closed
    assert elapsed < 0.3


@pytest.mark.asyncio
@pytest.mark.parametrize('shape', SHAPES)
@pytest.mark.parametrize('timeout', [None, b'1S'])
async def test_external_cancel_is_not_a_grpc_error(shape, timeout):
    statuses, _, closed, _, _ = await _call(shape, 'cancel', timeout=timeout)
    assert statuses == []
    assert closed


@pytest.mark.asyncio
@pytest.mark.parametrize('shape', SHAPES)
async def test_timeout_owner_marks_expiry_before_clock_rounds_up(monkeypatch, shape):
    timeout_at = asyncio.timeout_at
    monkeypatch.setattr(asyncio, 'timeout_at', lambda deadline: timeout_at(deadline - 0.01))
    statuses, *_ = await _call(shape, 'handler')
    assert statuses == [b'4']


@pytest.mark.asyncio
async def test_iterator_setup_expiry_prevents_first_message():
    entered = []
    closed = []
    statuses = []

    class Stream:
        def __aiter__(self):
            time.sleep(0.15)
            return self

        async def __anext__(self):
            entered.append(True)
            raise StopAsyncIteration

        async def aclose(self):
            closed.append(True)

    registry = GrpcServiceRegistry()
    registry.add_method('/svc/M', lambda request, context: Stream(), streaming=True)

    async def receive():
        return {'type': 'http.request', 'body': encode_message(b''), 'more_body': False}

    async def send(event):
        for item in event.to_asgi():
            statuses.extend(value for key, value in item.get('headers', []) if key == b'grpc-status')

    scope = {'type': 'http', 'path': '/svc/M',
             'headers': [(b'content-type', b'application/grpc'), (b'grpc-timeout', b'100m')]}
    await serve_grpc(registry, Connection.from_scope(scope), receive, send)
    assert not entered
    assert closed == [True]
    assert statuses == [b'4']
