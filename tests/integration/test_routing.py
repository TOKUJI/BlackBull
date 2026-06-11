"""Integration tests for URL routing — path parameters and named routes.

These run in-process through :class:`blackbull.testing.TestClient`; the
router, parser, and simplified-handler wiring are exercised end-to-end
but no TCP socket is bound.  See ``tests/conformance/`` for the
real-wire equivalents.
"""
import uuid

import pytest

from blackbull import BlackBull
from blackbull.testing import TestClient


def _make_app() -> BlackBull:
    app = BlackBull()

    @app.route(path='/users/{id:int}')
    async def get_user(id: int):
        return {'id': id}

    @app.route(path='/items/{slug:str}')
    async def get_item(slug: str):
        return {'slug': slug}

    @app.route(path='/docs/{doc_id:uuid}')
    async def get_doc(doc_id: uuid.UUID):
        return {'doc_id': str(doc_id)}

    @app.route(path='/files/{filepath:path}')
    async def get_file(filepath: str):
        return {'path': filepath}

    @app.route(path='/search', name='search')
    async def search():
        return {'results': []}

    return app


@pytest.fixture(scope="module")
def client():
    with TestClient(_make_app()) as c:
        yield c


@pytest.mark.integration
def test_int_path_param(client):
    r = client.get('/users/42')
    assert r.status_code == 200
    assert r.json() == {'id': 42}


@pytest.mark.integration
def test_str_path_param(client):
    r = client.get('/items/hello-world')
    assert r.status_code == 200
    assert r.json() == {'slug': 'hello-world'}


@pytest.mark.integration
def test_uuid_path_param_valid(client):
    doc_id = '12345678-1234-5678-1234-567812345678'
    r = client.get(f'/docs/{doc_id}')
    assert r.status_code == 200
    assert r.json()['doc_id'] == doc_id


@pytest.mark.integration
def test_uuid_path_param_malformed_is_404(client):
    r = client.get('/docs/not-a-uuid')
    assert r.status_code == 404


@pytest.mark.integration
def test_path_converter_preserves_slashes(client):
    r = client.get('/files/a/b/c.txt')
    assert r.status_code == 200
    assert r.json() == {'path': 'a/b/c.txt'}


@pytest.mark.integration
def test_named_route_url_for(client):
    assert client.app.url_path_for('search') == '/search'


@pytest.mark.integration
def test_unknown_route_404(client):
    r = client.get('/not-found')
    assert r.status_code == 404
