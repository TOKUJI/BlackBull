"""Unit + ASGI-bridge tests for BlackBull's unary gRPC support."""
import pytest

from blackbull import BlackBull
from blackbull.grpc import (
    GrpcServiceRegistry, GrpcStatus, GrpcError, GrpcDecodeError,
    encode_message, decode_messages,
)
from blackbull.grpc.asgi import serve_grpc, GrpcContext, _pct_encode_message


# ---------------------------------------------------------------------------
# Codec
# ---------------------------------------------------------------------------

class TestCodec:
    def test_encode_prefix_shape(self):
        framed = encode_message(b'hello')
        assert framed[0] == 0                       # uncompressed flag
        assert framed[1:5] == (5).to_bytes(4, 'big')  # big-endian length
        assert framed[5:] == b'hello'

    def test_encode_empty_message(self):
        framed = encode_message(b'')
        assert framed == b'\x00\x00\x00\x00\x00'

    def test_round_trip_single(self):
        framed = encode_message(b'payload')
        assert decode_messages(framed) == [(False, b'payload')]

    def test_round_trip_multiple(self):
        buf = encode_message(b'one') + encode_message(b'two') + encode_message(b'')
        assert decode_messages(buf) == [(False, b'one'), (False, b'two'), (False, b'')]

    def test_empty_buffer_yields_no_messages(self):
        assert decode_messages(b'') == []

    def test_compressed_flag_reported(self):
        assert decode_messages(encode_message(b'x', compressed=True)) == [(True, b'x')]

    def test_truncated_prefix_raises(self):
        with pytest.raises(GrpcDecodeError):
            decode_messages(b'\x00\x00\x00')          # < 5 prefix bytes

    def test_truncated_body_raises(self):
        # Declares 10-byte body but supplies 3.
        with pytest.raises(GrpcDecodeError):
            decode_messages(b'\x00' + (10).to_bytes(4, 'big') + b'abc')


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

class TestRegistry:
    def test_add_and_lookup(self):
        reg = GrpcServiceRegistry()

        async def h(request, context):
            return b''

        reg.add_method('/pkg.Svc/M', h)
        assert reg.lookup('/pkg.Svc/M') is h

    def test_lookup_normalises_leading_slash(self):
        reg = GrpcServiceRegistry()

        async def h(request, context):
            return b''

        reg.add_method('pkg.Svc/M', h)            # no leading slash
        assert reg.lookup('/pkg.Svc/M') is h

    def test_decorator(self):
        reg = GrpcServiceRegistry()

        @reg.method('/pkg.Svc/Deco')
        async def h(request, context):
            return b''

        assert reg.lookup('/pkg.Svc/Deco') is h

    def test_duplicate_raises(self):
        reg = GrpcServiceRegistry()

        async def h(request, context):
            return b''

        reg.add_method('/a/b', h)
        with pytest.raises(ValueError):
            reg.add_method('/a/b', h)

    def test_add_service(self):
        reg = GrpcServiceRegistry()

        async def hello(request, context):
            return b''

        reg.add_service('helloworld.Greeter', {'SayHello': hello})
        assert reg.lookup('/helloworld.Greeter/SayHello') is hello

    def test_unknown_lookup_returns_none(self):
        assert GrpcServiceRegistry().lookup('/nope/nope') is None


# ---------------------------------------------------------------------------
# serve_grpc — ASGI bridge harness
# ---------------------------------------------------------------------------

def _grpc_scope(path, headers=None):
    base = [(b'content-type', b'application/grpc'), (b':method', b'POST')]
    return {'type': 'http', 'path': path,
            'headers': headers if headers is not None else base}


def _receive_with(body: bytes):
    sent = False

    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {'type': 'http.request', 'body': body, 'more_body': False}
        return {'type': 'http.request', 'body': b'', 'more_body': False}
    return receive


def _collector():
    events = []

    async def send(event):
        events.append(event)
    return events, send


def _trailers_of(events):
    """Return the grpc trailers as a dict, whether reported in the
    trailers event or (for trailers-only errors) the start headers."""
    out = {}
    for e in events:
        if e['type'] in ('http.response.start', 'http.response.trailers'):
            for k, v in e['headers']:
                out[k] = v
    return out


@pytest.mark.asyncio
async def test_unary_success():
    reg = GrpcServiceRegistry()

    @reg.method('/echo.Echo/Echo')
    async def echo(request, context):
        return request[::-1]

    events, send = _collector()
    await serve_grpc(reg, _grpc_scope('/echo.Echo/Echo'),
                     _receive_with(encode_message(b'abc')), send)

    start = events[0]
    assert start['type'] == 'http.response.start'
    assert start['status'] == 200
    assert (b'content-type', b'application/grpc') in start['headers']
    assert start.get('trailers') is True

    body = events[1]
    assert body['type'] == 'http.response.body'
    assert decode_messages(body['body']) == [(False, b'cba')]

    trailers = events[2]
    assert trailers['type'] == 'http.response.trailers'
    assert (b'grpc-status', b'0') in trailers['headers']


@pytest.mark.asyncio
async def test_unimplemented_method():
    reg = GrpcServiceRegistry()
    events, send = _collector()
    await serve_grpc(reg, _grpc_scope('/nope.Svc/Missing'),
                     _receive_with(encode_message(b'')), send)
    assert _trailers_of(events)[b'grpc-status'] == str(int(GrpcStatus.UNIMPLEMENTED)).encode()


@pytest.mark.asyncio
async def test_handler_raises_grpc_error():
    reg = GrpcServiceRegistry()

    @reg.method('/x/Fail')
    async def fail(request, context):
        # Newline (0x0A) is below 0x20 so it is percent-encoded; the spaces
        # (0x20, in the unreserved 0x20-0x7E range) pass through per the
        # gRPC grpc-message encoding rules.
        raise GrpcError(GrpcStatus.NOT_FOUND, 'no such user\n')

    events, send = _collector()
    await serve_grpc(reg, _grpc_scope('/x/Fail'),
                     _receive_with(encode_message(b'')), send)
    tr = _trailers_of(events)
    assert tr[b'grpc-status'] == str(int(GrpcStatus.NOT_FOUND)).encode()
    assert tr[b'grpc-message'] == b'no such user%0A'


@pytest.mark.asyncio
async def test_context_abort():
    reg = GrpcServiceRegistry()

    @reg.method('/x/Abort')
    async def abort(request, context):
        context.abort(GrpcStatus.PERMISSION_DENIED, 'denied')
        return b''  # unreachable

    events, send = _collector()
    await serve_grpc(reg, _grpc_scope('/x/Abort'),
                     _receive_with(encode_message(b'')), send)
    assert _trailers_of(events)[b'grpc-status'] == \
        str(int(GrpcStatus.PERMISSION_DENIED)).encode()


@pytest.mark.asyncio
async def test_handler_unexpected_exception_becomes_internal():
    reg = GrpcServiceRegistry()

    @reg.method('/x/Boom')
    async def boom(request, context):
        raise RuntimeError('kaboom')

    events, send = _collector()
    await serve_grpc(reg, _grpc_scope('/x/Boom'),
                     _receive_with(encode_message(b'')), send)
    assert _trailers_of(events)[b'grpc-status'] == str(int(GrpcStatus.INTERNAL)).encode()


@pytest.mark.asyncio
async def test_handler_can_read_metadata_and_set_custom_status():
    reg = GrpcServiceRegistry()

    @reg.method('/x/Meta')
    async def meta(request, context):
        assert context.metadata(b'x-token') == b'secret'
        context.set_code(GrpcStatus.OK)
        context.set_trailing_metadata([(b'x-trailer', b'1')])
        return b'ok'

    headers = [(b'content-type', b'application/grpc'), (b'x-token', b'secret')]
    events, send = _collector()
    await serve_grpc(reg, _grpc_scope('/x/Meta', headers),
                     _receive_with(encode_message(b'')), send)
    trailers = events[2]['headers']
    assert (b'grpc-status', b'0') in trailers
    assert (b'x-trailer', b'1') in trailers


@pytest.mark.asyncio
async def test_two_messages_is_unimplemented_for_unary():
    reg = GrpcServiceRegistry()

    @reg.method('/x/Unary')
    async def unary(request, context):
        return b''

    body = encode_message(b'a') + encode_message(b'b')
    events, send = _collector()
    await serve_grpc(reg, _grpc_scope('/x/Unary'), _receive_with(body), send)
    assert _trailers_of(events)[b'grpc-status'] == str(int(GrpcStatus.UNIMPLEMENTED)).encode()


def test_pct_encode_message():
    # Printable ASCII 0x20-0x7E (including space) passes through unchanged;
    # only '%' and out-of-range bytes are encoded (matches grpc-go).
    assert _pct_encode_message('hello world') == b'hello world'
    assert _pct_encode_message('100%') == b'100%25'
    assert _pct_encode_message('tab\tend') == b'tab%09end'
    assert _pct_encode_message('café') == b'caf%C3%A9'  # UTF-8 then percent-encode


# ---------------------------------------------------------------------------
# Integration through the app dispatch path
# ---------------------------------------------------------------------------

class TestAppDispatch:
    @pytest.mark.asyncio
    async def test_enable_grpc_routes_grpc_requests(self):
        app = BlackBull()
        reg = GrpcServiceRegistry()

        @reg.method('/demo.Svc/Double')
        async def double(request, context):
            return request + request

        app.enable_grpc(reg)

        events, send = _collector()
        scope = {'type': 'http', 'method': 'POST', 'path': '/demo.Svc/Double',
                 'headers': [(b'content-type', b'application/grpc')],
                 'client': ('127.0.0.1', 1234)}
        await app(scope, _receive_with(encode_message(b'hi')), send)

        body = next(e for e in events if e['type'] == 'http.response.body')
        assert decode_messages(body['body']) == [(False, b'hihi')]

    @pytest.mark.asyncio
    async def test_non_grpc_request_still_routes_to_http_handler(self):
        app = BlackBull()
        reg = GrpcServiceRegistry()
        app.enable_grpc(reg)

        @app.route(path='/health')
        async def health():
            return 'ok'

        app._router.validate()

        events, send = _collector()
        scope = {'type': 'http', 'method': 'GET', 'path': '/health',
                 'headers': [(b'accept', b'text/plain')],
                 'client': ('127.0.0.1', 1234)}
        await app(scope, _receive_with(b''), send)

        start = next(e for e in events if e['type'] == 'http.response.start')
        assert start['status'] == 200
