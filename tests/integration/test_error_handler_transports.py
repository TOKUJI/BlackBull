"""Error selection is identical over HTTP/1.1 and HTTP/2."""
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path

import httpx
import pytest

from blackbull import BlackBull, JSONResponse
from blackbull.env import reset_settings_cache
from blackbull.router import HTTPException
from .conftest import live_server


@dataclass
class Payload:
    value: int


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize('force_asgi', [False, True])
@pytest.mark.parametrize('http2', [False, True])
@pytest.mark.parametrize('exception_handler', [False, True])
async def test_error_handlers_across_transports(monkeypatch, force_asgi, http2, exception_handler):
    monkeypatch.setenv('BB_FORCE_ASGI_SCOPE', str(int(force_asgi)))
    reset_settings_cache()
    app = BlackBull()
    events = {}
    selected_handlers = {}
    for name in ('request_received', 'before_handler', 'after_handler', 'request_completed'):
        async def observe(event, name=name):
            path = event.detail['conn'].path
            if name == 'request_received':
                events[path] = []
            events.setdefault(path, []).append(name)
        app.on(name, blocking=True)(observe)

    async def render(conn, send, selected):
        selected_handlers[conn.path] = selected
        status = conn.state['error_status']
        exc = conn.state.get('error_exception')
        await send(JSONResponse({
            'selected': selected, 'status': int(status),
            'exception': type(exc).__name__ if exc else None,
            'allowed': list(conn.state.get('allowed_methods', ())),
            'events': list(events.get(conn.path, ())),
            'client': list(conn.client),
        }, status=status))

    async def by_status(conn, receive, send):
        await render(conn, send, 'status')

    async def by_exception(conn, receive, send):
        await render(conn, send, 'exception')

    for status in (400, 404, 405, 415, 500):
        app.on_error(status)(by_status)
    if exception_handler:
        app.on_error(Exception)(by_exception)

    @app.route(path='/ok')
    async def ok(conn):
        return {'client': list(conn.client)}

    @app.route(path='/events')
    async def recorded_events(target: str):
        return {'events': events[target], 'selected': selected_handlers.get(target)}

    @app.route(path='/late')
    async def late(conn, receive, send):
        await send(JSONResponse({'client': list(conn.client), 'finished': True}))
        raise ValueError('after response completion')

    @app.route(path='/boom')
    async def boom():
        raise ValueError('broken')

    @app.route(path='/http')
    async def http():
        raise HTTPException(HTTPStatus.BAD_REQUEST, 'bad')

    @app.route(path='/query')
    async def query(value: int):
        return str(value)

    @app.route(path='/body', methods=['POST'])
    async def body(body: Payload):
        return str(body.value)

    @app.route(path='/guard', methods=['QUERY'], accept_query=['application/json'])
    async def guard():
        return 'guard'

    cases = [
        ('GET', '/missing', 404, None), ('POST', '/ok', 405, None),
        ('GET', '/boom', 500, 'ValueError'), ('GET', '/http', 400, 'HTTPException'),
        ('GET', '/query', 400, 'HTTPException'), ('POST', '/body', 400, 'HTTPException'),
        ('QUERY', '/guard', 415, 'HTTPException'),
    ]
    certs = Path(__file__).parents[1]
    with live_server(app, certfile=str(certs / 'cert.pem'), keyfile=str(certs / 'key.pem')) as server:
        async with httpx.AsyncClient(http2=http2, verify=False, timeout=5, trust_env=False) as client:
            url = f'https://127.0.0.1:{server.port}'
            for method, path, status, exc in cases:
                response = await client.request(method, url + path, content=b'{invalid',
                                                headers={'content-type': 'text/plain'})
                assert response.http_version == ('HTTP/2' if http2 else 'HTTP/1.1')
                assert response.status_code == status
                actual = response.json()
                assert actual['selected'] == ('exception' if exception_handler and exc else 'status')
                assert actual['status'] == status
                assert actual['exception'] == exc
                if status == 405:
                    assert 'GET' in actual['allowed']
                assert actual['events'] == (['request_received'] if path in ('/missing', '/ok', '/guard')
                                           else ['request_received', 'before_handler', 'after_handler'])
                recorded = await client.get(url + '/events', params={'target': path})
                assert recorded.json()['events'] == actual['events'] + ['request_completed']
                followup = await client.get(url + '/ok')
                assert followup.status_code == 200
                assert followup.json()['client'] == actual['client']
            finished = await client.get(url + '/late')
            assert finished.status_code == 200
            assert finished.json()['finished'] is True
            recorded = await client.get(url + '/events', params={'target': '/late'})
            assert recorded.json() == {
                'events': ['request_received', 'before_handler', 'after_handler', 'request_completed'],
                'selected': 'exception' if exception_handler else 'status',
            }
            followup = await client.get(url + '/ok')
            assert followup.json()['client'] == finished.json()['client']
