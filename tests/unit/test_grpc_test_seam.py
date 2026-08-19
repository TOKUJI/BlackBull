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
from blackbull.grpc import GrpcError, GrpcServiceRegistry, GrpcStatus
from blackbull.testing.grpc import GrpcTestServer

pytestmark = pytest.mark.asyncio


def _app_with(handler, method: str = '/demo.Greeter/SayHello'):
    app = BlackBull()
    registry = GrpcServiceRegistry()
    registry.add_method(method, handler)
    app.enable_grpc(registry)
    return app


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

        documented = {'status', 'grpc_message', 'message', 'messages', 'response'}
        assert {f.name for f in dc.fields(GrpcReply)} == documented
