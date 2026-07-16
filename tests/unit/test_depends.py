"""``Depends`` provider injection for simplified handlers (Sprint 74).

Registration-time resolution on the ``_adapt_handler`` seam: a parameter
whose *default value* is a ``Depends`` instance receives the provider's
value per request.  Async-generator providers get ``AsyncExitStack``-backed
teardown that runs *after* the response has been sent; plain async/sync
callables inject a value with no cleanup.  v1 fences: providers take no
parameters, and nested ``Depends`` is a registration-time ``TypeError``.
"""
from dataclasses import dataclass
from unittest.mock import AsyncMock

import pytest

from blackbull import BlackBull, Depends, Response
from blackbull.router import _adapt_handler
from blackbull.testing import TestClient

try:
    from beartype.roar import BeartypeCallHintParamViolation as _BeartypeViolation
except ImportError:  # pragma: no cover
    _BeartypeViolation = None
_TYPE_ERRORS = (TypeError,) if _BeartypeViolation is None else (TypeError, _BeartypeViolation)


def _scope(query: bytes = b'') -> dict:
    return {'type': 'http', 'method': 'GET', 'path': '/', 'headers': [],
            'query_string': query}


class TestDependsConstruction:
    def test_non_callable_provider_raises(self):
        # Plain TypeError from Depends itself, or beartype's violation (a
        # TypeError subclass) when --beartype-packages=blackbull is active.
        with pytest.raises(_TYPE_ERRORS):
            Depends(42)

    def test_provider_with_parameters_raises(self):
        async def provider(x): pass
        with pytest.raises(TypeError, match='parameter'):
            Depends(provider)

    def test_nested_depends_raises(self):
        async def inner(): return 1
        async def outer(db=Depends(inner)): return db
        with pytest.raises(TypeError, match='[Nn]ested'):
            Depends(outer)

    def test_sync_generator_provider_raises(self):
        def provider():
            yield 1
        with pytest.raises(TypeError, match='async generator'):
            Depends(provider)


class TestDependsRegistrationConflicts:
    def test_path_param_with_depends_default_raises(self):
        async def provider(): return 1
        async def fn(item_id=Depends(provider)): pass
        with pytest.raises(TypeError, match="item_id"):
            _adapt_handler(fn, '/items/{item_id}')

    def test_scope_name_with_depends_default_raises(self):
        async def provider(): return 1
        async def fn(scope=Depends(provider)): pass
        with pytest.raises(TypeError, match="scope"):
            _adapt_handler(fn, '/')

    def test_body_name_with_depends_default_raises(self):
        async def provider(): return 1
        async def fn(body=Depends(provider)): pass
        with pytest.raises(TypeError, match="body"):
            _adapt_handler(fn, '/')


class TestDependsInjection:
    @pytest.mark.asyncio
    async def test_async_function_provider_value_injected(self):
        captured = {}
        async def provider(): return {'conn': 1}
        async def fn(db=Depends(provider)): captured['db'] = db
        wrapper = _adapt_handler(fn, '/')
        await wrapper(_scope(), None, AsyncMock())
        assert captured['db'] == {'conn': 1}

    @pytest.mark.asyncio
    async def test_sync_function_provider_value_injected(self):
        captured = {}
        def provider(): return 'cfg'
        async def fn(cfg=Depends(provider)): captured['cfg'] = cfg
        wrapper = _adapt_handler(fn, '/')
        await wrapper(_scope(), None, AsyncMock())
        assert captured['cfg'] == 'cfg'

    @pytest.mark.asyncio
    async def test_async_generator_provider_value_injected(self):
        captured = {}
        async def provider():
            yield 'pool'
        async def fn(db=Depends(provider)): captured['db'] = db
        wrapper = _adapt_handler(fn, '/')
        await wrapper(_scope(), None, AsyncMock())
        assert captured['db'] == 'pool'

    @pytest.mark.asyncio
    async def test_cleanup_runs_after_response_send(self):
        events = []

        async def provider():
            events.append('setup')
            try:
                yield 'db'
            finally:
                events.append('cleanup')

        async def fn(db=Depends(provider)):
            events.append('handler')
            return Response(b'ok')

        async def recording_send(event):
            events.append('send')

        wrapper = _adapt_handler(fn, '/')
        await wrapper(_scope(), None, recording_send)
        assert events == ['setup', 'handler', 'send', 'cleanup']

    @pytest.mark.asyncio
    async def test_two_providers_clean_up_lifo(self):
        events = []

        async def first():
            try:
                yield 'a'
            finally:
                events.append('first-cleanup')

        async def second():
            try:
                yield 'b'
            finally:
                events.append('second-cleanup')

        async def fn(a=Depends(first), b=Depends(second)):
            return Response(b'ok')

        wrapper = _adapt_handler(fn, '/')
        await wrapper(_scope(), None, AsyncMock())
        assert events == ['second-cleanup', 'first-cleanup']

    @pytest.mark.asyncio
    async def test_cleanup_runs_on_handler_exception(self):
        events = []

        async def provider():
            try:
                yield 'db'
            finally:
                events.append('cleanup')

        async def fn(db=Depends(provider)):
            raise RuntimeError('boom')

        wrapper = _adapt_handler(fn, '/')
        with pytest.raises(RuntimeError, match='boom'):
            await wrapper(_scope(), None, AsyncMock())
        assert events == ['cleanup']

    @pytest.mark.asyncio
    async def test_provider_exception_propagates(self):
        async def provider():
            raise ValueError('no pool')
            yield  # pragma: no cover

        async def fn(db=Depends(provider)):
            return Response(b'never')

        wrapper = _adapt_handler(fn, '/')
        with pytest.raises(ValueError, match='no pool'):
            await wrapper(_scope(), None, AsyncMock())

    @pytest.mark.asyncio
    async def test_use_cache_shares_one_instance_per_request(self):
        calls = []

        async def provider():
            calls.append(True)
            return object()

        captured = {}
        async def fn(a=Depends(provider), b=Depends(provider)):
            captured.update(a=a, b=b)

        wrapper = _adapt_handler(fn, '/')
        await wrapper(_scope(), None, AsyncMock())
        assert captured['a'] is captured['b']
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_use_cache_false_calls_provider_per_param(self):
        calls = []

        async def provider():
            calls.append(True)
            return object()

        captured = {}
        async def fn(a=Depends(provider, use_cache=False),
                     b=Depends(provider, use_cache=False)):
            captured.update(a=a, b=b)

        wrapper = _adapt_handler(fn, '/')
        await wrapper(_scope(), None, AsyncMock())
        assert captured['a'] is not captured['b']
        assert len(calls) == 2

    @pytest.mark.asyncio
    async def test_fresh_instance_per_request(self):
        calls = []

        async def provider():
            calls.append(True)
            return object()

        seen = []
        async def fn(db=Depends(provider)): seen.append(db)

        wrapper = _adapt_handler(fn, '/')
        await wrapper(_scope(), None, AsyncMock())
        await wrapper(_scope(), None, AsyncMock())
        assert len(calls) == 2
        assert seen[0] is not seen[1]

    @pytest.mark.asyncio
    async def test_dataclass_annotated_depends_param_is_di_not_body(self):
        @dataclass
        class Pool:
            dsn: str

        def provider(): return Pool(dsn='x')

        captured = {}
        called = []

        async def should_not_be_called():
            called.append(True)
            return {'type': 'http.request', 'body': b'', 'more_body': False}

        async def fn(db: Pool = Depends(provider)): captured['db'] = db

        wrapper = _adapt_handler(fn, '/')
        await wrapper(_scope(), should_not_be_called, AsyncMock())
        assert captured['db'] == Pool(dsn='x')
        assert called == []

    @pytest.mark.asyncio
    async def test_depends_alongside_path_query_and_body(self):
        def provider(): return 'svc'
        captured = {}

        async def fn(item_id: int, q: str, body: bytes, svc=Depends(provider)):
            captured.update(item_id=item_id, q=q, body=body, svc=svc)

        async def receive():
            return {'type': 'http.request', 'body': b'payload', 'more_body': False}

        wrapper = _adapt_handler(fn, '/items/{item_id}')
        scope = _scope(b'q=bull')
        scope['path_params'] = {'item_id': '5'}
        await wrapper(scope, receive, AsyncMock())
        assert captured == {'item_id': 5, 'q': 'bull', 'body': b'payload', 'svc': 'svc'}


class TestDependsEndToEnd:
    def test_db_pool_pattern_through_the_full_stack(self):
        app = BlackBull()
        events = []

        async def get_db():
            events.append('setup')
            try:
                yield {'users': {1: 'ada'}}
            finally:
                events.append('cleanup')

        @app.route(path='/users/{uid:int}')
        async def get_user(uid: int, db=Depends(get_db)):
            return {'name': db['users'][uid]}

        with TestClient(app) as client:
            r = client.get('/users/1')
            assert r.status_code == 200
            assert r.json() == {'name': 'ada'}
        assert events == ['setup', 'cleanup']

    def test_provider_exception_maps_to_500(self):
        app = BlackBull()

        async def get_db():
            raise ValueError('no pool')
            yield  # pragma: no cover

        @app.route(path='/broken')
        async def broken(db=Depends(get_db)):
            return 'never'

        with TestClient(app, raise_app_exceptions=False) as client:
            r = client.get('/broken')
            assert r.status_code == 500
