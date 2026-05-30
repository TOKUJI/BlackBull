"""Integration tests for URL routing — path parameters and named routes.

Unit tests mock the scope dict; these tests prove that the router, parser,
and simplified-handler wiring work together over a real TCP connection.
"""
import asyncio
import uuid
from multiprocessing import Process

import httpx
import pytest

from blackbull import BlackBull, JSONResponse
from .conftest import live_server


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
def live():
    app = _make_app()
    with live_server(app) as handle:
        yield handle
def _base(app) -> str:
    return f'http://127.0.0.1:{app.port}'


@pytest.mark.integration
@pytest.mark.asyncio
async def test_int_path_param(live):
    async with httpx.AsyncClient() as c:
        r = await c.get(f'{_base(live)}/users/42')
    assert r.status_code == 200
    assert r.json() == {'id': 42}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_str_path_param(live):
    async with httpx.AsyncClient() as c:
        r = await c.get(f'{_base(live)}/items/hello-world')
    assert r.status_code == 200
    assert r.json() == {'slug': 'hello-world'}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_uuid_path_param_valid(live):
    doc_id = '12345678-1234-5678-1234-567812345678'
    async with httpx.AsyncClient() as c:
        r = await c.get(f'{_base(live)}/docs/{doc_id}')
    assert r.status_code == 200
    assert r.json()['doc_id'] == doc_id


@pytest.mark.integration
@pytest.mark.asyncio
async def test_uuid_path_param_malformed_is_404(live):
    async with httpx.AsyncClient() as c:
        r = await c.get(f'{_base(live)}/docs/not-a-uuid')
    assert r.status_code == 404


@pytest.mark.integration
@pytest.mark.asyncio
async def test_path_converter_preserves_slashes(live):
    async with httpx.AsyncClient() as c:
        r = await c.get(f'{_base(live)}/files/a/b/c.txt')
    assert r.status_code == 200
    assert r.json() == {'path': 'a/b/c.txt'}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_named_route_url_for(live):
    assert live.url_path_for('search') == '/search'


@pytest.mark.integration
@pytest.mark.asyncio
async def test_unknown_route_404(live):
    async with httpx.AsyncClient() as c:
        r = await c.get(f'{_base(live)}/not-found')
    assert r.status_code == 404
