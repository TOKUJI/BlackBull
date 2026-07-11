"""Interactive server-streaming must not withhold yielded messages.

The write-coalescing batcher (Sprint 63's streaming-collapse fix) buffers
consecutive messages into one DATA frame.  Sprint 66's health ``Watch``
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
        events.append(event)

    await serve_grpc(reg, _grpc_scope('/slow.S/Tick'),
                     _receive_with(encode_message(b'')), send)
    messages = [m for b in _bodies(events) for _, m in decode_messages(b)]
    assert messages == [b'tick0', b'tick1', b'tick2']
    assert len(_bodies(events)) == 3      # one flush per awaited message
