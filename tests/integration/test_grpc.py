"""Integration tests for gRPC via BlackBull's full ASGI dispatch path.

Each test serves a real ``ASGIServer`` on an ephemeral h2c port and drives
it with BlackBull's own ``HTTP2Client`` — verifying that gRPC requests flow
through the whole app (``BlackBull.__call__`` → ``_dispatch`` →
``serve_grpc``) over a real socket.

.. note::

   These tests originally ran in-process over ``TestClient``
   (``httpx.ASGITransport``).  That transport has no support for the
   ``http.response.trailers`` ASGI event, and since Sprint 58 every gRPC
   response — success *and* error — reports its status in **trailing
   headers** (the framing real gRPC clients require).  httpx therefore never
   observed response completion and its transport asserted
   (``response_complete.is_set()``) on all gRPC calls — bug 1.23 in the
   2026-07-07 audit.  ``HTTP2Client`` handles trailers natively, and folds
   them into ``res.headers``.

   Pure-REST assertions (no trailers on the wire) still use ``TestClient``.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest

from blackbull import BlackBull
from blackbull.grpc import (
    GrpcServiceRegistry, GrpcStatus, GrpcError,
    encode_message,
)
from blackbull.client.http2 import HTTP2Client
from blackbull.server.server import ASGIServer
from blackbull.testing import TestClient


# Tag all tests in this file as integration tests.
pytestmark = pytest.mark.integration

_SERVER_STARTUP_WAIT_SECONDS = 0.15


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_grpc_app() -> tuple[BlackBull, GrpcServiceRegistry]:
    """Create a minimal BlackBull app with gRPC support."""
    app = BlackBull()
    reg = GrpcServiceRegistry()
    return app, reg


@asynccontextmanager
async def _serve(app: BlackBull):
    """Serve *app* on an ephemeral h2c port; yield the port."""
    server = ASGIServer(app)
    server.open_socket(port=0)
    task = asyncio.create_task(server.run())
    await asyncio.sleep(_SERVER_STARTUP_WAIT_SECONDS)
    try:
        yield server.port
    finally:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


async def _grpc_call(port: int, path: str, payload: bytes,
                     headers: list[tuple[str, str]] | None = None):
    """Send one gRPC-style POST over a real h2c socket.

    Returns the ``ClientResponse``; gRPC status/message ride in
    ``res.headers`` (trailing headers are folded in by ``HTTP2Client``).
    """
    hdrs = [('content-type', 'application/grpc')] + (headers or [])
    async with HTTP2Client('127.0.0.1', port) as c:
        return await c.request('POST', path, headers=hdrs, body=payload)


def _grpc_status(res) -> str:
    return res.headers.get(b'grpc-status', b'').decode()


def _grpc_message(res) -> str:
    return res.headers.get(b'grpc-message', b'').decode()


# ---------------------------------------------------------------------------
# §1 — Dispatch routing: gRPC requests are correctly intercepted
# ---------------------------------------------------------------------------

class TestGrpcDispatchRouting:
    """Verify that ``BlackBull._dispatch`` routes ``application/grpc``
    requests to the gRPC bridge (not the HTTP router)."""

    @pytest.mark.asyncio
    async def test_grpc_content_type_triggers_grpc_path(self):
        """POST with ``Content-Type: application/grpc`` → UNIMPLEMENTED
        (if not registered) rather than 404 from HTTP router."""
        app, reg = _make_grpc_app()
        app.enable_grpc(reg)

        async with _serve(app) as port:
            res = await _grpc_call(port, '/some/grpc/method', encode_message(b''))
        assert res.status == 200
        assert _grpc_status(res) == str(int(GrpcStatus.UNIMPLEMENTED))

    @pytest.mark.asyncio
    async def test_unregistered_method_returns_unimplemented(self):
        """POST to a path with no registered gRPC handler → UNIMPLEMENTED (12)."""
        app, reg = _make_grpc_app()
        app.enable_grpc(reg)

        async with _serve(app) as port:
            res = await _grpc_call(port, '/No.Such/Method', encode_message(b''))
        assert res.status == 200
        assert _grpc_status(res) == str(int(GrpcStatus.UNIMPLEMENTED))

    def test_non_grpc_post_without_content_type_is_rest(self):
        """A POST without ``application/grpc`` content-type must go through
        the HTTP router, not the gRPC bridge."""
        app, reg = _make_grpc_app()

        @reg.method('/svc/Only')
        async def only_grpc(request, context):
            return b'grpc'  # pragma: no cover — should not be reached

        app.enable_grpc(reg)

        with TestClient(app) as client:
            resp = client.post('/svc/Only', content=b'hello')
            assert 'grpc-status' not in dict(resp.headers)
            # No REST route for POST /svc/Only → 405 or 404
            assert resp.status_code in (404, 405)

    @pytest.mark.asyncio
    async def test_content_type_with_charset_is_handled(self):
        """``Content-Type: application/grpc; charset=utf-8`` should match
        the ``startswith(b'application/grpc')`` check in ``_dispatch``."""
        app, reg = _make_grpc_app()
        app.enable_grpc(reg)

        async with _serve(app) as port:
            hdrs = [('content-type', 'application/grpc; charset=utf-8')]
            async with HTTP2Client('127.0.0.1', port) as c:
                res = await c.request('POST', '/No.Such/Method',
                                      headers=hdrs, body=encode_message(b''))
        assert res.status == 200
        assert _grpc_status(res) == str(int(GrpcStatus.UNIMPLEMENTED))


# ---------------------------------------------------------------------------
# §2 — Error handling: all error paths report grpc-status in trailers
# ---------------------------------------------------------------------------

class TestGrpcErrorDispatch:
    """gRPC errors must produce HTTP 200 with grpc-status in the trailing
    headers — never HTTP 4xx/5xx."""

    @pytest.mark.asyncio
    async def test_grpc_error_returns_200_with_grpc_status(self):
        app, reg = _make_grpc_app()

        @reg.method('/svc/Fail')
        async def fail(request, context):
            raise GrpcError(GrpcStatus.RESOURCE_EXHAUSTED, 'overloaded')

        app.enable_grpc(reg)

        async with _serve(app) as port:
            res = await _grpc_call(port, '/svc/Fail', encode_message(b''))
        assert res.status == 200
        assert _grpc_status(res) == str(int(GrpcStatus.RESOURCE_EXHAUSTED))

    @pytest.mark.asyncio
    async def test_handler_exception_returns_internal(self):
        app, reg = _make_grpc_app()

        @reg.method('/svc/Boom')
        async def boom(request, context):
            raise RuntimeError('unexpected failure')

        app.enable_grpc(reg)

        async with _serve(app) as port:
            res = await _grpc_call(port, '/svc/Boom', encode_message(b''))
        assert res.status == 200
        assert _grpc_status(res) == str(int(GrpcStatus.INTERNAL))
        assert 'unexpected failure' in _grpc_message(res)

    @pytest.mark.asyncio
    async def test_invalid_message_body_returns_internal(self):
        app, reg = _make_grpc_app()

        @reg.method('/svc/Valid')
        async def valid(request, context):
            return b'ok'

        app.enable_grpc(reg)

        async with _serve(app) as port:
            res = await _grpc_call(port, '/svc/Valid', b'not a valid grpc frame')
        assert res.status == 200
        assert _grpc_status(res) == str(int(GrpcStatus.INTERNAL))

    @pytest.mark.asyncio
    async def test_context_abort_produces_correct_status(self):
        app, reg = _make_grpc_app()

        @reg.method('/svc/Abort')
        async def abort_handler(request, context):
            context.abort(GrpcStatus.PERMISSION_DENIED, 'nope')
            return b''  # unreachable

        app.enable_grpc(reg)

        async with _serve(app) as port:
            res = await _grpc_call(port, '/svc/Abort', encode_message(b''))
        assert res.status == 200
        assert _grpc_status(res) == str(int(GrpcStatus.PERMISSION_DENIED))

    @pytest.mark.asyncio
    async def test_all_17_status_codes_are_wireable_via_integration(self):
        """For each canonical gRPC status code, verify the correct
        grpc-status trailer is emitted through the full dispatch path.

        One app/server hosts a method per code — starting 17 servers
        sequentially would dominate the suite's runtime.
        """
        app = BlackBull()
        reg = GrpcServiceRegistry()
        for code in GrpcStatus:
            @reg.method(f'/svc/Code{code.value}')
            async def h(request, context, _code=code):
                raise GrpcError(_code, f'status {_code.value}')
        app.enable_grpc(reg)

        async with _serve(app) as port:
            for code in GrpcStatus:
                res = await _grpc_call(port, f'/svc/Code{code.value}',
                                       encode_message(b''))
                assert _grpc_status(res) == str(int(code)), (
                    f'GrpcStatus.{code.name} ({code.value}) failed')

    @pytest.mark.asyncio
    async def test_empty_body_zero_messages_returns_error(self):
        app, reg = _make_grpc_app()

        @reg.method('/svc/Unary')
        async def unary(request, context):
            return b'ok'

        app.enable_grpc(reg)

        async with _serve(app) as port:
            res = await _grpc_call(port, '/svc/Unary', b'')
        assert res.status == 200
        assert _grpc_status(res) in (
            str(int(GrpcStatus.INTERNAL)),
            str(int(GrpcStatus.UNIMPLEMENTED)),
        )

    @pytest.mark.asyncio
    async def test_compressed_message_rejected_via_integration(self):
        app, reg = _make_grpc_app()

        @reg.method('/svc/Unary')
        async def unary(request, context):
            return b'ok'

        app.enable_grpc(reg)

        async with _serve(app) as port:
            res = await _grpc_call(port, '/svc/Unary',
                                   encode_message(b'data', compressed=True))
        assert res.status == 200
        assert _grpc_status(res) == str(int(GrpcStatus.UNIMPLEMENTED))


# ---------------------------------------------------------------------------
# §3 — REST + gRPC coexistence
# ---------------------------------------------------------------------------

class TestRestAndGrpcCoexistence:
    """Verify that REST routes and gRPC methods coexist without interference."""

    def test_rest_route_still_works_when_grpc_enabled(self):
        app, reg = _make_grpc_app()

        @app.route(path='/api/hello')
        async def rest_hello():
            return 'Hello from REST'

        app.enable_grpc(reg)

        with TestClient(app) as client:
            resp = client.get('/api/hello')
            assert resp.status_code == 200
            assert resp.text == 'Hello from REST'

    @pytest.mark.asyncio
    async def test_grpc_content_type_to_root_returns_unimplemented(self):
        app, reg = _make_grpc_app()

        @app.route(path='/')
        async def index():
            return 'home'

        app.enable_grpc(reg)

        async with _serve(app) as port:
            async with HTTP2Client('127.0.0.1', port) as c:
                # Normal REST GET — works
                rest = await c.request('GET', '/')
                # POST with application/grpc → gRPC path → UNIMPLEMENTED
                res = await c.request(
                    'POST', '/',
                    headers=[('content-type', 'application/grpc')],
                    body=encode_message(b''))

        assert rest.status == 200
        assert rest.body == b'home'
        assert res.status == 200
        assert _grpc_status(res) == str(int(GrpcStatus.UNIMPLEMENTED))

    def test_grpc_disabled_does_not_crash(self):
        app = BlackBull()

        @app.route(path='/svc/Something', methods=['POST'])
        async def rest_handler(scope, receive, send):
            await send({'type': 'http.response.start', 'status': 200,
                        'headers': [(b'content-type', b'text/plain')]})
            await send({'type': 'http.response.body', 'body': b'rest ok'})

        # Do NOT call enable_grpc

        with TestClient(app) as client:
            resp = client.post('/svc/Something',
                               content=encode_message(b''),
                               headers={'Content-Type': 'application/grpc'})
            assert resp.status_code == 200
            assert resp.text == 'rest ok'
            assert 'grpc-status' not in dict(resp.headers)


# ---------------------------------------------------------------------------
# §4 — Metadata and context through dispatch
# ---------------------------------------------------------------------------

class TestGrpcMetadataDispatch:
    """Request metadata (HTTP headers) accessible via GrpcContext through
    the full dispatch path."""

    @pytest.mark.asyncio
    async def test_request_header_accessible_via_context_metadata(self):
        app, reg = _make_grpc_app()

        @reg.method('/svc/MetaEcho')
        async def meta_echo(request, context):
            token = context.metadata(b'x-token', b'no-token')
            raise GrpcError(GrpcStatus.OK, f'token={token.decode()}')

        app.enable_grpc(reg)

        async with _serve(app) as port:
            res = await _grpc_call(port, '/svc/MetaEcho', encode_message(b''),
                                   headers=[('x-token', 'secret-value')])
        assert _grpc_message(res) == 'token=secret-value'

    @pytest.mark.asyncio
    async def test_missing_header_returns_default(self):
        app, reg = _make_grpc_app()

        @reg.method('/svc/Default')
        async def default_test(request, context):
            val = context.metadata(b'x-missing', b'fallback')
            raise GrpcError(GrpcStatus.OK, f'val={val.decode()}')

        app.enable_grpc(reg)

        async with _serve(app) as port:
            res = await _grpc_call(port, '/svc/Default', encode_message(b''))
        assert _grpc_message(res) == 'val=fallback'


# ---------------------------------------------------------------------------
# §5 — Edge cases
# ---------------------------------------------------------------------------

class TestGrpcEdgeCases:
    """Corner cases that exercise the dispatch boundary."""

    @pytest.mark.asyncio
    async def test_very_long_path_returns_unimplemented(self):
        app, reg = _make_grpc_app()
        app.enable_grpc(reg)

        long_path = '/' + 'x' * 4096 + '/y'
        async with _serve(app) as port:
            res = await _grpc_call(port, long_path, encode_message(b''))
        assert res.status == 200
        assert _grpc_status(res) == str(int(GrpcStatus.UNIMPLEMENTED))

    @pytest.mark.asyncio
    async def test_path_with_dots_and_slashes(self):
        app, reg = _make_grpc_app()

        @reg.method('/my.package.MyService/MyMethod')
        async def my_method(request, context):
            raise GrpcError(GrpcStatus.OK, 'found')

        app.enable_grpc(reg)

        async with _serve(app) as port:
            res = await _grpc_call(port, '/my.package.MyService/MyMethod',
                                   encode_message(b''))
        assert _grpc_status(res) == str(int(GrpcStatus.OK))
        assert _grpc_message(res) == 'found'

    @pytest.mark.asyncio
    async def test_multiple_services_registered(self):
        app = BlackBull()
        reg = GrpcServiceRegistry()

        async def add(request, context):
            raise GrpcError(GrpcStatus.OK, 'add called')

        async def mul(request, context):
            raise GrpcError(GrpcStatus.OK, 'mul called')

        reg.add_service('math.Arith', {'Add': add, 'Mul': mul})

        async def upper(request, context):
            raise GrpcError(GrpcStatus.OK, 'upper called')

        reg.add_method('/str.Text/Upper', upper)
        app.enable_grpc(reg)

        async with _serve(app) as port:
            res = await _grpc_call(port, '/math.Arith/Add', encode_message(b''))
            assert _grpc_message(res) == 'add called'

            res = await _grpc_call(port, '/math.Arith/Mul', encode_message(b''))
            assert _grpc_message(res) == 'mul called'

            res = await _grpc_call(port, '/str.Text/Upper', encode_message(b''))
            assert _grpc_message(res) == 'upper called'

    @pytest.mark.asyncio
    async def test_query_string_does_not_affect_grpc_routing(self):
        app, reg = _make_grpc_app()

        @reg.method('/svc/NoQS')
        async def noqs(request, context):
            raise GrpcError(GrpcStatus.OK, 'routed')

        app.enable_grpc(reg)

        async with _serve(app) as port:
            res = await _grpc_call(port, '/svc/NoQS?ignored=1',
                                   encode_message(b''))
        assert _grpc_message(res) == 'routed'
