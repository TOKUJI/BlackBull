"""One handler per request, run between one pair of handler events (BLA-684).

The application layer owns ``before_handler``/``after_handler`` for routes and
for gRPC alike; ``after_handler`` reports what the handler raised, ``None``
only on success.
"""
import asyncio

import pytest

from blackbull import BlackBull, Response
from blackbull.grpc import GrpcServiceRegistry
from blackbull.testing.grpc import GrpcTestServer
from blackbull.testing.native import get

pytestmark = pytest.mark.asyncio


def _recording_app():
    app = BlackBull()
    seen = []

    @app.on('before_handler')
    async def before(event):
        seen.append(('before_handler', event.detail['handler'], None))

    @app.on('after_handler')
    async def after(event):
        seen.append(('after_handler', event.detail['handler'], event.detail['exception']))

    return app, seen


async def test_a_cancelled_route_reports_the_cancellation_to_after_handler():
    app, seen = _recording_app()
    started = asyncio.Event()

    @app.route(path='/')
    async def slow(conn, receive, send):
        started.set()
        await asyncio.Event().wait()

    call = asyncio.ensure_future(get(app, '/'))
    await started.wait()
    call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call
    await asyncio.sleep(0)    # observers run as their own tasks
    assert [(name, handler) for name, handler, _ in seen] == [
        ('before_handler', 'slow'), ('after_handler', 'slow')]
    assert isinstance(seen[-1][2], asyncio.CancelledError)


async def test_a_route_that_succeeds_reports_none():
    app, seen = _recording_app()

    @app.route(path='/')
    async def ok(conn, receive, send):
        await send(Response(b'ok'))

    assert (await get(app, '/')).status == 200
    await asyncio.sleep(0)
    assert seen == [('before_handler', 'ok', None), ('after_handler', 'ok', None)]


async def test_a_route_failure_is_logged_before_after_handler_can_raise(caplog):
    app = BlackBull()

    @app.intercept('after_handler')
    async def veto(event):
        raise RuntimeError('interceptor')

    @app.route(path='/')
    async def broken(conn, receive, send):
        raise ValueError('route failure')

    with caplog.at_level('ERROR', logger='blackbull.app'), \
            pytest.raises(RuntimeError, match='interceptor'):
        await get(app, '/')
    assert any('ValueError: route failure' in r.getMessage()
               for r in caplog.records)


async def test_enabling_grpc_again_replaces_the_registry():
    app = BlackBull()
    first, second = GrpcServiceRegistry(), GrpcServiceRegistry()

    @first.method('/svc/Old')
    async def old(request, context):
        return b'old'

    @second.method('/svc/New')
    async def new(request, context):
        return b'new'

    app.enable_grpc(first)
    app.enable_grpc(second)
    async with GrpcTestServer(app) as server:
        assert (await server.unary('/svc/New')).message == b'new'
        assert (await server.unary('/svc/Old')).status.name == 'UNIMPLEMENTED'
