"""Tests for ``BlackBull.extensions`` and the ``init_app(app)`` convention.

The extension surface is intentionally minimal — one dict attribute
(``app.extensions``) and a documented convention for third-party
packages (``init_app(app)``).  These tests verify the mechanism and
double as the worked examples for ``docs/guide/extensions.md``.

Coverage:
    * ``app.extensions`` exists and is a writable dict.
    * An extension class following the ``init_app(app)`` pattern can
      register middleware, routes, event handlers, and error handlers.
    * Extension key collision raises ``RuntimeError`` (per the convention).
    * Extension dependency ordering — an extension can look up another
      extension in ``app.extensions`` at ``init_app`` time.
    * Preserving ``init_app`` idempotency — calling it twice behaves
      predictably.
    * Representative ASGI middleware via ``app.use()`` — smoke test.
"""

from __future__ import annotations

import pytest
from http import HTTPMethod, HTTPStatus

from blackbull import BlackBull, Response
from blackbull.testing import TestClient


# ---------------------------------------------------------------------------
# Task A — app.extensions exists
# ---------------------------------------------------------------------------

class TestExtensionsAttribute:
    """``app.extensions`` must exist as a plain dict on every BlackBull
    instance, initially empty."""

    def test_extensions_exists_on_app(self):
        """``app.extensions`` is a dict attribute on ``BlackBull``."""
        app = BlackBull()
        assert hasattr(app, 'extensions'), (
            'BlackBull must expose app.extensions')
        assert isinstance(app.extensions, dict), (
            'app.extensions must be a dict')

    def test_extensions_is_empty_by_default(self):
        """A freshly-constructed app has an empty extensions dict."""
        app = BlackBull()
        assert app.extensions == {}, (
            'app.extensions must be empty at construction time')

    def test_extensions_is_writable(self):
        """Third-party code can write into app.extensions."""
        app = BlackBull()
        app.extensions['test'] = object()
        assert 'test' in app.extensions
        popped = app.extensions.pop('test')
        assert popped is not None


# ---------------------------------------------------------------------------
# Task A + B — init_app convention with middleware, events, routes, errors
# ---------------------------------------------------------------------------

class _FakeAuthExtension:
    """A minimal extension following the ``init_app(app)`` convention.

    Registers:
        - An auth middleware that injects ``scope['user']``.
        - A ``request_received`` event handler.
        - A ``/whoami`` route.
        - A custom 403 error handler.
    """

    def __init__(self, app: BlackBull | None = None):
        self._middleware_called = False
        self._event_fired = False
        self._error_handler_called = False
        self._whoami_called = False
        if app is not None:
            self.init_app(app)

    def init_app(self, app: BlackBull) -> None:
        app.extensions['auth'] = self
        app.use(self._auth_middleware)
        app.on('request_received')(self._on_request)
        app.on_error(403)(self._on_403)

        @app.route(path='/whoami')
        async def whoami(scope):
            self._whoami_called = True
            user = scope.state.get('user', 'anonymous')
            return {'user': user}

        # Keep a reference so the route callback is collected properly
        self._whoami_handler = whoami

    async def _auth_middleware(self, scope, receive, send, call_next):
        self._middleware_called = True
        # Dummy auth — always sets 'testuser'
        scope.state['user'] = 'testuser'
        await call_next(scope, receive, send)

    async def _on_request(self, event):
        self._event_fired = True

    async def _on_403(self, scope, receive, send):
        self._error_handler_called = True
        await send(Response(b'forbidden', status=HTTPStatus.FORBIDDEN))


class TestInitAppConvention:
    """The ``init_app(app)`` convention must work end-to-end with
    BlackBull's existing middleware, event, route, and error APIs."""

    def test_extension_registers_in_extensions_dict(self):
        """After ``init_app(app)``, the extension must be stored under
        its key in ``app.extensions``."""
        app = BlackBull()
        ext = _FakeAuthExtension()
        ext.init_app(app)
        assert app.extensions.get('auth') is ext, (
            'extension must be stored at app.extensions["auth"]')

    def test_extension_constructor_with_app_argument(self):
        """Passing ``app`` to the constructor must call ``init_app``
        immediately (Flask-compatible convenience pattern)."""
        app = BlackBull()
        ext = _FakeAuthExtension(app=app)
        assert app.extensions.get('auth') is ext

    def test_extension_middleware_is_called(self):
        """Middleware registered by ``init_app`` must run and inject
        ``scope['user']``."""
        app = BlackBull()
        ext = _FakeAuthExtension(app=app)

        # Add a route that returns the injected user
        @app.route(path='/me')
        async def me(scope):
            return {'user': scope.state.get('user', '?')}

        with TestClient(app) as client:
            resp = client.get('/me')
            assert resp.status_code == 200
            # The middleware should have injected 'testuser'
            assert resp.json() == {'user': 'testuser'}
            assert ext._middleware_called, (
                'auth middleware must have been called')

    def test_extension_route_is_served(self):
        """Routes registered by ``init_app`` must be reachable."""
        app = BlackBull()
        ext = _FakeAuthExtension(app=app)

        with TestClient(app) as client:
            resp = client.get('/whoami')
            assert resp.status_code == 200
            assert resp.json() == {'user': 'testuser'}
            assert ext._whoami_called

    def test_extension_event_handler_is_fired(self):
        """Event handlers registered by ``init_app`` must fire on
        every request."""
        app = BlackBull()
        ext = _FakeAuthExtension(app=app)

        @app.route(path='/ping')
        async def ping():
            return 'pong'

        with TestClient(app) as client:
            resp = client.get('/ping')
            assert resp.status_code == 200
            # Fire-and-forget events need a settle window — poll with
            # a small sleep rather than spinning the GIL, so the
            # dispatcher's background task can actually run.
            import time
            deadline = time.monotonic() + 2.0
            while not ext._event_fired and time.monotonic() < deadline:
                time.sleep(0.02)
            assert ext._event_fired, (
                'request_received event must have fired')

    @pytest.mark.xfail(
        reason="on_error(STATUS) handlers fire only when routing/dispatch "
               "raises (MethodNotApplicable, PathNotRegistered, or an "
               "exception that becomes 500). A handler that explicitly "
               "returns Response(status=403) is an emission, not an error "
               "event — wiring status-code matching to handler-returned "
               "responses requires intercepting send and is out of Sprint 40 "
               "scope.",
        strict=True,
    )
    def test_extension_error_handler_is_called(self):
        """Error handlers registered by ``init_app`` must handle
        the appropriate status code."""
        app = BlackBull()
        ext = _FakeAuthExtension(app=app)

        @app.route(path='/secret')
        async def secret():
            return Response(b'forbidden', status=HTTPStatus.FORBIDDEN)

        with TestClient(app) as client:
            resp = client.get('/secret')
            assert resp.status_code == 403
            assert ext._error_handler_called, (
                '403 error handler must have been called')


# ---------------------------------------------------------------------------
# Task D §3 — Namespace collision detection
# ---------------------------------------------------------------------------

class _CollidingExtension:
    """An extension that checks for key collision at ``init_app`` time."""
    def __init__(self, key: str = 'auth'):
        self._key = key

    def init_app(self, app: BlackBull) -> None:
        if self._key in app.extensions:
            existing = type(app.extensions[self._key]).__module__
            raise RuntimeError(
                f"app.extensions[{self._key!r}] is already registered "
                f"by {existing}. Cannot initialise "
                f"{type(self).__module__}.")
        app.extensions[self._key] = self


class TestExtensionKeyCollision:
    """If two extensions claim the same key in ``app.extensions``, the
    second MUST raise ``RuntimeError`` (convention, not framework
    enforced)."""

    def test_collision_raises_runtime_error(self):
        """Registering two extensions under the same key ('auth')
        must raise RuntimeError from the second ``init_app``."""
        app = BlackBull()
        ext1 = _CollidingExtension(key='auth')
        ext1.init_app(app)

        ext2 = _CollidingExtension(key='auth')
        with pytest.raises(RuntimeError, match='already registered'):
            ext2.init_app(app)

    def test_different_keys_no_collision(self):
        """Two extensions using different keys must both succeed."""
        app = BlackBull()
        ext_auth = _CollidingExtension(key='auth')
        ext_auth.init_app(app)
        ext_admin = _CollidingExtension(key='admin')
        ext_admin.init_app(app)

        assert app.extensions['auth'] is ext_auth
        assert app.extensions['admin'] is ext_admin


# ---------------------------------------------------------------------------
# Task D §4 — Extension dependency ordering
# ---------------------------------------------------------------------------

class _DependentExtension:
    """An extension that requires another extension to be initialised first."""
    def __init__(self, key: str = 'admin', requires: str = 'auth'):
        self._key = key
        self._requires = requires

    def init_app(self, app: BlackBull) -> None:
        if self._requires not in app.extensions:
            dep_name = self._requires
            raise RuntimeError(
                f'{type(self).__name__} requires an extension at '
                f"app.extensions[{dep_name!r}]. "
                f'Initialise it first.')
        app.extensions[self._key] = self


class _PrerequisiteExtension:
    """A simple extension that provides a prerequisite for others."""
    def __init__(self, key: str = 'auth'):
        self._key = key

    def init_app(self, app: BlackBull) -> None:
        app.extensions[self._key] = self


class TestExtensionDependencyOrdering:
    """Extensions must be initialised in dependency order — prerequisites
    first, dependents second.  BlackBull does not resolve this; the
    application author controls it via ``init_app`` call order."""

    def test_dependent_after_prerequisite_succeeds(self):
        """Prerequisite first, dependent second → both succeed."""
        app = BlackBull()
        auth = _PrerequisiteExtension(key='auth')
        auth.init_app(app)
        admin = _DependentExtension(key='admin', requires='auth')
        admin.init_app(app)

        assert 'auth' in app.extensions
        assert 'admin' in app.extensions

    def test_dependent_before_prerequisite_raises(self):
        """Dependent first, prerequisite missing → RuntimeError."""
        app = BlackBull()
        admin = _DependentExtension(key='admin', requires='auth')
        with pytest.raises(RuntimeError, match='requires an extension'):
            admin.init_app(app)

    def test_dependency_lookup_in_init_app(self):
        """An extension that successfully looks up its dependency can
        use it — end-to-end with two real-ish extensions."""
        app = BlackBull()

        class _AuthProvider:
            def init_app(self, a):
                a.extensions['auth'] = self
                self.secret = 's3cret'

        class _AdminDashboard:
            def init_app(self, a):
                if 'auth' not in a.extensions:
                    raise RuntimeError('auth required')
                self._auth = a.extensions['auth']
                a.extensions['admin'] = self

        auth = _AuthProvider()
        auth.init_app(app)
        admin = _AdminDashboard()
        admin.init_app(app)

        assert admin._auth is auth
        assert admin._auth.secret == 's3cret'


# ---------------------------------------------------------------------------
# Task D §2 — init_app idempotency
# ---------------------------------------------------------------------------

class TestInitAppIdempotency:
    """Calling ``init_app(app)`` twice on the same extension instance
    should be predictable — the second call should either be a no-op
    or raise a clear error."""

    def test_init_app_called_twice_raises_or_noops(self):
        """If the convention detects double-init, it should raise
        RuntimeError rather than double-register routes/middleware."""
        app = BlackBull()

        class _OnceOnlyExtension:
            def __init__(self):
                self._initialised = False

            def init_app(self, a):
                if self._initialised:
                    raise RuntimeError(
                        f'{type(self).__name__} is already initialised')
                self._initialised = True
                a.extensions['once'] = self

        ext = _OnceOnlyExtension()
        ext.init_app(app)
        with pytest.raises(RuntimeError, match='already initialised'):
            ext.init_app(app)


# ---------------------------------------------------------------------------
# Task D §6 — Representative ASGI middleware via app.use()
# ---------------------------------------------------------------------------

class TestASGIMiddlewareCompat:
    """Arbitrary ASGI 3.0 middleware that follows the ``(scope, receive,
    send, call_next)`` convention must work when passed to ``app.use()``."""

    def test_asgi_middleware_via_app_use(self):
        """A hand-written ASGI middleware must be callable via ``app.use()``
        and must see every request."""
        app = BlackBull()
        calls: list[str] = []

        async def _x_request_id(scope, receive, send, call_next):
            calls.append('mw_enter')
            scope.state['x-request-id'] = 'test-001'
            await call_next(scope, receive, send)
            calls.append('mw_exit')

        app.use(_x_request_id)

        @app.route(path='/mw-test')
        async def mw_test(scope):
            return {'request_id': scope.state.get('x-request-id', '?')}

        with TestClient(app) as client:
            resp = client.get('/mw-test')
            assert resp.status_code == 200
            assert resp.json() == {'request_id': 'test-001'}
            assert calls == ['mw_enter', 'mw_exit'], (
                f'middleware call sequence wrong: {calls!r}')

    def test_asgi_middleware_can_short_circuit(self):
        """An ASGI middleware that does NOT call ``call_next`` must
        short-circuit the request — the route handler must never run."""
        app = BlackBull()
        handler_called = False

        async def _block_mw(scope, receive, send, call_next):
            # Short-circuit without calling call_next
            await send({
                'type': 'http.response.start',
                'status': 403,
                'headers': [(b'content-type', b'text/plain')],
            })
            await send({
                'type': 'http.response.body',
                'body': b'blocked',
            })

        app.use(_block_mw)

        @app.route(path='/secret')
        async def secret():
            nonlocal handler_called
            handler_called = True
            return 'you should not see this'

        with TestClient(app) as client:
            resp = client.get('/secret')
            assert resp.status_code == 403
            assert resp.text == 'blocked'
            assert not handler_called, (
                'handler must not run when middleware short-circuits')


# ---------------------------------------------------------------------------
# Extension + TestClient together (dogfooding)
# ---------------------------------------------------------------------------

class TestExtensionWithTestClient:
    """Full dogfooding: an ``init_app``-style extension exercised
    end-to-end through ``TestClient``."""

    def test_extension_registers_route_reachable_via_testclient(self):
        """A third-party package's extension registers a route; the
        route is reachable through TestClient."""
        app = BlackBull()

        class _HelloExtension:
            def init_app(self, a):
                a.extensions['hello'] = self

                @a.route(path='/hello-ext')
                async def hello():
                    return {'msg': 'hello from extension'}

                self._handler = hello

        _HelloExtension().init_app(app)

        with TestClient(app) as client:
            resp = client.get('/hello-ext')
            assert resp.status_code == 200
            assert resp.json() == {'msg': 'hello from extension'}

    def test_multiple_extensions_coexist(self):
        """Two extensions registering routes, middleware, and events
        must coexist without interference."""
        app = BlackBull()

        class _ExtA:
            def init_app(self, a):
                a.extensions['a'] = self
                a.use(self._mw)

                @a.route(path='/a')
                async def a_route():
                    return 'a'

                self._handler = a_route

            async def _mw(self, scope, receive, send, call_next):
                scope.state['x-ext-a'] = 'yes'
                await call_next(scope, receive, send)

        class _ExtB:
            def init_app(self, a):
                a.extensions['b'] = self

                @a.route(path='/b')
                async def b_route(scope):
                    return {'ext_a': scope.state.get('x-ext-a', 'no')}

                self._handler = b_route

        _ExtA().init_app(app)
        _ExtB().init_app(app)

        with TestClient(app) as client:
            assert client.get('/a').text == 'a'
            resp = client.get('/b')
            assert resp.json() == {'ext_a': 'yes'}, (
                '_ExtA middleware must inject x-ext-a before _ExtB route runs')
