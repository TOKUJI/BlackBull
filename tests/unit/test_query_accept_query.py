"""RFC 10008 §2.2 / §3 — QUERY normative response semantics (Sprint 78 P2).

A QUERY route may declare the request media types it accepts via the
``accept_query=[...]`` route option.  That drives two behaviours:

* **`Accept-Query` response header** — an RFC 9651 Structured Field list of
  the accepted media types, emitted on the route's responses (success and
  the 415 rejection) so a client can discover what to send.
* **Content-Type enforcement** on QUERY requests — 400 when the request
  carries no media type, 415 when the media type is not accepted (with the
  `Accept-Query` header so the client can correct), and 422 (raisable by the
  handler as :class:`UnprocessableQuery`) for a well-formed but semantically
  unprocessable query.

The 4xx responses flow through the normal error-router path, so custom
error handlers keep working.
"""
import pytest

from blackbull import BlackBull, QUERY, UnprocessableQuery
from blackbull.testing import TestClient


def _app():
    app = BlackBull()

    @app.route(path='/search', methods=[QUERY],
               accept_query=['application/sql', 'text/plain'])
    async def search(body: bytes):
        if body == b'BOOM':
            raise UnprocessableQuery('query references an unknown field')
        return body

    return app


class TestAcceptQueryHeader:
    def test_header_on_successful_query_response(self):
        with TestClient(_app()) as client:
            r = client.request('QUERY', '/search', content=b'select 1',
                               headers={'content-type': 'application/sql'})
            assert r.status_code == 200
            assert r.content == b'select 1'
            assert r.headers.get('accept-query') == 'application/sql, text/plain'

    def test_header_is_structured_field_list(self):
        """RFC 9651 SF list: comma-space separated tokens, no parameters."""
        with TestClient(_app()) as client:
            r = client.request('QUERY', '/search', content=b'x',
                               headers={'content-type': 'text/plain'})
            aq = r.headers['accept-query']
            assert aq == 'application/sql, text/plain'


class TestContentTypeEnforcement:
    def test_missing_content_type_is_400(self):
        with TestClient(_app()) as client:
            r = client.request('QUERY', '/search', content=b'select 1')
            assert r.status_code == 400

    def test_unsupported_media_type_is_415(self):
        with TestClient(_app()) as client:
            r = client.request('QUERY', '/search', content=b'{}',
                               headers={'content-type': 'application/json'})
            assert r.status_code == 415

    def test_415_carries_accept_query_header(self):
        """A 415 MUST advertise Accept-Query so the client can correct."""
        with TestClient(_app()) as client:
            r = client.request('QUERY', '/search', content=b'{}',
                               headers={'content-type': 'application/json'})
            assert r.status_code == 415
            assert r.headers.get('accept-query') == 'application/sql, text/plain'

    def test_supported_media_type_passes(self):
        with TestClient(_app()) as client:
            r = client.request('QUERY', '/search', content=b'select 1',
                               headers={'content-type': 'application/sql'})
            assert r.status_code == 200

    def test_media_type_parameters_are_ignored(self):
        """A charset parameter must not defeat the media-type match."""
        with TestClient(_app()) as client:
            r = client.request('QUERY', '/search', content=b'select 1',
                               headers={'content-type': 'application/sql; charset=utf-8'})
            assert r.status_code == 200

    def test_media_type_match_is_case_insensitive(self):
        with TestClient(_app()) as client:
            r = client.request('QUERY', '/search', content=b'select 1',
                               headers={'content-type': 'Application/SQL'})
            assert r.status_code == 200


class TestUnprocessableQuery:
    def test_handler_raises_422(self):
        with TestClient(_app()) as client:
            r = client.request('QUERY', '/search', content=b'BOOM',
                               headers={'content-type': 'application/sql'})
            assert r.status_code == 422

    def test_422_is_httpexception_subclass(self):
        from blackbull.router import HTTPException
        from http import HTTPStatus
        exc = UnprocessableQuery('nope')
        assert isinstance(exc, HTTPException)
        assert exc.status == HTTPStatus.UNPROCESSABLE_ENTITY


class TestEnforcementScope:
    def test_non_query_method_is_not_enforced(self):
        """accept_query enforcement targets QUERY; other methods registered on
        the path are not Content-Type-gated by it."""
        app = BlackBull()

        @app.route(path='/multi', methods=[QUERY, 'POST'],
                   accept_query=['application/sql'])
        async def multi(body: bytes):
            return body

        with TestClient(app) as client:
            # POST with no Content-Type is not rejected by the QUERY gate.
            r = client.post('/multi', content=b'hi')
            assert r.status_code == 200

    def test_route_without_accept_query_has_no_header(self):
        app = BlackBull()

        @app.route(path='/plain', methods=[QUERY])
        async def plain(body: bytes):
            return body

        with TestClient(app) as client:
            r = client.request('QUERY', '/plain', content=b'x')
            assert r.status_code == 200
            assert 'accept-query' not in r.headers


class TestCustomErrorHandlerStillWorks:
    def test_415_reaches_custom_error_handler(self):
        from http import HTTPStatus

        app = BlackBull()

        @app.route(path='/search', methods=[QUERY],
                   accept_query=['application/sql'])
        async def search(body: bytes):
            return body

        @app.on_error(HTTPStatus.UNSUPPORTED_MEDIA_TYPE)
        async def on_415(scope, receive, send):
            await send(b'custom 415', HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                       [(b'content-type', b'text/plain')])

        with TestClient(app) as client:
            r = client.request('QUERY', '/search', content=b'{}',
                               headers={'content-type': 'application/json'})
            assert r.status_code == 415
            assert r.content == b'custom 415'
            # The injector still adds Accept-Query beneath a custom handler.
            assert r.headers.get('accept-query') == 'application/sql'
