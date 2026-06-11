"""Integration tests for error handling — on_error() and default error responses."""
from http import HTTPMethod, HTTPStatus

import pytest

from blackbull import BlackBull, JSONResponse
from blackbull.testing import TestClient


def _make_app() -> BlackBull:
    app = BlackBull()

    @app.route(path='/ok')
    async def ok():
        return {'ok': True}

    @app.route(path='/get-only', methods=[HTTPMethod.GET])
    async def get_only():
        return {}

    @app.route(path='/raises-value-error')
    async def raises_value_error():
        raise ValueError('bad input')

    @app.route(path='/raises-type-error')
    async def raises_type_error():
        raise TypeError('type problem')

    @app.route(path='/raises-runtime-error')
    async def raises_runtime_error():
        raise RuntimeError('something broke')

    @app.on_error(HTTPStatus.NOT_FOUND)
    async def custom_404(scope, receive, send):
        await send(JSONResponse({'error': 'not_found'}, status=HTTPStatus.NOT_FOUND))

    @app.on_error(ValueError)
    async def handle_value_error(scope, receive, send):
        await send(JSONResponse({'error': 'value_error'}, status=HTTPStatus.BAD_REQUEST))

    @app.on_error(Exception)
    async def handle_exception(scope, receive, send):
        await send(JSONResponse({'error': 'exception'}, status=HTTPStatus.INTERNAL_SERVER_ERROR))

    return app


@pytest.fixture(scope="module")
def client():
    with TestClient(_make_app()) as c:
        yield c


@pytest.mark.integration
def test_custom_404(client):
    r = client.get('/unknown-path')
    assert r.status_code == 404
    assert r.json() == {'error': 'not_found'}


@pytest.mark.integration
def test_default_405_has_allow_header(client):
    r = client.post('/get-only')
    assert r.status_code == 405
    assert 'allow' in r.headers


@pytest.mark.integration
def test_custom_exception_handler(client):
    r = client.get('/raises-value-error')
    assert r.status_code == 400
    assert r.json() == {'error': 'value_error'}


@pytest.mark.integration
def test_mro_dispatch_catches_subclass(client):
    """on_error(Exception) must catch TypeError even though only Exception is registered."""
    r = client.get('/raises-type-error')
    assert r.status_code == 500
    assert r.json() == {'error': 'exception'}


@pytest.mark.integration
def test_unhandled_exception_uses_fallback_500(client):
    """RuntimeError has no specific handler; the Exception base handler fires."""
    r = client.get('/raises-runtime-error')
    assert r.status_code == 500
