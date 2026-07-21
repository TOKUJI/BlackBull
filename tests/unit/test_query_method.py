"""HTTP QUERY method support (RFC 10008) — Sprint 78 P1.

QUERY (RFC 10008, June 2026) is the first new standard HTTP method since
PATCH: safe + idempotent + cacheable, **with a request body**.  The stdlib
``http.HTTPMethod`` enum has no ``QUERY`` member (RFC 10008 postdates the
3.15 feature freeze), so the package exports a plain-string constant
``blackbull.QUERY`` usable anywhere a method is accepted.

These tests pin the string-form routing path (register → dispatch → 405
Allow) and the forward-compat contract: if CPython later grows
``HTTPMethod.QUERY``, ``StrEnum`` equality must keep string-registered
routes matching enum-coerced dispatch keys with no migration.
"""
from enum import StrEnum
from http import HTTPMethod

import pytest

from blackbull import BlackBull, Connection as Request, QUERY  # Sprint 79 alias
from blackbull.router import Router, MethodNotApplicable
from blackbull.utils import Scheme
from blackbull.testing import TestClient


class _FutureHTTPMethod(StrEnum):
    """Stand-in for a future ``http.HTTPMethod.QUERY`` member (≥3.16)."""
    QUERY = 'QUERY'


# ---------------------------------------------------------------------------
# The exported constant
# ---------------------------------------------------------------------------

class TestQueryConstant:
    def test_value(self):
        assert QUERY == 'QUERY'

    def test_is_str(self):
        # Plain str, not an enum: usable as a dict key interchangeably with
        # a future StrEnum member of the same value.
        assert isinstance(QUERY, str)

    def test_equal_to_future_enum_member(self):
        assert QUERY == _FutureHTTPMethod.QUERY
        assert hash(QUERY) == hash(_FutureHTTPMethod.QUERY)


# ---------------------------------------------------------------------------
# Router registration + lookup (string form)
# ---------------------------------------------------------------------------

class TestRouterQuery:
    def _router_with_query_route(self):
        router = Router()
        async def handler(scope, receive, send): ...
        router[('/q', QUERY)] = handler
        return router, handler

    def test_register_and_lookup(self):
        router, handler = self._router_with_query_route()
        assert router[('/q', 'QUERY', Scheme.http)] is handler

    def test_lookup_with_future_enum_member(self):
        """Forward-compat: when CPython adds HTTPMethod.QUERY, _dispatch will
        coerce 'QUERY' to the enum member; the string-registered route must
        still match (StrEnum hash/eq contract)."""
        router, handler = self._router_with_query_route()
        assert router[('/q', _FutureHTTPMethod.QUERY, Scheme.http)] is handler

    def test_register_with_enum_lookup_with_str(self):
        """The reverse direction: enum-registered, string-dispatched."""
        router = Router()
        async def handler(scope, receive, send): ...
        router[('/q', _FutureHTTPMethod.QUERY)] = handler
        assert router[('/q', 'QUERY', Scheme.http)] is handler

    def test_query_on_get_only_path_raises_405(self):
        router = Router()
        async def handler(scope, receive, send): ...
        router[('/g', HTTPMethod.GET)] = handler
        with pytest.raises(MethodNotApplicable) as exc:
            router[('/g', QUERY, Scheme.http)]
        assert QUERY not in exc.value.allowed_methods

    def test_allowed_methods_includes_query(self):
        """405 on a QUERY-registered path must advertise QUERY in Allow."""
        router, handler = self._router_with_query_route()
        with pytest.raises(MethodNotApplicable) as exc:
            router[('/q', HTTPMethod.POST, Scheme.http)]
        assert 'QUERY' in {str(m) for m in exc.value.allowed_methods}


# ---------------------------------------------------------------------------
# End-to-end dispatch through TestClient
# ---------------------------------------------------------------------------

class TestQueryDispatch:
    def _app(self):
        app = BlackBull()

        @app.route(path='/search', methods=[QUERY])
        async def search(body: bytes):
            return body

        @app.route(path='/json', methods=[QUERY])
        async def search_json(request: Request):
            data = await request.json()
            return {'echo': data}

        @app.route(path='/getonly')
        async def getonly():
            return 'ok'

        return app

    def test_query_dispatches_with_body(self):
        with TestClient(self._app()) as client:
            r = client.request('QUERY', '/search', content=b'select *')
            assert r.status_code == 200
            assert r.content == b'select *'

    def test_query_request_json(self):
        with TestClient(self._app()) as client:
            r = client.request('QUERY', '/json', json={'q': 'bull'})
            assert r.status_code == 200
            assert r.json() == {'echo': {'q': 'bull'}}

    def test_query_on_get_only_route_is_405(self):
        with TestClient(self._app()) as client:
            r = client.request('QUERY', '/getonly')
            assert r.status_code == 405

    def test_405_allow_header_includes_query(self):
        with TestClient(self._app()) as client:
            r = client.post('/search')
            assert r.status_code == 405
            allow = r.headers.get('allow', '')
            assert 'QUERY' in {m.strip() for m in allow.split(',')}

    def test_get_routes_reports_query(self):
        routes = {(ri.method, ri.path) for ri in self._app().get_routes()}
        assert ('QUERY', '/search') in routes


# ---------------------------------------------------------------------------
# OpenAPI: QUERY routes are excluded under the 3.1 emitter (Sprint 78 P3)
# ---------------------------------------------------------------------------

class TestQueryOpenAPI:
    def test_query_route_excluded_from_31_spec(self):
        """OpenAPI 3.1 has no `query` operation (3.2 does).  Until the
        emitter moves to 3.2, QUERY routes stay out of the spec — and must
        never be faked as another operation."""
        from blackbull.openapi import generate_spec

        app = BlackBull()

        @app.route(path='/search', methods=[QUERY, HTTPMethod.GET])
        async def search():
            return 'ok'

        spec = generate_spec(app)
        assert spec['openapi'].startswith('3.1')
        ops = set(spec['paths']['/search'])
        assert 'get' in ops
        assert 'query' not in ops
        assert ops == {'get'}
