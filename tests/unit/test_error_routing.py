"""
Tests for ErrorRouter and BlackBull error handling
===================================================

ErrorRouter
-----------
Unit tests for registration and lookup by HTTPStatus and exception class/instance.

BlackBull error dispatch
------------------------
Two layers of coverage:

* **TestClient-based** (``TestBlackBullErrorDispatchViaTestClient``) —
  exercises 404 / 405 / exception dispatch through the public
  ``blackbull.testing.TestClient`` surface.  These are the
  dogfooding tests for the TestClient feature.

* **Raw ASGI** (``TestBlackBullErrorDispatch``) — exercises scope
  inspection (``scope.state['allowed_methods']``,
  ``scope.state['error_exception']``) that the TestClient public
  API does not expose.  Kept for internal error-routing coverage.
"""

import pytest
from http import HTTPStatus, HTTPMethod

from blackbull.router import ErrorRouter
from blackbull.app import BlackBull, _default_error_handler
from blackbull.headers import Headers
from blackbull.testing import TestClient
from blackbull.utils import Scheme


# ---------------------------------------------------------------------------
# ErrorRouter unit tests
# ---------------------------------------------------------------------------

class TestErrorRouterHTTPStatus:

    def test_registered_status_is_found(self):
        router = ErrorRouter()
        async def handler(scope, receive, send): pass
        router[HTTPStatus.NOT_FOUND] = handler
        assert router[HTTPStatus.NOT_FOUND] is handler

    def test_overwrite_replaces_handler(self):
        router = ErrorRouter()
        async def h1(scope, receive, send): pass
        async def h2(scope, receive, send): pass
        router[HTTPStatus.NOT_FOUND] = h1
        router[HTTPStatus.NOT_FOUND] = h2
        assert router[HTTPStatus.NOT_FOUND] is h2

    def test_unregistered_status_returns_none(self):
        router = ErrorRouter()
        assert router[HTTPStatus.FORBIDDEN] is None

    def test_non_error_status_raises(self):
        router = ErrorRouter()
        with pytest.raises(ValueError):
            router[HTTPStatus.OK] = lambda s, r, s2: None

    def test_contains_true_when_registered(self):
        router = ErrorRouter()
        router[HTTPStatus.INTERNAL_SERVER_ERROR] = lambda s, r, s2: None
        assert HTTPStatus.INTERNAL_SERVER_ERROR in router

    def test_contains_false_when_not_registered(self):
        router = ErrorRouter()
        assert HTTPStatus.BAD_GATEWAY not in router

    def test_decorator_form(self):
        router = ErrorRouter()
        @router(HTTPStatus.FORBIDDEN)
        async def handler(scope, receive, send): pass
        assert router[HTTPStatus.FORBIDDEN] is handler


class TestErrorRouterException:

    def test_registered_exception_class_is_found(self):
        router = ErrorRouter()
        async def handler(scope, receive, send): pass
        router[ValueError] = handler
        assert router[ValueError] is handler

    def test_lookup_by_instance(self):
        router = ErrorRouter()
        async def handler(scope, receive, send): pass
        router[ValueError] = handler
        assert router[ValueError("msg")] is handler

    def test_mro_walk_finds_base_class_handler(self):
        """KeyError is a LookupError; handler for LookupError should be returned."""
        router = ErrorRouter()
        async def handler(scope, receive, send): pass
        router[LookupError] = handler
        assert router[KeyError] is handler
        assert router[KeyError("k")] is handler

    def test_most_specific_class_wins(self):
        router = ErrorRouter()
        async def base_handler(scope, receive, send): pass
        async def specific_handler(scope, receive, send): pass
        router[LookupError] = base_handler
        router[KeyError] = specific_handler
        assert router[KeyError] is specific_handler
        assert router[IndexError] is base_handler  # not KeyError, falls to LookupError

    def test_unregistered_exception_returns_none(self):
        router = ErrorRouter()
        assert router[RuntimeError] is None

    def test_contains_true_via_mro(self):
        router = ErrorRouter()
        router[Exception] = lambda s, r, s2: None
        assert KeyError in router


# ---------------------------------------------------------------------------
# BlackBull error dispatch integration tests (in-process, no real server)
# ---------------------------------------------------------------------------

def _make_scope(path='/', method='GET', type_='http'):
    from blackbull.connection import Connection
    return Connection.from_scope({
        'type': type_,
        'method': method,
        'path': path,
        'scheme': 'http',
        'headers': [],
        'state': {},
    })


class _CaptureSend:
    """Collects all ASGI send() calls.

    Accepts both forms:
    - ASGI event dict: ``await send({'type': 'http.response.start', ...})``
    - High-level bytes: ``await send(body_bytes, status, headers)``
    """
    def __init__(self):
        self.events = []

    async def __call__(self, body_or_event, status: HTTPStatus=None, headers=None):
        if isinstance(body_or_event, dict):
            self.events.append(body_or_event)
        elif isinstance(body_or_event, bytes):
            if status is not None:
                self.events.append({
                    'type': 'http.response.start',
                    'status': status,
                    'headers': headers or [],
                })
            if body_or_event:
                self.events.append({'type': 'http.response.body', 'body': body_or_event})

    @property
    def status(self):
        for e in self.events:
            if e.get('type') == 'http.response.start':
                return e['status']

    @property
    def body(self):
        for e in self.events:
            if e.get('type') == 'http.response.body':
                return e['body']


class TestDefaultErrorHandler:

    @pytest.mark.asyncio
    async def test_default_404_returns_404_status(self):
        scope = _make_scope('/nonexistent')
        send = _CaptureSend()
        await _default_error_handler(scope, None, send)
        # _default_error_handler reads error_status from scope.state
        # With empty state it uses INTERNAL_SERVER_ERROR as default,
        # so test by setting the status explicitly.
        scope.state['error_status'] = HTTPStatus.NOT_FOUND
        send2 = _CaptureSend()
        await _default_error_handler(scope, None, send2)
        assert send2.status == 404

    @pytest.mark.asyncio
    async def test_default_handler_includes_status_phrase_in_body(self):
        scope = _make_scope()
        scope.state['error_status'] = HTTPStatus.NOT_FOUND
        send = _CaptureSend()
        await _default_error_handler(scope, None, send)
        assert b'Not Found' in send.body

    @pytest.mark.asyncio
    async def test_default_handler_includes_exception_info(self):
        scope = _make_scope()
        exc = ValueError("something went wrong")
        scope.state['error_status'] = HTTPStatus.INTERNAL_SERVER_ERROR
        scope.state['error_exception'] = exc
        send = _CaptureSend()
        await _default_error_handler(scope, None, send)
        assert b'ValueError' in send.body
        assert b'something went wrong' in send.body

    @pytest.mark.asyncio
    async def test_default_handler_includes_allow_header_for_405(self):
        scope = _make_scope()
        scope.state['error_status'] = HTTPStatus.METHOD_NOT_ALLOWED
        scope.state['allowed_methods'] = ('get', 'post')
        send = _CaptureSend()
        await _default_error_handler(scope, None, send)
        assert send.status == 405
        start = next(e for e in send.events if e['type'] == 'http.response.start')
        headers = dict(start['headers'])
        assert b'allow' in headers
        assert b'GET' in headers[b'allow']
        assert b'POST' in headers[b'allow']


class TestDevErrorPageTraceback:
    """DEV mode: unhandled exceptions surface their full traceback so users
    debugging locally see the failure inline (alpha-readiness §9)."""

    def _raise(self):
        # Real frame so the traceback is non-empty.
        raise RuntimeError("kaboom")

    @pytest.mark.asyncio
    async def test_dev_plain_text_includes_traceback(self):
        from blackbull.env import reset_settings_cache
        import os
        os.environ['BLACKBULL_ENV'] = 'development'
        reset_settings_cache()
        try:
            try:
                self._raise()
            except RuntimeError as e:
                exc = e
            scope = _make_scope()
            scope.state['error_status'] = HTTPStatus.INTERNAL_SERVER_ERROR
            scope.state['error_exception'] = exc
            send = _CaptureSend()
            await _default_error_handler(scope, None, send)
            assert send.status == 500
            # Traceback frames are present, not just class/message.
            assert b'Traceback' in send.body
            assert b'kaboom' in send.body
            assert b'_raise' in send.body
        finally:
            os.environ.pop('BLACKBULL_ENV', None)
            reset_settings_cache()

    @pytest.mark.asyncio
    async def test_dev_html_for_browser_accept(self):
        from blackbull.env import reset_settings_cache
        import os
        os.environ['BLACKBULL_ENV'] = 'development'
        reset_settings_cache()
        try:
            try:
                self._raise()
            except RuntimeError as e:
                exc = e
            scope = _make_scope()
            scope.headers = Headers([(b'accept', b'text/html,application/xhtml+xml')])
            scope.state['error_status'] = HTTPStatus.INTERNAL_SERVER_ERROR
            scope.state['error_exception'] = exc
            send = _CaptureSend()
            await _default_error_handler(scope, None, send)
            start = next(e for e in send.events if e['type'] == 'http.response.start')
            ctype = dict(start['headers']).get(b'content-type', b'')
            assert b'text/html' in ctype
            assert b'<pre>' in send.body
            assert b'RuntimeError' in send.body
        finally:
            os.environ.pop('BLACKBULL_ENV', None)
            reset_settings_cache()


class TestDevClientErrorPage:
    """DEV mode, 4xx HTTPException: the framework already diagnosed the
    client's mistake, so the page carries the status + detail line but no
    Python traceback (Sprint 74).  Tracebacks are for *unexpected* server
    faults — 5xx and non-HTTPException — which keep the full frames."""

    @staticmethod
    def _client_error():
        from blackbull.router import HTTPException
        try:
            raise HTTPException(
                HTTPStatus.BAD_REQUEST,
                "missing required query parameter 'q' for handler 'search'")
        except HTTPException as e:
            return e

    @pytest.mark.asyncio
    async def test_dev_4xx_shows_detail_but_no_traceback(self):
        from blackbull.env import reset_settings_cache
        import os
        os.environ['BLACKBULL_ENV'] = 'development'
        reset_settings_cache()
        try:
            exc = self._client_error()
            scope = _make_scope()
            scope.state['error_status'] = HTTPStatus.BAD_REQUEST
            scope.state['error_exception'] = exc
            send = _CaptureSend()
            await _default_error_handler(scope, None, send)
            assert send.status == 400
            # The actionable diagnosis stays…
            assert b'Bad Request' in send.body
            assert b"missing required query parameter 'q'" in send.body
            # …the server-internals noise goes.
            assert b'Traceback' not in send.body
            assert b'File "' not in send.body
        finally:
            os.environ.pop('BLACKBULL_ENV', None)
            reset_settings_cache()

    @pytest.mark.asyncio
    async def test_dev_4xx_html_shows_detail_but_no_traceback(self):
        from blackbull.env import reset_settings_cache
        import os
        os.environ['BLACKBULL_ENV'] = 'development'
        reset_settings_cache()
        try:
            exc = self._client_error()
            scope = _make_scope()
            scope.headers = Headers([(b'accept', b'text/html,application/xhtml+xml')])
            scope.state['error_status'] = HTTPStatus.BAD_REQUEST
            scope.state['error_exception'] = exc
            send = _CaptureSend()
            await _default_error_handler(scope, None, send)
            assert b"missing required query parameter" in send.body
            assert b'<pre>' not in send.body       # the traceback block
            assert b'Traceback' not in send.body
        finally:
            os.environ.pop('BLACKBULL_ENV', None)
            reset_settings_cache()

    @pytest.mark.asyncio
    async def test_dev_5xx_httpexception_keeps_traceback(self):
        from blackbull.env import reset_settings_cache
        from blackbull.router import HTTPException
        import os
        os.environ['BLACKBULL_ENV'] = 'development'
        reset_settings_cache()
        try:
            try:
                raise HTTPException(HTTPStatus.BAD_GATEWAY, 'upstream died')
            except HTTPException as e:
                exc = e
            scope = _make_scope()
            scope.state['error_status'] = HTTPStatus.BAD_GATEWAY
            scope.state['error_exception'] = exc
            send = _CaptureSend()
            await _default_error_handler(scope, None, send)
            assert send.status == 502
            assert b'Traceback' in send.body
            assert b'upstream died' in send.body
        finally:
            os.environ.pop('BLACKBULL_ENV', None)
            reset_settings_cache()


class TestProductionErrorPage:
    """Production mode keeps the response terse — no exception class or
    message is leaked to the network."""

    @pytest.mark.asyncio
    async def test_prod_omits_exception_details(self):
        from blackbull.env import reset_settings_cache
        import os
        os.environ['BLACKBULL_ENV'] = 'production'
        reset_settings_cache()
        try:
            exc = ValueError("secret-internal-detail")
            scope = _make_scope()
            scope.state['error_status'] = HTTPStatus.INTERNAL_SERVER_ERROR
            scope.state['error_exception'] = exc
            send = _CaptureSend()
            await _default_error_handler(scope, None, send)
            assert send.status == 500
            assert b'Internal Server Error' in send.body
            assert b'ValueError' not in send.body
            assert b'secret-internal-detail' not in send.body
            assert b'Traceback' not in send.body
        finally:
            os.environ.pop('BLACKBULL_ENV', None)
            reset_settings_cache()


class TestBlackBullErrorDispatch:

    def _make_app(self):
        app = BlackBull()

        @app.route(path='/hello', methods=[HTTPMethod.GET])
        async def hello():
            return 'hello'

        @app.route(path='/post-only', methods=[HTTPMethod.POST])
        async def post_only():
            return 'posted'

        return app

    # -- Tests replaced with TestClient (same guarantees, public API) -----

    def test_404_calls_default_handler(self):
        app = self._make_app()
        with TestClient(app) as client:
            response = client.get('/nonexistent')
        assert response.status_code == 404

    def test_404_calls_custom_on_error_handler(self):
        app = self._make_app()

        @app.on_error(HTTPStatus.NOT_FOUND)
        async def custom_404(scope, receive, send):
            await send(b'custom not found', HTTPStatus.NOT_FOUND)

        with TestClient(app) as client:
            response = client.get('/nonexistent')
        assert response.status_code == 404
        assert response.text == 'custom not found'

    def test_405_returns_method_not_allowed(self):
        """GET to a POST-only route must yield 405, not 404."""
        app = self._make_app()
        with TestClient(app) as client:
            response = client.get('/post-only')
        assert response.status_code == 405

    def test_405_allow_header_lists_registered_methods(self):
        app = self._make_app()
        with TestClient(app) as client:
            response = client.get('/post-only')
        assert response.status_code == 405
        assert 'allow' in response.headers
        assert 'POST' in response.headers['allow'].upper()

    def test_405_calls_custom_on_error_handler(self):
        app = self._make_app()

        @app.on_error(HTTPStatus.METHOD_NOT_ALLOWED)
        async def custom_405(scope, receive, send):
            await send(b'custom 405', HTTPStatus.METHOD_NOT_ALLOWED)

        with TestClient(app) as client:
            response = client.get('/post-only')
        assert response.status_code == 405
        assert response.text == 'custom 405'

    def test_exception_in_handler_calls_error_router(self):
        app = self._make_app()

        @app.on_error(RuntimeError)
        async def handle_runtime(scope, receive, send):
            await send(b'runtime error caught', HTTPStatus.INTERNAL_SERVER_ERROR)

        @app.route(path='/boom')
        async def boom():
            raise RuntimeError("kaboom")

        with TestClient(app) as client:
            response = client.get('/boom')
        assert response.status_code == 500
        assert response.text == 'runtime error caught'

    # -- Tests kept as raw ASGI (need scope.state inspection) -----------

    @pytest.mark.asyncio
    async def test_on_error_with_exception_base_class_catches_subclass(self):
        app = self._make_app()
        caught = []

        @app.on_error(Exception)
        async def catch_all(scope, receive, send):
            caught.append(type(scope.state.get('error_exception')).__name__)
            await send(b'caught', HTTPStatus.INTERNAL_SERVER_ERROR)

        @app.route(path='/key-err')
        async def key_err(scope, receive, send):
            raise KeyError("missing")

        scope = _make_scope('/key-err')
        send = _CaptureSend()
        await app(scope, None, send)
        assert caught == ['KeyError']

    @pytest.mark.asyncio
    async def test_unknown_http_method_returns_405(self):
        """An HTTP method not in HTTPMethod must return 405, not raise ValueError."""
        app = self._make_app()
        scope = _make_scope('/hello', method='NOTEXIST')
        send = _CaptureSend()
        await app(scope, None, send)
        assert send.status == 405, (
            f"Expected 405 for unknown method NOTEXIST, got {send.status}"
        )

    @pytest.mark.asyncio
    async def test_unknown_http_method_does_not_raise(self):
        """ValueError from HTTPMethod() must be caught internally; __call__ must not raise."""
        app = self._make_app()
        scope = _make_scope('/hello', method='HEAD')
        send = _CaptureSend()
        # Must complete without raising ValueError
        await app(scope, None, send)