"""Integration tests for gRPC via BlackBull's full ASGI dispatch path.

Uses :class:`TestClient` (in-process ``httpx.ASGITransport``) to verify
that gRPC requests flow through the app correctly — from ``BlackBull.__call__``
through ``_dispatch`` to ``serve_grpc``.

.. important::

   ``httpx.ASGITransport`` runs over HTTP/1.1 and does **not** support the
   ``http.response.trailers`` ASGI event.  gRPC success responses use separate
   trailers (HEADERS → DATA → TRAILERS), which crash httpx's ASGI transport.

   Therefore, integration tests via ``TestClient`` can only verify:

   * **Trailers-only error responses** (grpc-status embedded in response
     HEADERS — no separate TRAILERS event).
   * **Dispatch routing** — that ``application/grpc`` content-type correctly
     activates the gRPC path.
   * **REST + gRPC coexistence** — that non-gRPC requests are unaffected.

   Full success-path validation (DATA frame + TRAILERS) is covered in
   ``tests/conformance/grpc/`` using a direct ``serve_grpc`` harness.
"""
from __future__ import annotations

import pytest

from blackbull import BlackBull
from blackbull.grpc import (
    GrpcServiceRegistry, GrpcStatus, GrpcError,
    encode_message, decode_messages,
)
from blackbull.testing import TestClient


# Tag all tests in this file as integration tests.
pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_grpc_app() -> tuple[BlackBull, GrpcServiceRegistry]:
    """Create a minimal BlackBull app with gRPC support."""
    app = BlackBull()
    reg = GrpcServiceRegistry()
    return app, reg


def _grpc_post(client: TestClient, path: str, payload: bytes,
               headers: dict | None = None) -> tuple[int, bytes, dict]:
    """Send a gRPC-style POST and return (status, body, response_headers).

    gRPC errors use trailers-only: HTTP 200 + grpc-status in response headers.
    gRPC success uses separate trailers which httpx cannot handle (see module
    docstring).  Callers should check for error responses via headers.
    """
    h = {'Content-Type': 'application/grpc'}
    if headers:
        h.update(headers)
    resp = client.post(path, content=payload, headers=h)
    return resp.status_code, resp.content, dict(resp.headers)


# ---------------------------------------------------------------------------
# §1 — Dispatch routing: gRPC requests are correctly intercepted
# ---------------------------------------------------------------------------

class TestGrpcDispatchRouting:
    """Verify that ``BlackBull._dispatch`` routes ``application/grpc``
    requests to the gRPC bridge (not the HTTP router)."""

    def test_grpc_content_type_triggers_grpc_path(self):
        """POST with ``Content-Type: application/grpc`` → UNIMPLEMENTED
        (if not registered) rather than 404 from HTTP router."""
        app, reg = _make_grpc_app()
        app.enable_grpc(reg)

        with TestClient(app) as client:
            status, _, headers = _grpc_post(
                client, '/some/grpc/method', encode_message(b''))
            assert status == 200
            assert headers.get('grpc-status') == str(int(GrpcStatus.UNIMPLEMENTED))

    def test_unregistered_method_returns_unimplemented(self):
        """POST to a path with no registered gRPC handler → UNIMPLEMENTED (12)."""
        app, reg = _make_grpc_app()
        app.enable_grpc(reg)

        with TestClient(app) as client:
            status, _, headers = _grpc_post(
                client, '/No.Such/Method', encode_message(b''))
            assert status == 200
            assert headers.get('grpc-status') == str(int(GrpcStatus.UNIMPLEMENTED))

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

    def test_content_type_with_charset_is_handled(self):
        """``Content-Type: application/grpc; charset=utf-8`` should match
        the ``startswith(b'application/grpc')`` check in ``_dispatch``."""
        app, reg = _make_grpc_app()
        app.enable_grpc(reg)

        with TestClient(app) as client:
            resp = client.post('/No.Such/Method',
                               content=encode_message(b''),
                               headers={'Content-Type': 'application/grpc; charset=utf-8'})
            assert resp.status_code == 200
            assert dict(resp.headers).get('grpc-status') == \
                str(int(GrpcStatus.UNIMPLEMENTED))


# ---------------------------------------------------------------------------
# §2 — Error handling: all error paths produce correct trailers-only responses
# ---------------------------------------------------------------------------

class TestGrpcErrorDispatch:
    """gRPC errors must produce HTTP 200 with grpc-status in response
    headers (trailers-only) — never HTTP 4xx/5xx.  These are all testable
    via TestClient because no separate TRAILERS event is emitted."""

    def test_grpc_error_returns_200_with_grpc_status(self):
        app, reg = _make_grpc_app()

        @reg.method('/svc/Fail')
        async def fail(request, context):
            raise GrpcError(GrpcStatus.RESOURCE_EXHAUSTED, 'overloaded')

        app.enable_grpc(reg)

        with TestClient(app) as client:
            status, _, headers = _grpc_post(
                client, '/svc/Fail', encode_message(b''))
            assert status == 200
            assert headers.get('grpc-status') == \
                str(int(GrpcStatus.RESOURCE_EXHAUSTED))

    def test_handler_exception_returns_internal(self):
        app, reg = _make_grpc_app()

        @reg.method('/svc/Boom')
        async def boom(request, context):
            raise RuntimeError('unexpected failure')

        app.enable_grpc(reg)

        with TestClient(app) as client:
            status, _, headers = _grpc_post(
                client, '/svc/Boom', encode_message(b''))
            assert status == 200
            assert headers.get('grpc-status') == str(int(GrpcStatus.INTERNAL))
            assert 'unexpected failure' in headers.get('grpc-message', '')

    def test_invalid_message_body_returns_internal(self):
        app, reg = _make_grpc_app()

        @reg.method('/svc/Valid')
        async def valid(request, context):
            return b'ok'

        app.enable_grpc(reg)

        with TestClient(app) as client:
            status, _, headers = _grpc_post(
                client, '/svc/Valid', b'not a valid grpc frame')
            assert status == 200
            assert headers.get('grpc-status') == str(int(GrpcStatus.INTERNAL))

    def test_context_abort_produces_correct_status(self):
        app, reg = _make_grpc_app()

        @reg.method('/svc/Abort')
        async def abort_handler(request, context):
            context.abort(GrpcStatus.PERMISSION_DENIED, 'nope')
            return b''  # unreachable

        app.enable_grpc(reg)

        with TestClient(app) as client:
            status, _, headers = _grpc_post(
                client, '/svc/Abort', encode_message(b''))
            assert status == 200
            assert headers.get('grpc-status') == \
                str(int(GrpcStatus.PERMISSION_DENIED))

    def test_all_17_status_codes_are_wireable_via_integration(self):
        """For each canonical gRPC status code, verify the correct
        grpc-status trailer is emitted through the full dispatch path."""
        for code in GrpcStatus:
            app = BlackBull()
            reg = GrpcServiceRegistry()
            path = f'/svc/Code{code.value}'

            @reg.method(path)
            async def h(request, context, _code=code):
                raise GrpcError(_code, f'status {_code.value}')

            app.enable_grpc(reg)

            with TestClient(app) as client:
                _, _, headers = _grpc_post(client, path, encode_message(b''))
                assert headers.get('grpc-status') == str(int(code)), (
                    f'GrpcStatus.{code.name} ({code.value}) failed')

    def test_empty_body_zero_messages_returns_error(self):
        app, reg = _make_grpc_app()

        @reg.method('/svc/Unary')
        async def unary(request, context):
            return b'ok'

        app.enable_grpc(reg)

        with TestClient(app) as client:
            status, _, headers = _grpc_post(client, '/svc/Unary', b'')
            assert status == 200
            assert headers.get('grpc-status') in (
                str(int(GrpcStatus.INTERNAL)),
                str(int(GrpcStatus.UNIMPLEMENTED)),
            )

    def test_compressed_message_rejected_via_integration(self):
        app, reg = _make_grpc_app()

        @reg.method('/svc/Unary')
        async def unary(request, context):
            return b'ok'

        app.enable_grpc(reg)

        with TestClient(app) as client:
            status, _, headers = _grpc_post(
                client, '/svc/Unary',
                encode_message(b'data', compressed=True))
            assert status == 200
            assert headers.get('grpc-status') == \
                str(int(GrpcStatus.UNIMPLEMENTED))


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

    def test_grpc_content_type_to_root_returns_unimplemented(self):
        app, reg = _make_grpc_app()

        @app.route(path='/')
        async def index():
            return 'home'

        app.enable_grpc(reg)

        with TestClient(app) as client:
            # Normal REST GET — works
            resp = client.get('/')
            assert resp.status_code == 200
            assert resp.text == 'home'

            # POST with application/grpc → gRPC path → UNIMPLEMENTED
            status, _, headers = _grpc_post(client, '/', encode_message(b''))
            assert status == 200
            assert headers.get('grpc-status') == \
                str(int(GrpcStatus.UNIMPLEMENTED))

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
    the full dispatch path.  Uses error responses to verify metadata
    without hitting the trailers issue."""

    def test_request_header_accessible_via_context_metadata(self):
        app, reg = _make_grpc_app()

        @reg.method('/svc/MetaEcho')
        async def meta_echo(request, context):
            token = context.metadata(b'x-token', b'no-token')
            raise GrpcError(GrpcStatus.OK, f'token={token.decode()}')

        app.enable_grpc(reg)

        with TestClient(app) as client:
            _, _, headers = _grpc_post(
                client, '/svc/MetaEcho', encode_message(b''),
                headers={'x-token': 'secret-value'})
            assert headers.get('grpc-message') == 'token=secret-value'

    def test_missing_header_returns_default(self):
        app, reg = _make_grpc_app()

        @reg.method('/svc/Default')
        async def default_test(request, context):
            val = context.metadata(b'x-missing', b'fallback')
            raise GrpcError(GrpcStatus.OK, f'val={val.decode()}')

        app.enable_grpc(reg)

        with TestClient(app) as client:
            _, _, headers = _grpc_post(
                client, '/svc/Default', encode_message(b''))
            assert headers.get('grpc-message') == 'val=fallback'


# ---------------------------------------------------------------------------
# §5 — Edge cases
# ---------------------------------------------------------------------------

class TestGrpcEdgeCases:
    """Corner cases that exercise the dispatch boundary."""

    def test_very_long_path_returns_unimplemented(self):
        app, reg = _make_grpc_app()
        app.enable_grpc(reg)

        long_path = '/' + 'x' * 4096 + '/y'
        with TestClient(app) as client:
            status, _, headers = _grpc_post(
                client, long_path, encode_message(b''))
            assert status == 200
            assert headers.get('grpc-status') == \
                str(int(GrpcStatus.UNIMPLEMENTED))

    def test_path_with_dots_and_slashes(self):
        app, reg = _make_grpc_app()

        @reg.method('/my.package.MyService/MyMethod')
        async def my_method(request, context):
            raise GrpcError(GrpcStatus.OK, 'found')

        app.enable_grpc(reg)

        with TestClient(app) as client:
            _, _, headers = _grpc_post(
                client, '/my.package.MyService/MyMethod', encode_message(b''))
            assert headers.get('grpc-status') == str(int(GrpcStatus.OK))
            assert headers.get('grpc-message') == 'found'

    def test_multiple_services_registered(self):
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

        with TestClient(app) as client:
            _, _, headers = _grpc_post(
                client, '/math.Arith/Add', encode_message(b''))
            assert headers.get('grpc-message') == 'add called'

            _, _, headers = _grpc_post(
                client, '/math.Arith/Mul', encode_message(b''))
            assert headers.get('grpc-message') == 'mul called'

            _, _, headers = _grpc_post(
                client, '/str.Text/Upper', encode_message(b''))
            assert headers.get('grpc-message') == 'upper called'

    def test_query_string_does_not_affect_grpc_routing(self):
        app, reg = _make_grpc_app()

        @reg.method('/svc/NoQS')
        async def noqs(request, context):
            raise GrpcError(GrpcStatus.OK, 'routed')

        app.enable_grpc(reg)

        with TestClient(app) as client:
            _, _, headers = _grpc_post(
                client, '/svc/NoQS?ignored=1', encode_message(b''))
            assert headers.get('grpc-message') == 'routed'
