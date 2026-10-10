"""Interactive server-streaming must not withhold yielded messages.

The write-coalescing batcher buffers
consecutive messages into one DATA frame.  The health ``Watch``
interop exposed the gap: a producer that *blocks indefinitely* between yields
(a status watch, a chat stream, a notification feed) had its buffered
message(s) withheld until the next message completed — for ``Watch``, i.e.
until a status change that may never come, so the client's initial status
never arrived.

The contract pinned here: once the producer suspends (is genuinely waiting),
everything already yielded is flushed to the client; synchronous bursts keep
batching into single DATA frames.
"""
import asyncio

import pytest

from blackbull.grpc import GrpcServiceRegistry, encode_message, decode_messages
from blackbull.grpc.asgi import serve_grpc
from blackbull.native import NativeResponse


def _grpc_scope(path):
    return {'type': 'http', 'path': path,
            'headers': [(b'content-type', b'application/grpc'),
                        (b':method', b'POST')]}


def _receive_with(body: bytes):
    sent = False

    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {'type': 'http.request', 'body': body, 'more_body': False}
        return {'type': 'http.request', 'body': b'', 'more_body': False}
    return receive


def _bodies(events):
    return [e['body'] for e in events if e['type'] == 'http.response.body']


@pytest.mark.asyncio
async def test_message_delivered_while_producer_blocks():
    """The first yielded message reaches the wire while the handler is still
    parked on an await that may never resolve."""
    reg = GrpcServiceRegistry()
    release = asyncio.Event()

    @reg.method('/watch.W/Watch')
    async def watch(request, context):
        yield b'current-status'
        await release.wait()                 # blocks until the test flips it
        yield b'new-status'

    events = []

    async def send(event):
        # gRPC emits native on the seam; these assertions are
        # about the wire, so expand to the ASGI events it stands for.
        if isinstance(event, NativeResponse):
            events.extend(event.to_asgi())
        else:
            events.append(event)

    call = asyncio.create_task(serve_grpc(
        reg, _grpc_scope('/watch.W/Watch'),
        _receive_with(encode_message(b'')), send))

    # The first message must arrive without touching `release` — poll for the
    # body event while the producer stays parked.
    async def first_body():
        while not _bodies(events):
            await asyncio.sleep(0.01)
        return _bodies(events)[0]

    body = await asyncio.wait_for(first_body(), timeout=2.0)
    assert decode_messages(body) == [(False, b'current-status')]
    assert not call.done()

    release.set()
    await asyncio.wait_for(call, timeout=2.0)
    all_messages = [m for b in _bodies(events) for _, m in decode_messages(b)]
    assert all_messages == [b'current-status', b'new-status']
    trailers = [e for e in events if e['type'] == 'http.response.trailers']
    assert trailers and (b'grpc-status', b'0') in trailers[0]['headers']


@pytest.mark.asyncio
async def test_synchronous_burst_still_batches():
    """Bulk streams keep the collapse fix: a synchronous burst of small
    messages coalesces into far fewer DATA events than messages."""
    reg = GrpcServiceRegistry()
    n = 1000

    @reg.method('/bulk.B/Burst')
    async def burst(request, context):
        for i in range(n):
            yield b'x' * 20

    events = []

    async def send(event):
        # gRPC emits native on the seam; these assertions are
        # about the wire, so expand to the ASGI events it stands for.
        if isinstance(event, NativeResponse):
            events.extend(event.to_asgi())
        else:
            events.append(event)

    await serve_grpc(reg, _grpc_scope('/bulk.B/Burst'),
                     _receive_with(encode_message(b'')), send)
    bodies = _bodies(events)
    total = sum(len(decode_messages(b)) for b in bodies)
    assert total == n
    # 1000 × 25 B framed ≈ 25 KB → at most a handful of flushes (16 KB batch
    # threshold + tail), far fewer than one event per message.
    assert len(bodies) <= 5


@pytest.mark.asyncio
async def test_slow_producer_flushes_each_message():
    """A producer that awaits between yields delivers each message promptly —
    three sleeps, three separate deliveries observable in arrival order."""
    reg = GrpcServiceRegistry()

    @reg.method('/slow.S/Tick')
    async def tick(request, context):
        for i in range(3):
            await asyncio.sleep(0.02)
            yield f'tick{i}'.encode()

    events = []

    async def send(event):
        # gRPC emits native on the seam; these assertions are
        # about the wire, so expand to the ASGI events it stands for.
        if isinstance(event, NativeResponse):
            events.extend(event.to_asgi())
        else:
            events.append(event)

    await serve_grpc(reg, _grpc_scope('/slow.S/Tick'),
                     _receive_with(encode_message(b'')), send)
    messages = [m for b in _bodies(events) for _, m in decode_messages(b)]
    assert messages == [b'tick0', b'tick1', b'tick2']
    assert len(_bodies(events)) == 3      # one flush per awaited message


@pytest.mark.asyncio
@pytest.mark.parametrize('batch_bytes', [14, 16384])
@pytest.mark.parametrize('client_streaming', [False, True])
@pytest.mark.parametrize('timed', [False, True])
async def test_batches_rearm_after_backpressure(monkeypatch, batch_bytes, client_streaming, timed):
    import blackbull.grpc.asgi as grpc_asgi

    monkeypatch.setattr(grpc_asgi, '_STREAM_BATCH_BYTES', batch_bytes)
    registry = GrpcServiceRegistry()
    delivered = [asyncio.Event() for _ in range(3)]
    write_started = asyncio.Event()
    release_write = asyncio.Event()
    expected = [bytes([i, j]) for i in range(3) for j in range(32)]
    received = []
    statuses = []

    async def stream(request, context):
        if client_streaming:
            async for _ in request:
                pass
        message = bytearray(2)
        for batch in range(3):
            for index in range(32):
                message[:] = bytes([batch, index])
                yield message
            message[:] = b'xx'
            await delivered[batch].wait()

    registry.add_method('/svc/M', stream, client_streaming=client_streaming)

    async def send(event):
        for item in event.to_asgi():
            if item.get('body'):
                if not write_started.is_set():
                    write_started.set()
                    await release_write.wait()
                received.extend(payload for _, payload in decode_messages(item['body']))
                for batch in range(3):
                    if len(received) >= (batch + 1) * 32:
                        delivered[batch].set()
            statuses.extend(value for key, value in item.get('headers', []) if key == b'grpc-status')

    scope = _grpc_scope('/svc/M')
    if timed:
        scope['headers'].append((b'grpc-timeout', b'5S'))
    async with asyncio.TaskGroup() as group:
        task = group.create_task(serve_grpc(
            registry, scope, _receive_with(encode_message(b'')), send))
        await asyncio.wait_for(write_started.wait(), 1)
        release_write.set()
        await asyncio.wait_for(task, 1)
    assert received == expected
    assert statuses == [b'0']


@pytest.mark.asyncio
@pytest.mark.parametrize('timed', [False, True])
async def test_synchronous_stream_closes_before_final_delivery(timed):
    registry = GrpcServiceRegistry()
    events = []

    class Stream:
        def __init__(self, context):
            self.context = context
            self.sent = False

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self.sent:
                raise StopAsyncIteration
            self.sent = True
            return b'last'

        async def aclose(self):
            assert not events
            await self.context.send_initial_metadata([(b'x-close', b'initial')])
            self.context.set_trailing_metadata([(b'x-close', b'trailing')])

    registry.add_method('/svc/M', lambda request, context: Stream(context), streaming=True)

    async def send(event):
        events.extend(event.to_asgi())

    scope = _grpc_scope('/svc/M')
    if timed:
        scope['headers'].append((b'grpc-timeout', b'1S'))
    await serve_grpc(registry, scope, _receive_with(encode_message(b'')), send)
    assert (b'x-close', b'initial') in events[0]['headers']
    assert (b'x-close', b'trailing') in events[-1]['headers']
    assert (b'grpc-status', b'0') in events[-1]['headers']
    assert [payload for body in _bodies(events) for _, payload in decode_messages(body)] == [b'last']
    completed = list(events)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert events == completed


@pytest.mark.asyncio
@pytest.mark.parametrize('timed', [False, True])
async def test_cooperative_producer_delivers_before_parking(timed):
    registry = GrpcServiceRegistry()
    delivered = asyncio.Event()
    received = []
    statuses = []

    @registry.method('/svc/M')
    async def stream(request, context):
        for _ in range(3):
            yield b'item'
            await asyncio.sleep(0)
        await delivered.wait()

    async def send(event):
        for item in event.to_asgi():
            if item.get('body'):
                received.extend(payload for _, payload in decode_messages(item['body']))
                if len(received) == 3:
                    delivered.set()
            statuses.extend(value for key, value in item.get('headers', []) if key == b'grpc-status')

    scope = _grpc_scope('/svc/M')
    if timed:
        scope['headers'].append((b'grpc-timeout', b'1S'))
    await asyncio.wait_for(serve_grpc(
        registry, scope, _receive_with(encode_message(b'')), send), 0.5)
    assert received == [b'item'] * 3
    assert statuses == [b'0']
