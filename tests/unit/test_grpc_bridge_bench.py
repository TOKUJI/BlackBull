import asyncio
import gc
from types import SimpleNamespace
import weakref

import pytest

import blackbull.grpc.asgi as bridge
from blackbull.grpc import GrpcServiceRegistry, encode_message
from bench.micro.grpc_bridge import measure
from bench.micro import grpc_bridge


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


@pytest.mark.asyncio
async def test_bridge_samples_do_not_carry_queued_work_into_the_next_sample(monkeypatch):
    pending = set()
    references = []

    class Payload:
        pass

    async def observed(*args, **kwargs):
        assert not pending, 'queued callbacks from the previous sample remain'
        assert all(ref() is None for ref in references), 'previous sample garbage remains'
        result = await measure(*args, **kwargs)
        token = Payload()
        token.cycle = token
        references.append(weakref.ref(token))
        pending.add(token)
        asyncio.get_running_loop().call_soon(pending.remove, token)
        return result

    monkeypatch.setattr(grpc_bridge, 'measure', observed)
    monkeypatch.setattr(grpc_bridge.os, 'sched_setaffinity', lambda *_: None)
    args = SimpleNamespace(null=True, baseline='HEAD', shapes=['Server'],
                           messages=1, yield_every=0, request_messages=1,
                           chunk_bytes=0, timeout=None, sizes=[16], warmup=1,
                           rounds=1, calls=1)
    enabled = gc.isenabled()
    try:
        await grpc_bridge.main(args)
        assert not pending
        assert all(ref() is None for ref in references)
    finally:
        if enabled:
            gc.enable()
