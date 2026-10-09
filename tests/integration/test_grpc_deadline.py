import asyncio
from collections import Counter

from hpack import Decoder
import pytest

from blackbull import BlackBull
from blackbull.grpc import GrpcServiceRegistry, encode_message
from blackbull.protocol.frame_types import FrameTypes
from tests.conformance.http2._harness import _make_h2_frame, _make_headers_frame
from tests.integration.test_grpc import _serve


pytestmark = pytest.mark.integration
SHAPES = ('unary', 'server-streaming', 'client-streaming', 'bidi')


@pytest.mark.asyncio
@pytest.mark.parametrize('force_asgi', [False, True])
@pytest.mark.parametrize('shape', SHAPES)
@pytest.mark.parametrize('stage', ['input', 'handler', 'output'])
async def test_deadline_and_next_rpc_share_h2_connection(monkeypatch, force_asgi, shape, stage):
    monkeypatch.setenv('BB_FORCE_ASGI_SCOPE', '1' if force_asgi else '0')
    app = BlackBull()
    registry = GrpcServiceRegistry()
    parked = asyncio.Event()
    closed = []
    lifecycle = Counter()
    errors = {}
    handler_events = {}
    completed = {path: asyncio.Event() for path in ('/svc/M', '/svc/OK')}

    def observer(name):
        async def record(event):
            path = event.detail['path']
            lifecycle[path, name] += 1
            if name in ('before_handler', 'after_handler'):
                handler_events[path, name] = event.detail
                if name == 'after_handler':
                    errors[path] = event.detail['exception']
            if name == 'request_completed':
                completed[path].set()
        return record

    event_names = ('request_received', 'before_handler', 'after_handler', 'request_completed')
    for name in event_names:
        app.on(name, blocking=True)(observer(name))

    async def work(request, context):
        try:
            if shape in ('client-streaming', 'bidi'):
                async for _ in request:
                    pass
            if stage == 'handler':
                await parked.wait()
            return b'x' * 32
        finally:
            closed.append(True)

    async def unary(request, context):
        return await work(request, context)

    async def streaming(request, context):
        yield await work(request, context)

    async def success(request, context):
        return b''

    registry.add_method('/svc/M', streaming if shape in ('server-streaming', 'bidi')
                        else unary, client_streaming=shape in ('client-streaming', 'bidi'))
    registry.add_method('/svc/OK', success)
    app.enable_grpc(registry)

    async with _serve(app) as port:
        reader, writer = await asyncio.open_connection('127.0.0.1', port)
        decoder = Decoder()

        async def terminal(stream_id):
            statuses = []
            async with asyncio.timeout(1):
                while True:
                    head = await reader.readexactly(9)
                    payload = await reader.readexactly(int.from_bytes(head[:3], 'big'))
                    kind, flags = head[3], head[4]
                    sid = int.from_bytes(head[5:], 'big')
                    if kind == 4 and not flags & 1:
                        writer.write(_make_h2_frame(FrameTypes.SETTINGS, 1))
                        await writer.drain()
                    if kind == 1:
                        fields = decoder.decode(payload, raw=True)
                        if sid == stream_id:
                            statuses.extend(v for k, v in fields if k == b'grpc-status')
                    if sid == stream_id and (kind == 3 or kind in (0, 1) and flags & 1):
                        return kind, statuses, int.from_bytes(payload, 'big') if kind == 3 else None

        def headers(sid, path, timeout=None):
            fields = [(b':method', b'POST'), (b':scheme', b'http'),
                      (b':authority', b'localhost'), (b':path', path),
                      (b'content-type', b'application/grpc')]
            if timeout is not None:
                fields.append((b'grpc-timeout', timeout))
            return _make_headers_frame(sid, fields=fields)

        try:
            settings = b'\x00\x04\x00\x00\x00\x00' if stage == 'output' else b''
            writer.write(b'PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n'
                         + _make_h2_frame(FrameTypes.SETTINGS, payload=settings)
                         + headers(1, b'/svc/M', b'30m')
                         + _make_h2_frame(FrameTypes.DATA, 0 if stage == 'input' else 1,
                                          1, encode_message(b'x')[:3] if stage == 'input'
                                          else encode_message(b'x')))
            await writer.drain()
            kind, statuses, reset_code = await terminal(1)
            assert statuses == [b'4'] or stage == 'output' and kind == 3 and reset_code == 2
            if stage == 'output':
                writer.write(_make_h2_frame(FrameTypes.SETTINGS,
                                           payload=b'\x00\x04\x00\x00\xff\xff'))
            writer.write(headers(3, b'/svc/OK')
                         + _make_h2_frame(FrameTypes.DATA, 1, 3, encode_message(b'')))
            await writer.drain()
            kind, statuses, _ = await terminal(3)
            assert kind == 1 and statuses == [b'0']
            async with asyncio.timeout(1):
                await asyncio.gather(*(event.wait() for event in completed.values()))
            assert lifecycle == Counter({(path, name): 1 for path in completed for name in event_names})
            assert all(detail['handler'] == 'serve_grpc' for detail in handler_events.values())
            assert all('exception' not in handler_events[path, 'before_handler'] for path in completed)
            assert errors['/svc/OK'] is None
            if reset_code is not None:
                assert isinstance(errors['/svc/M'], TimeoutError)
            else:
                assert errors['/svc/M'] is None
        finally:
            writer.close()
            await writer.wait_closed()
    if stage != 'input' or shape in ('client-streaming', 'bidi'):
        assert closed
