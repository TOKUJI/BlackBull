"""The gRPC bridge emits native, like every other framework-owned producer.

`serve_grpc` is dispatched from `BlackBull._dispatch` — it is internal code on
BlackBull's own send path despite living in a module called `asgi.py`.  It also
runs *before* the handler-boundary adapter (`_wrap_send_native`), so whatever it
emits reaches middleware and the sender unconverted: emitting dicts there put
ASGI shapes on the native seam that nothing had asked for.

The wire contract is unchanged, and these tests say so in both directions — the
seam carries `NativeResponse`, and expanding those objects reproduces exactly
the `http.response.*` sequence gRPC has always put on the wire.
"""
import pytest

from blackbull.grpc.asgi import serve_grpc
from blackbull.grpc.codec import decode_messages, encode_message
from blackbull.grpc.registry import GrpcServiceRegistry
from blackbull.grpc.status import GrpcError, GrpcStatus
from blackbull.native import NativeResponse


def _scope(path):
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


def _seam_collector():
    """Records exactly what crosses the seam, with no normalisation."""
    seen = []

    async def send(event):
        seen.append(event)
    return seen, send


def _as_wire(seen):
    """The ASGI events those native objects stand for."""
    out = []
    for e in seen:
        out.extend(e.to_asgi() if isinstance(e, NativeResponse) else [e])
    return out


async def _drive(reg, path, body=b''):
    seen, send = _seam_collector()
    await serve_grpc(reg, _scope(path), _receive_with(body), send)
    return seen


@pytest.mark.asyncio
async def test_unary_seam_is_native():
    reg = GrpcServiceRegistry()

    @reg.method('/echo.Echo/Echo')
    async def echo(request, context):
        return request[::-1]

    seen = await _drive(reg, '/echo.Echo/Echo', encode_message(b'abc'))

    assert seen and all(isinstance(e, NativeResponse) for e in seen), (
        f'gRPC put non-native events on the seam: '
        f'{[type(e).__name__ for e in seen]}')

    wire = _as_wire(seen)
    assert [e['type'] for e in wire] == [
        'http.response.start', 'http.response.body', 'http.response.trailers']
    assert wire[0]['status'] == 200
    assert wire[0].get('trailers') is True
    assert (b'content-type', b'application/grpc') in wire[0]['headers']
    assert decode_messages(wire[1]['body']) == [(False, b'cba')]
    assert (b'grpc-status', b'0') in wire[2]['headers']


@pytest.mark.asyncio
async def test_trailers_only_error_seam_is_native():
    """An unimplemented method answers Trailers-Only — still native."""
    reg = GrpcServiceRegistry()
    seen = await _drive(reg, '/nope.Svc/Missing', encode_message(b''))

    assert all(isinstance(e, NativeResponse) for e in seen)
    wire = _as_wire(seen)
    trailers = {k: v for e in wire for k, v in e.get('headers', [])}
    assert trailers[b'grpc-status'] == str(int(GrpcStatus.UNIMPLEMENTED)).encode()


@pytest.mark.asyncio
async def test_handler_error_after_start_seam_is_native():
    reg = GrpcServiceRegistry()

    @reg.method('/echo.Echo/Boom')
    async def boom(request, context):
        raise GrpcError(GrpcStatus.INTERNAL, 'nope')

    seen = await _drive(reg, '/echo.Echo/Boom', encode_message(b'x'))

    assert all(isinstance(e, NativeResponse) for e in seen)
    wire = _as_wire(seen)
    trailers = {k: v for e in wire for k, v in e.get('headers', [])}
    assert trailers[b'grpc-status'] == str(int(GrpcStatus.INTERNAL)).encode()


@pytest.mark.asyncio
async def test_server_streaming_seam_is_native():
    reg = GrpcServiceRegistry()

    @reg.method('/echo.Echo/Stream')
    async def stream(request, context):
        for i in range(3):
            yield f'm{i}'.encode()

    seen = await _drive(reg, '/echo.Echo/Stream', encode_message(b'go'))

    assert all(isinstance(e, NativeResponse) for e in seen), (
        f'streaming put non-native events on the seam: '
        f'{[type(e).__name__ for e in seen]}')
    wire = _as_wire(seen)
    assert wire[0]['type'] == 'http.response.start'
    assert wire[-1]['type'] == 'http.response.trailers'
    payload = b''.join(e['body'] for e in wire
                       if e['type'] == 'http.response.body')
    assert decode_messages(payload) == [(False, b'm0'), (False, b'm1'),
                                        (False, b'm2')]
