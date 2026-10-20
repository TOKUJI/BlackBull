import pytest

import blackbull.grpc.asgi as bridge
from blackbull.grpc import GrpcServiceRegistry, encode_message
from bench.micro.grpc_bridge import measure


@pytest.mark.asyncio
@pytest.mark.parametrize('native', [False, True])
@pytest.mark.parametrize('streaming, request_messages', [(False, 1), (True, 1), (True, 8)])
@pytest.mark.parametrize('chunk_bytes', [0, 1])
async def test_bridge_benchmark_uses_connection(native, streaming, request_messages, chunk_bytes):
    seen, events = [], []

    async def handler(request, context):
        if streaming:
            async for item in request:
                seen.append(item)
        else:
            seen.append(request)
        return b'ok'

    async def send(event):
        events.extend(event.to_asgi())

    registry = GrpcServiceRegistry()
    registry.add_method('/svc/Test', handler, client_streaming=streaming)
    await measure(bridge, registry, 'Test', native, 16, 2, send,
                  request_messages=request_messages, chunk_bytes=chunk_bytes)
    assert seen == [b'x' * 16] * (2 * request_messages)
    assert b''.join(e.get('body', b'') for e in events) == encode_message(b'ok') * 2
    assert [v for e in events for k, v in e.get('headers', [])
            if k == b'grpc-status'] == [b'0', b'0']
