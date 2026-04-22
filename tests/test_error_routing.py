"""
Tests for ErrorRouter and BlackBull error handling
===================================================

ErrorRouter
-----------
Unit tests for registration and lookup by HTTPStatus and exception class/instance.

BlackBull error dispatch
------------------------
Integration tests that verify the correct handler is called for 404, 405,
and custom on_error registrations, using an in-process ASGI call rather than
a real server so there are no port or subprocess concerns.
"""

import pytest
from http import HTTPStatus, HTTPMethod

from blackbull.router import ErrorRouter
from blackbull.BlackBull import BlackBull, _default_error_handler
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
    return {
        'type': type_,
        'method': method,
        'path': path,
        'scheme': 'http',
        'headers': [],
        'state': {},
    }


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
        # _default_error_handler reads error_status from scope['state']
        # With empty state it uses INTERNAL_SERVER_ERROR as default,
        # so test by setting the status explicitly.
        scope['state']['error_status'] = HTTPStatus.NOT_FOUND
        send2 = _CaptureSend()
        await _default_error_handler(scope, None, send2)
        assert send2.status == 404

    @pytest.mark.asyncio
    async def test_default_handler_includes_status_phrase_in_body(self):
        scope = _make_scope()
        scope['state']['error_status'] = HTTPStatus.NOT_FOUND
        send = _CaptureSend()
        await _default_error_handler(scope, None, send)
        assert b'Not Found' in send.body

    @pytest.mark.asyncio
    async def test_default_handler_includes_exception_info(self):
        scope = _make_scope()
        exc = ValueError("something went wrong")
        scope['state']['error_status'] = HTTPStatus.INTERNAL_SERVER_ERROR
        scope['state']['error_exception'] = exc
        send = _CaptureSend()
        await _default_error_handler(scope, None, send)
        assert b'ValueError' in send.body
        assert b'something went wrong' in send.body

    @pytest.mark.asyncio
    async def test_default_handler_includes_allow_header_for_405(self):
        scope = _make_scope()
        scope['state']['error_status'] = HTTPStatus.METHOD_NOT_ALLOWED
        scope['state']['allowed_methods'] = ('get', 'post')
        send = _CaptureSend()
        await _default_error_handler(scope, None, send)
        assert send.status == 405
        start = next(e for e in send.events if e['type'] == 'http.response.start')
        headers = dict(start['headers'])
        assert b'allow' in headers
        assert b'GET' in headers[b'allow']
        assert b'POST' in headers[b'allow']


class TestBlackBullErrorDispatch:

    def _make_app(self):
        app = BlackBull()

        @app.route(path='/hello', methods=[HTTPMethod.GET])
        async def hello(scope, receive, send):
            await send(b'hello', HTTPStatus.OK)

        @app.route(path='/post-only', methods=[HTTPMethod.POST])
        async def post_only(scope, receive, send):
            await send(b'posted', HTTPStatus.OK)

        return app

    @pytest.mark.asyncio
    async def test_404_calls_default_handler(self):
        app = self._make_app()
        scope = _make_scope('/nonexistent')
        send = _CaptureSend()
        await app(scope, None, send)
        assert send.status == 404

    @pytest.mark.asyncio
    async def test_404_calls_custom_on_error_handler(self):
        app = self._make_app()
        called = []

        @app.on_error(HTTPStatus.NOT_FOUND)
        async def custom_404(scope, receive, send):
            called.append(True)
            await send(b'custom not found', HTTPStatus.NOT_FOUND)

        scope = _make_scope('/nonexistent')
        send = _CaptureSend()
        await app(scope, None, send)
        assert called, "custom on_error handler was not called"
        assert send.status == 404
        assert send.body == b'custom not found'

    @pytest.mark.asyncio
    async def test_405_returns_method_not_allowed(self):
        """GET to a POST-only route must yield 405, not 404."""
        app = self._make_app()
        scope = _make_scope('/post-only', method='GET')
        send = _CaptureSend()
        await app(scope, None, send)
        assert send.status == 405

    @pytest.mark.asyncio
    async def test_405_allow_header_lists_registered_methods(self):
        app = self._make_app()
        scope = _make_scope('/post-only', method='GET')
        send = _CaptureSend()
        await app(scope, None, send)
        start = next(e for e in send.events if e['type'] == 'http.response.start')
        headers = dict(start['headers'])
        assert b'allow' in headers
        assert b'POST' in headers[b'allow'].upper()

    @pytest.mark.asyncio
    async def test_405_calls_custom_on_error_handler(self):
        app = self._make_app()
        called = []

        @app.on_error(HTTPStatus.METHOD_NOT_ALLOWED)
        async def custom_405(scope, receive, send):
            called.append(scope['state'].get('allowed_methods'))
            await send(b'custom 405', HTTPStatus.METHOD_NOT_ALLOWED)

        scope = _make_scope('/post-only', method='GET')
        send = _CaptureSend()
        await app(scope, None, send)
        assert called, "custom 405 handler was not called"
        assert send.body == b'custom 405'

    @pytest.mark.asyncio
    async def test_exception_in_handler_calls_error_router(self):
        app = self._make_app()
        caught = []

        @app.on_error(RuntimeError)
        async def handle_runtime(scope, receive, send):
            caught.append(scope['state'].get('error_exception'))
            await send(b'runtime error caught', HTTPStatus.INTERNAL_SERVER_ERROR)

        @app.route(path='/boom')
        async def boom(scope, receive, send):
            raise RuntimeError("kaboom")

        scope = _make_scope('/boom')
        send = _CaptureSend()
        await app(scope, None, send)
        assert caught, "error handler for RuntimeError was not called"
        assert isinstance(caught[0], RuntimeError)

    @pytest.mark.asyncio
    async def test_on_error_with_exception_base_class_catches_subclass(self):
        app = self._make_app()
        caught = []

        @app.on_error(Exception)
        async def catch_all(scope, receive, send):
            caught.append(type(scope['state'].get('error_exception')).__name__)
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
