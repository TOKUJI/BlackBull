"""The gRPC test seam — a documented pattern, not a new client.

`docs/guide/grpc.md` ships all four RPC shapes and had no testing section;
`testing.md` did not mention gRPC at all.  The framework's *own* tests have
always driven gRPC with BlackBull's `HTTP2Client` over a real h2c socket,
because that client folds trailing headers into `res.headers` and every
gRPC response — success and error alike — reports its status there.  The
gap was that this pattern was internal and undocumented, so the path an
application developer found instead was `grpcio`.

BlackBull ships no gRPC *client* and this does not add one.  What it adds
is the boilerplate those internal tests repeat — serve on an ephemeral
port, POST with `content-type: application/grpc`, read the status out of
the trailers — behind one helper, so an app developer can assert on their
own servicer without reconstructing it.
"""
from __future__ import annotations

import pytest

from blackbull import BlackBull
from blackbull.grpc import GrpcError, GrpcServiceRegistry, GrpcStatus, encode_message
from blackbull.testing.grpc import GrpcTestServer

def _app_with(handler, method: str = '/demo.Greeter/SayHello'):
    app = BlackBull()
    registry = GrpcServiceRegistry()
    registry.add_method(method, handler)
    app.enable_grpc(registry)
    return app


@pytest.mark.asyncio
class TestTheSeamDrivesAServicer:
    async def test_a_unary_handler_answers(self):
        async def _hello(request, context):
            return b'hi ' + request

        async with GrpcTestServer(_app_with(_hello)) as grpc:
            reply = await grpc.unary('/demo.Greeter/SayHello', b'world')

        assert reply.status == GrpcStatus.OK
        assert reply.message == b'hi world'
        assert reply.grpc_message == ''

    async def test_a_raised_grpc_error_arrives_as_status_and_message(self):
        """The reason the seam exists: both ride in *trailing* headers.

        An ASGI transport with no `http.response.trailers` support never
        observes them, which is why the framework's own tests moved off
        `TestClient` for gRPC in the first place.
        """
        async def _boom(request, context):
            raise GrpcError(GrpcStatus.NOT_FOUND, 'no such greeting')

        async with GrpcTestServer(_app_with(_boom)) as grpc:
            reply = await grpc.unary('/demo.Greeter/SayHello', b'x')

        assert reply.status == GrpcStatus.NOT_FOUND
        assert reply.grpc_message == 'no such greeting'

    async def test_an_unregistered_method_is_unimplemented(self):
        async def _hello(request, context):  # pragma: no cover - never called
            return b''

        async with GrpcTestServer(_app_with(_hello)) as grpc:
            reply = await grpc.unary('/No.Such/Method', b'')

        assert reply.status == GrpcStatus.UNIMPLEMENTED

    async def test_metadata_reaches_the_handler(self):
        seen: dict = {}

        async def _echo(request, context):
            # A method, and the pairs are bytes — grpcio's spelling, kept.
            seen['meta'] = dict(context.invocation_metadata())
            return b'ok'

        async with GrpcTestServer(_app_with(_echo)) as grpc:
            await grpc.unary('/demo.Greeter/SayHello', b'x',
                             metadata=[('x-tenant', 'acme')])

        assert seen['meta'].get(b'x-tenant') == b'acme'


@pytest.mark.asyncio
class TestItIsNotAClient:
    async def test_the_helper_lives_in_testing_not_client(self):
        """BlackBull ships no gRPC client, and this must not become one."""
        import blackbull.testing.grpc as seam

        assert seam.__name__.startswith('blackbull.testing.')

    async def test_it_reuses_http2client_rather_than_wrapping_a_new_one(self):
        import inspect

        import blackbull.testing.grpc as seam

        src = inspect.getsource(seam)
        assert 'HTTP2Client' in src, 'the seam should reuse the shipped client'
        assert 'class GrpcClient' not in src, 'this must not become a client'

    async def test_the_server_is_reachable_for_a_raw_call(self):
        """Escape hatch: the port is public, so anything can drive it."""
        async def _hello(request, context):
            return b'ok'

        async with GrpcTestServer(_app_with(_hello)) as grpc:
            assert isinstance(grpc.port, int) and grpc.port > 0
            assert grpc.host == '127.0.0.1'


@pytest.mark.asyncio
class TestTheDocumentedExampleRuns:
    """Sprint 107 shipped a guide whose quick-start raised on the first run.

    The guide is where a reader starts, and it fails in their terminal
    rather than in CI — so the example it shows gets executed here.
    """

    async def test_the_grpc_guide_example(self):
        from blackbull.testing.grpc import GrpcTestServer

        async def say_hello(request, context):
            return b'hi ' + request

        app = _app_with(say_hello)

        async with GrpcTestServer(app) as grpc:
            reply = await grpc.unary('/demo.Greeter/SayHello', b'world')

        assert reply.status is GrpcStatus.OK
        assert reply.message == b'hi world'

    async def test_the_error_example_from_the_guide(self):
        from blackbull.testing.grpc import GrpcTestServer

        async def failing(request, context):
            raise GrpcError(GrpcStatus.NOT_FOUND, 'no such greeting')

        async with GrpcTestServer(_app_with(failing)) as grpc:
            reply = await grpc.unary('/demo.Greeter/SayHello', b'x')

        assert reply.status is GrpcStatus.NOT_FOUND
        assert reply.grpc_message == 'no such greeting'

    async def test_every_reply_field_the_guide_documents_exists(self):
        import dataclasses as dc
        from blackbull.testing.grpc import GrpcReply

        documented = {'status', 'grpc_message', 'message', 'messages',
                      'response', 'violation'}
        assert {f.name for f in dc.fields(GrpcReply)} == documented


# --- Judging a reply (gRPC PROTOCOL-HTTP2.md, http-grpc-status-mapping.md) ---

_GRPC_CT = (b'content-type', b'application/grpc')


def _response(*, status: int = 200, head=(_GRPC_CT,), body: bytes = b'',
              trailers=(), ended_on_head: bool = False):
    from blackbull.client.http2 import ClientResponse
    from blackbull.headers import Headers

    return ClientResponse(status=status, headers=Headers(list(head)),
                          body=body, trailers=Headers(list(trailers)),
                          ended_on_head=ended_on_head)


def _judge(response, *, unary: bool | None = None):
    from blackbull.testing.grpc import _read_reply

    return _read_reply(response, unary=unary)


def _status(value: bytes) -> tuple[bytes, bytes]:
    return (b'grpc-status', value)


def _compressed(payload: bytes) -> bytes:
    return b'\x01' + len(payload).to_bytes(4, 'big') + payload


class TestAWellFormedReplyIsReportedAsIs:
    def test_status_zero_with_one_message_is_ok(self):
        reply = _judge(_response(body=encode_message(b'ok'),
                                 trailers=[_status(b'0')]), unary=True)

        assert reply.status is GrpcStatus.OK
        assert reply.messages == (b'ok',)
        assert reply.violation is None

    def test_an_empty_message_is_a_message(self):
        reply = _judge(_response(body=encode_message(b''),
                                 trailers=[_status(b'0')]), unary=True)

        assert reply.status is GrpcStatus.OK
        assert reply.message == b''
        assert reply.messages == (b'',)
        assert reply.violation is None

    def test_a_content_type_with_a_subtype_is_grpc(self):
        reply = _judge(_response(
            head=[(b'content-type', b'application/grpc+proto')],
            body=encode_message(b'ok'), trailers=[_status(b'0')]))

        assert reply.status is GrpcStatus.OK
        assert reply.violation is None

    def test_an_explicit_error_in_a_trailers_only_response(self):
        reply = _judge(_response(head=[_GRPC_CT, _status(b'5'),
                                       (b'grpc-message', b'no%20such')],
                                 ended_on_head=True),
                       unary=True)

        assert reply.status is GrpcStatus.NOT_FOUND
        assert reply.grpc_message == 'no such'
        assert reply.messages == ()
        assert reply.violation is None

    def test_an_explicit_error_in_trailers_after_the_head(self):
        reply = _judge(_response(trailers=[_status(b'13')]), unary=True)

        assert reply.status is GrpcStatus.INTERNAL
        assert reply.violation is None

    def test_a_streaming_reply_may_carry_many_messages(self):
        body = b''.join(encode_message(m) for m in (b'a', b'b', b'c'))
        reply = _judge(_response(body=body, trailers=[_status(b'0')]),
                       unary=False)

        assert reply.status is GrpcStatus.OK
        assert reply.messages == (b'a', b'b', b'c')
        assert reply.violation is None

    def test_an_unknown_call_shape_is_not_judged_on_message_count(self):
        body = encode_message(b'a') + encode_message(b'b')
        reply = _judge(_response(body=body, trailers=[_status(b'0')]))

        assert reply.status is GrpcStatus.OK
        assert reply.violation is None

    def test_a_declared_encoding_with_uncompressed_messages_is_ok(self):
        reply = _judge(_response(head=[_GRPC_CT, (b'grpc-encoding', b'gzip')],
                                 body=encode_message(b'ok'),
                                 trailers=[_status(b'0')]))

        assert reply.status is GrpcStatus.OK
        assert reply.violation is None


class TestAMissingStatusIsSynthesizedFromTheHttpStatus:
    @pytest.mark.parametrize(('http_status', 'expected'), [
        (200, GrpcStatus.UNKNOWN),
        (400, GrpcStatus.INTERNAL),
        (401, GrpcStatus.UNAUTHENTICATED),
        (403, GrpcStatus.PERMISSION_DENIED),
        (404, GrpcStatus.UNIMPLEMENTED),
        (429, GrpcStatus.UNAVAILABLE),
        (502, GrpcStatus.UNAVAILABLE),
        (503, GrpcStatus.UNAVAILABLE),
        (504, GrpcStatus.UNAVAILABLE),
        (500, GrpcStatus.UNKNOWN),
        (418, GrpcStatus.UNKNOWN),
    ])
    def test_the_mapping_table(self, http_status, expected):
        reply = _judge(_response(status=http_status))

        assert reply.status is expected
        assert 'grpc-status' in reply.violation

    def test_a_body_that_ends_without_trailers_has_no_status(self):
        reply = _judge(_response(body=encode_message(b'ok')), unary=True)

        assert reply.status is GrpcStatus.UNKNOWN
        assert 'grpc-status' in reply.violation
        assert reply.messages == (b'ok',)

    def test_a_status_in_a_head_that_did_not_end_the_stream_is_not_used(self):
        """Head, then an empty DATA frame with END_STREAM: no trailers at all."""
        reply = _judge(_response(head=[_GRPC_CT, _status(b'0')]), unary=False)

        assert reply.status is GrpcStatus.UNKNOWN
        assert 'grpc-status' in reply.violation

    def test_a_status_in_the_head_of_a_reply_with_trailers_is_not_used(self):
        reply = _judge(_response(head=[_GRPC_CT, _status(b'0')],
                                 body=encode_message(b'ok'),
                                 trailers=[(b'x-other', b'1')]), unary=True)

        assert reply.status is GrpcStatus.UNKNOWN
        assert 'grpc-status' in reply.violation


class TestAnUnreadableStatusIsUnknown:
    @pytest.mark.parametrize('raw', [b'abc', b'01', b'00', b'', b'-1', b'1.0',
                                     b'17', b'\xd9\xa0'])
    def test_a_value_that_is_not_a_defined_code(self, raw):
        reply = _judge(_response(body=encode_message(b'ok'),
                                 trailers=[_status(raw)]))

        assert reply.status is GrpcStatus.UNKNOWN
        assert 'grpc-status' in reply.violation

    @pytest.mark.parametrize('values', [(b'0', b'0'), (b'0', b'5')])
    def test_a_repeated_status(self, values):
        reply = _judge(_response(body=encode_message(b'ok'),
                                 trailers=[_status(v) for v in values]))

        assert reply.status is GrpcStatus.UNKNOWN
        assert 'grpc-status' in reply.violation


class TestOkIsNeverReportedForABrokenReply:
    def test_a_non_200_http_status_with_status_zero(self):
        reply = _judge(_response(status=503, body=encode_message(b'ok'),
                                 trailers=[_status(b'0')]))

        assert reply.status is GrpcStatus.UNAVAILABLE
        assert '503' in reply.violation

    def test_a_non_200_http_status_keeps_an_explicit_error(self):
        reply = _judge(_response(status=503, head=[_GRPC_CT, _status(b'8')],
                                 ended_on_head=True))

        assert reply.status is GrpcStatus.RESOURCE_EXHAUSTED
        assert '503' in reply.violation

    @pytest.mark.parametrize('head', [
        [(b'content-type', b'text/plain')],
        [],
        [(b'content-type', b'application/grpcx')],
    ])
    def test_a_content_type_that_is_not_grpc(self, head):
        reply = _judge(_response(head=head, body=encode_message(b'ok'),
                                 trailers=[_status(b'0')]))

        assert reply.status is GrpcStatus.UNKNOWN
        assert 'content-type' in reply.violation

    def test_a_content_type_that_is_not_grpc_keeps_an_explicit_error(self):
        reply = _judge(_response(head=[(b'content-type', b'text/html'),
                                       _status(b'5')], ended_on_head=True))

        assert reply.status is GrpcStatus.NOT_FOUND
        assert 'content-type' in reply.violation

    @pytest.mark.parametrize('body', [
        b'\x00\x00',                       # truncated length prefix
        encode_message(b'ok')[:-1],        # message shorter than its prefix
        encode_message(b'ok') + b'\x00',   # trailing partial prefix
    ])
    def test_broken_message_framing(self, body):
        reply = _judge(_response(body=body, trailers=[_status(b'0')]))

        assert reply.status is GrpcStatus.INTERNAL
        assert 'framing' in reply.violation
        assert reply.response.body == body

    def test_broken_framing_keeps_an_explicit_error(self):
        reply = _judge(_response(body=b'\x00\x00', trailers=[_status(b'5')]))

        assert reply.status is GrpcStatus.NOT_FOUND
        assert 'framing' in reply.violation

    @pytest.mark.parametrize('head', [
        [_GRPC_CT],
        [_GRPC_CT, (b'grpc-encoding', b'identity')],
        [_GRPC_CT, (b'grpc-encoding', b'gzip')],
    ])
    def test_a_compressed_message_to_an_identity_only_client(self, head):
        """The helper announces no grpc-accept-encoding (compression.md)."""
        reply = _judge(_response(head=head, body=_compressed(b'ok'),
                                 trailers=[_status(b'0')]))

        assert reply.status is GrpcStatus.INTERNAL
        assert 'compress' in reply.violation


@pytest.mark.parametrize('content_type', [
    b'application/grpc', b'application/grpc+proto', b' application/grpc ',
    b'application/grpcx', b'Application/grpc', b'application/grpc;x=1',
    b'text/plain', b''])
def test_the_helper_accepts_the_content_types_the_server_accepts(content_type):
    """One grammar on both sides: what the server echoes is what the helper reads."""
    from blackbull.grpc.asgi import _resolve_content_type

    reply = _judge(_response(head=[(b'content-type', content_type)],
                             body=encode_message(b'ok'), trailers=[_status(b'0')]))

    server_accepts = _resolve_content_type(content_type) == content_type
    assert (reply.violation is None) is server_accepts


class TestAUnaryReplyCarriesExactlyOneMessage:
    @pytest.mark.parametrize('body', [b'', encode_message(b'a') + encode_message(b'b')])
    def test_status_zero_with_another_message_count(self, body):
        reply = _judge(_response(body=body, trailers=[_status(b'0')]),
                       unary=True)

        assert reply.status is GrpcStatus.INTERNAL
        assert 'message' in reply.violation

    def test_an_explicit_error_needs_no_message(self):
        reply = _judge(_response(trailers=[_status(b'9')]), unary=True)

        assert reply.status is GrpcStatus.FAILED_PRECONDITION
        assert reply.violation is None


class TestTheStatusMessageIsPercentDecoded:
    @pytest.mark.parametrize(('raw', 'text'), [
        (b'plain', 'plain'),
        (b'no%20such', 'no such'),
        (b'%E3%81%82', 'あ'),
        (b'100%', '100%'),
        (b'%zz', '%zz'),
        (b'%4', '%4'),
        (b'%FF', '�'),
    ])
    def test_invalid_encodings_are_kept_not_raised(self, raw, text):
        reply = _judge(_response(head=[_GRPC_CT, _status(b'5'),
                                       (b'grpc-message', raw)],
                                 ended_on_head=True))

        assert reply.grpc_message == text


# --- Positive controls: the same judgements through the real server ---

def _raw_app(path: str, events):
    """An app whose route answers with exactly *events*, gRPC or not."""
    app = BlackBull()

    @app.route(path=path, methods=['POST'])
    async def _raw(scope, receive, send):
        for event in events:
            await send(event)

    return app


def _start(status: int = 200, content_type: bytes = b'application/grpc',
           trailers: bool = False) -> dict:
    return {'type': 'http.response.start', 'status': status,
            'headers': [(b'content-type', content_type)], 'trailers': trailers}


def _body(data: bytes) -> dict:
    return {'type': 'http.response.body', 'body': data, 'more_body': False}


def _trailers(*fields) -> dict:
    return {'type': 'http.response.trailers', 'headers': list(fields),
            'more_trailers': False}


@pytest.mark.asyncio
class TestAnomaliesAreCaughtThroughTheRealServer:
    async def test_a_reply_without_status(self):
        app = _raw_app('/demo.Raw/Call', [_start(), _body(encode_message(b'x'))])

        async with GrpcTestServer(app) as grpc:
            reply = await grpc.unary('/demo.Raw/Call', b'x')

        assert reply.status is GrpcStatus.UNKNOWN
        assert 'grpc-status' in reply.violation

    async def test_a_plain_http_error(self):
        app = _raw_app('/demo.Raw/Call',
                       [_start(503, b'text/plain'), _body(b'busy')])

        async with GrpcTestServer(app) as grpc:
            reply = await grpc.unary('/demo.Raw/Call', b'x')

        assert reply.status is GrpcStatus.UNAVAILABLE
        assert reply.violation is not None

    async def test_a_status_in_a_head_that_did_not_end_the_stream(self):
        """The server sends the head, then an empty DATA frame with END_STREAM."""
        start = _start()
        start['headers'].append((b'grpc-status', b'0'))
        app = _raw_app('/demo.Raw/Call', [start, _body(b'')])

        async with GrpcTestServer(app) as grpc:
            reply = await grpc.unary('/demo.Raw/Call', b'x')

        assert reply.response.ended_on_head is False
        assert reply.status is GrpcStatus.UNKNOWN
        assert 'grpc-status' in reply.violation

    async def test_status_zero_after_a_truncated_message(self):
        app = _raw_app('/demo.Raw/Call', [
            _start(trailers=True), _body(encode_message(b'xyz')[:-1]),
            _trailers((b'grpc-status', b'0'))])

        async with GrpcTestServer(app) as grpc:
            reply = await grpc.unary('/demo.Raw/Call', b'x')

        assert reply.status is GrpcStatus.INTERNAL
        assert 'framing' in reply.violation

    async def test_the_status_message_round_trips_through_the_server(self):
        details = '100% あ\n"quoted"\tend'

        async def _fail(request, context):
            raise GrpcError(GrpcStatus.NOT_FOUND, details)

        async with GrpcTestServer(_app_with(_fail)) as grpc:
            reply = await grpc.unary('/demo.Greeter/SayHello', b'x')

        assert reply.status is GrpcStatus.NOT_FOUND
        assert reply.grpc_message == details

    async def test_the_call_shape_comes_from_the_registry(self):
        """A streaming method may answer many messages, a unary one only one."""
        async def _many(request, context):
            for part in (b'a', b'b', b'c'):
                yield part

        async def _one(request, context):
            return b'one'

        app = BlackBull()
        registry = GrpcServiceRegistry()
        registry.add_method('/demo.Svc/Many', _many)
        registry.add_method('/demo.Svc/One', _one)
        app.enable_grpc(registry)

        async with GrpcTestServer(app) as grpc:
            many = await grpc.unary('/demo.Svc/Many', b'x')
            one = await grpc.unary('/demo.Svc/One', b'x')

        assert (many.status, many.messages, many.violation) == (
            GrpcStatus.OK, (b'a', b'b', b'c'), None)
        assert (one.status, one.messages, one.violation) == (
            GrpcStatus.OK, (b'one',), None)
