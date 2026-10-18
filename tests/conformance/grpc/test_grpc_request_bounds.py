from __future__ import annotations

import asyncio
import gzip
from itertools import chain
import struct
import tracemalloc

import pytest

from blackbull.grpc import GrpcServiceRegistry, encode_message
from blackbull.grpc.asgi import serve_grpc
import blackbull.grpc.asgi as grpc_asgi
from blackbull.native import NativeResponse
from blackbull.request import ClientDisconnected
from blackbull.connection import Connection


SHAPES = ('unary', 'server-streaming', 'client-streaming', 'bidi')


async def _call(chunks, shape='unary', *, encoding=b'', native=False, receiver=None):
    chunks = iter(chunks)
    calls = 0
    seen = []
    events = []

    def next_item():
        nonlocal calls
        calls += 1
        try:
            item = next(chunks)
        except StopIteration:
            pytest.fail('read beyond the supplied input boundary')
        if isinstance(item, BaseException):
            raise item
        return item

    async def receive():
        assert not native, 'native input must use next_chunk'
        item = next_item()
        return item if isinstance(item, dict) else {
            'type': 'http.request', 'body': item[0], 'more_body': item[1]}

    async def next_chunk():
        item = next_item()
        if isinstance(item, dict):
            raise ClientDisconnected()
        if not item[1]:
            # Native recipients deliver the final bytes before the EOF marker.
            nonlocal chunks
            chunks = chain([(b'', False)], chunks) if item[0] else chunks
        return item[0] or (b'' if item[1] else None)

    if native:
        receive.next_chunk = next_chunk
    if receiver is not None:
        receive = receiver

    async def unary(request, context):
        seen.append(request)
        return b'ok'

    async def server_stream(request, context):
        seen.append(request)
        yield b'ok'

    async def client_stream(request_iter, context):
        async for request in request_iter:
            seen.append(request)
        return b'ok'

    async def bidi(request_iter, context):
        async for request in request_iter:
            seen.append(request)
        yield b'ok'

    registry = GrpcServiceRegistry()
    registry.add_method('/svc/M', dict(zip(SHAPES, (
        unary, server_stream, client_stream, bidi)))[shape])

    async def send(event):
        events.extend(event.to_asgi() if isinstance(event, NativeResponse) else [event])

    await serve_grpc(registry, Connection.from_scope({
        'type': 'http', 'path': '/svc/M',
        'headers': [(b'content-type', b'application/grpc'),
                    (b'grpc-encoding', encoding)]}), receive, send)
    headers = {k: v for event in events for k, v in event.get('headers', [])}
    return headers[b'grpc-status'], seen, calls


@pytest.fixture(autouse=True)
def small_limits(monkeypatch):
    monkeypatch.setattr(grpc_asgi, 'MAX_MESSAGE_SIZE', 16)
    monkeypatch.setattr(grpc_asgi, 'MAX_MESSAGE_LENGTH', 64)


@pytest.mark.asyncio
@pytest.mark.parametrize('shape', SHAPES)
@pytest.mark.parametrize('native', [False, True])
@pytest.mark.parametrize('split', range(1, 6))
@pytest.mark.parametrize('compressed', [False, True])
async def test_declared_oversize_rejected_before_reading_body(shape, native, split, compressed):
    prefix = struct.pack('>BI', int(compressed), 65 if compressed else 17)
    chunks = [(prefix[:split], True)]
    if split < 5:
        chunks.append((prefix[split:], True))
    status, seen, calls = await _call(chunks, shape, native=native, encoding=b'gzip')
    assert status == b'8'
    assert seen == []
    assert calls == len(chunks)


@pytest.mark.asyncio
@pytest.mark.parametrize('shape', SHAPES)
@pytest.mark.parametrize('native', [False, True])
@pytest.mark.parametrize('size', [0, 15, 16, 17])
@pytest.mark.parametrize('compressed', [False, True])
async def test_message_boundaries_across_single_byte_chunks(shape, native, size, compressed):
    message = b'x' * size
    payload = gzip.compress(message) if compressed else message
    frame = encode_message(payload, compressed=compressed)
    chunks = [(frame[i:i + 1], i < len(frame) - 1) for i in range(len(frame))]
    status, seen, _ = await _call(chunks, shape, native=native, encoding=b'gzip')
    assert status == (b'8' if size > 16 else b'0')
    assert seen == ([] if size > 16 else [message])


@pytest.mark.asyncio
@pytest.mark.parametrize('shape', SHAPES[:2])
@pytest.mark.parametrize('native', [False, True])
@pytest.mark.parametrize('coalesced', [False, True])
async def test_unary_extra_message_rejected_at_its_prefix(shape, native, coalesced):
    first = encode_message(b'one')
    second_prefix = struct.pack('>BI', 0, 16)
    chunks = [(first + second_prefix, True)] if coalesced else [
        (first, True), (second_prefix, True)]
    status, seen, calls = await _call(chunks, shape, native=native)
    assert status == b'12'
    assert seen == []
    assert calls == len(chunks)


@pytest.mark.asyncio
@pytest.mark.parametrize('shape', SHAPES[2:])
@pytest.mark.parametrize('native', [False, True])
async def test_streaming_total_is_not_a_unary_body_limit(shape, native):
    status, seen, _ = await _call(
        [(encode_message(b'x' * 16) * 20, False)], shape, native=native)
    assert status == b'0'
    assert seen == [b'x' * 16] * 20


@pytest.mark.asyncio
@pytest.mark.parametrize('shape', SHAPES)
@pytest.mark.parametrize('native', [False, True])
@pytest.mark.parametrize('cut', range(1, 7))
async def test_truncated_prefix_or_body_is_internal(shape, native, cut):
    status, seen, _ = await _call([(encode_message(b'ok')[:cut], False)], shape, native=native)
    assert status == b'13'
    assert seen == []


@pytest.mark.asyncio
@pytest.mark.parametrize('shape', SHAPES)
@pytest.mark.parametrize('native', [False, True])
async def test_disconnect_before_body_completion_is_cancelled(shape, native):
    status, seen, _ = await _call([
        (encode_message(b'ok')[:6], True), {'type': 'http.disconnect'}], shape, native=native)
    assert status == b'1'
    assert seen == []


@pytest.mark.asyncio
@pytest.mark.parametrize('shape', SHAPES)
@pytest.mark.parametrize('native', [False, True])
async def test_caller_cancellation_escapes(shape, native):
    with pytest.raises(asyncio.CancelledError):
        await _call([(b'\x00', True), asyncio.CancelledError()], shape, native=native)


@pytest.mark.asyncio
@pytest.mark.parametrize('shape', SHAPES)
@pytest.mark.parametrize('native', [False, True])
async def test_complete_message_before_disconnect_is_not_a_complete_rpc(shape, native):
    status, seen, _ = await _call([
        (encode_message(b'ok'), True), {'type': 'http.disconnect'}], shape, native=native)
    assert status == b'1'
    assert seen == ([b'ok'] if shape in SHAPES[2:] else [])


@pytest.mark.asyncio
async def test_unary_does_not_copy_the_rest_of_an_extra_message():
    chunk = encode_message(b'ok') + encode_message(b'x' * (512 * 1024))
    tracemalloc.start()
    try:
        status, seen, _ = await _call([(chunk, False)])
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert status == b'12'
    assert seen == []
    # The already allocated transport chunk is outside this measurement.
    assert peak < 64 * 1024


@pytest.mark.asyncio
@pytest.mark.parametrize('shape', SHAPES)
@pytest.mark.parametrize('compressed', [False, True])
@pytest.mark.parametrize('delta', [-1, 0, 1])
async def test_encoded_safety_cap_boundaries(monkeypatch, shape, compressed, delta):
    payload = gzip.compress(b'x' * 16) if compressed else b'x' * 16
    monkeypatch.setattr(grpc_asgi, 'MAX_MESSAGE_LENGTH', len(payload) + delta)
    status, seen, _ = await _call([
        (encode_message(payload, compressed=compressed), False)], shape, encoding=b'gzip')
    assert status == (b'8' if delta < 0 else b'0')
    assert seen == ([] if delta < 0 else [b'x' * 16])


@pytest.mark.asyncio
@pytest.mark.parametrize('shape', SHAPES)
@pytest.mark.parametrize('native', [False, True])
@pytest.mark.parametrize('body', [b'', b'\x00', encode_message(b'ok') + b'\x00'])
async def test_empty_or_partial_extra_message_at_eof(shape, native, body):
    status, seen, _ = await _call([(body, False)], shape, native=native)
    if body:
        assert status == b'13'
        assert seen == ([b'ok'] if shape in SHAPES[2:] and len(body) > 1 else [])
    else:
        assert status == (b'12' if shape in SHAPES[:2] else b'0')
        assert seen == []


@pytest.mark.asyncio
@pytest.mark.parametrize('shape', SHAPES)
@pytest.mark.parametrize('native', [False, True])
async def test_early_rejection_credits_only_consumed_h2_data(shape, native):
    from blackbull.protocol.frame_types import Data, FrameTypes, DataFrameFlags
    from blackbull.server.recipient import HTTP2Recipient

    credits = []

    async def credit(size):
        credits.append(size)

    recipient = HTTP2Recipient(credit_callback=credit, max_body=0, min_rate=0.0)
    for payload, end_stream in [(struct.pack('>BI', 0, 17), False), (b'x' * 17, True)]:
        frame = Data(len(payload), FrameTypes.DATA,
                     DataFrameFlags.END_STREAM if end_stream else 0, 1, data=payload)
        assert recipient.put_DATAFrame(frame)

    async def asgi_receive():
        return await recipient()

    status, seen, _ = await _call([], shape, receiver=recipient if native else asgi_receive)
    assert status == b'8'
    assert seen == []
    assert credits == [5]
    assert recipient.take_uncredited() == 17
    assert recipient.take_uncredited() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('shape', SHAPES)
async def test_oversize_first_prefix_does_not_copy_coalesced_body(shape):
    chunk = struct.pack('>BI', 0, 17) + b'x' * (512 * 1024)
    tracemalloc.start()
    try:
        status, seen, _ = await _call([(chunk, False)], shape)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert status == b'8'
    assert seen == []
    assert peak < 64 * 1024
