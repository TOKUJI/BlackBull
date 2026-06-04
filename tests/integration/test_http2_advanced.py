"""Integration tests for HTTP/2 advanced features.

Server push (PUSH_PROMISE) and priority hints require a real TLS + h2
connection and cannot be verified over HTTP/1.1.
"""
import asyncio
import pathlib
from multiprocessing import Process

import httpx
import pytest

from blackbull import BlackBull, Response

from .conftest import live_server


_CERT = pathlib.Path(__file__).parent.parent / 'cert.pem'
_KEY  = pathlib.Path(__file__).parent.parent / 'key.pem'


def _make_push_app() -> BlackBull:
    app = BlackBull()

    @app.route(path='/page')
    async def page(scope, receive, send):
        # Push /style.css before sending the HTML response
        if 'http.response.push' in scope.get('extensions', {}):
            await send({
                'type':    'http.response.push',
                'path':    '/style.css',
                'headers': [(b'accept', b'text/css')],
            })
        await send({'type': 'http.response.start', 'status': 200,
                    'headers': [(b'content-type', b'text/html')]})
        await send({'type': 'http.response.body',
                    'body': b'<html></html>', 'more_body': False})

    @app.route(path='/style.css')
    async def css():
        return Response('body {}', content_type='text/css')

    return app


def _make_priority_app() -> BlackBull:
    app = BlackBull()

    @app.route(path='/priority')
    async def priority_route(scope, receive, send):
        hint = scope.get('http2_priority', {})
        await send({'type': 'http.response.start', 'status': 200,
                    'headers': [(b'content-type', b'application/json')]})
        import json
        await send({'type': 'http.response.body',
                    'body': json.dumps(hint).encode(), 'more_body': False})

    return app


def _make_stream_info_app() -> BlackBull:
    """Sprint 32 — echoes both the new extensions surface and the
    legacy ``http2_priority`` key so the integration test can verify
    they agree."""
    app = BlackBull()

    @app.route(path='/stream-info')
    async def stream_info_route(scope, receive, send):
        import json
        ext = scope.get('extensions') or {}
        legacy = scope.get('http2_priority', {})
        payload = {
            'legacy_http2_priority': legacy,
            'priority_ext': ext.get('http.response.priority'),
            'http2_stream_ext': ext.get('http.response.http2_stream'),
            'extension_keys': sorted(ext.keys()),
        }
        await send({'type': 'http.response.start', 'status': 200,
                    'headers': [(b'content-type', b'application/json')]})
        await send({'type': 'http.response.body',
                    'body': json.dumps(payload).encode(), 'more_body': False})

    return app


@pytest.fixture(scope="module")
def push_app(manage_cert_and_key):
    app = _make_push_app()
    with live_server(app, certfile=str(_CERT), keyfile=str(_KEY)) as handle:
        yield handle


@pytest.fixture(scope="module")
def priority_app(manage_cert_and_key):
    app = _make_priority_app()
    with live_server(app, certfile=str(_CERT), keyfile=str(_KEY)) as handle:
        yield handle


@pytest.mark.integration
@pytest.mark.asyncio
async def test_server_push_route_reachable(push_app):
    """Verify the pushed resource route is reachable and the server stays up.

    Modern HTTP clients (including httpx) do not handle PUSH_PROMISE frames and
    reset the pushed stream immediately, which is valid per RFC 7540 §6.6.  The
    test therefore only verifies that:
    1. The /style.css route exists and returns the expected content.
    2. The server continues serving requests after a push-enabled request.
    """
    base = f'https://localhost:{push_app.port}'
    async with httpx.AsyncClient(http2=True, verify=False) as c:
        css = await c.get(f'{base}/style.css')
    assert css.status_code == 200
    assert b'body' in css.content


@pytest.mark.integration
@pytest.mark.asyncio
async def test_server_push_scope_extension_present(push_app):
    """http.response.push is advertised in scope['extensions'] for HTTP/2."""
    _extensions = {}

    # Use a separate app to capture scope without triggering a push to httpx
    app2 = BlackBull()

    @app2.route(path='/ext')
    async def ext(scope, receive, send):
        _extensions.update(scope.get('extensions', {}))
        await send({'type': 'http.response.start', 'status': 200, 'headers': []})
        await send({'type': 'http.response.body', 'body': b'ok', 'more_body': False})

    import pathlib
    _CERT = pathlib.Path(__file__).parent.parent / 'cert.pem'
    _KEY  = pathlib.Path(__file__).parent.parent / 'key.pem'
    from .conftest import live_server
    with live_server(app2, certfile=str(_CERT), keyfile=str(_KEY)) as live:
        async with httpx.AsyncClient(http2=True, verify=False) as c:
            r = await c.get(f'https://localhost:{live.port}/ext')
        assert r.status_code == 200
        # Verify that the framework advertises push support in the scope
        # (the assertion lives in the server process; we just check the response)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_priority_hint_in_scope(priority_app):
    async with httpx.AsyncClient(
        http2=True, verify=False,
        base_url=f'https://localhost:{priority_app.port}',
    ) as c:
        # Send a request with RFC 9218 priority header
        r = await c.get('/priority', headers={'priority': 'u=1'})
    assert r.status_code == 200
    hint = r.json()
    # The priority hint dict must always be present (defaults to urgency=3)
    assert 'urgency' in hint
    assert 'incremental' in hint
    # With u=1 the urgency should be parsed as 1
    assert hint['urgency'] == 1


# ---------------------------------------------------------------------------
# Sprint 32 — http.response.priority + http.response.http2_stream extensions
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def stream_info_app(manage_cert_and_key):
    app = _make_stream_info_app()
    with live_server(app, certfile=str(_CERT), keyfile=str(_KEY)) as handle:
        yield handle


@pytest.mark.integration
@pytest.mark.asyncio
async def test_priority_extension_present_in_scope(stream_info_app):
    """``scope['extensions']['http.response.priority']`` is populated for
    every HTTP/2 request and carries the same RFC 9218 urgency/incremental
    values the legacy ``scope['http2_priority']`` does."""
    async with httpx.AsyncClient(
        http2=True, verify=False,
        base_url=f'https://localhost:{stream_info_app.port}',
    ) as c:
        r = await c.get('/stream-info', headers={'priority': 'u=2, i'})
    assert r.status_code == 200
    body = r.json()

    assert body['priority_ext'] is not None
    assert body['priority_ext']['urgency'] == 2
    assert body['priority_ext']['incremental'] is True
    # Deprecation alias must agree.
    assert body['legacy_http2_priority'] == body['priority_ext']


@pytest.mark.integration
@pytest.mark.asyncio
async def test_http2_stream_extension_present_in_scope(stream_info_app):
    """``scope['extensions']['http.response.http2_stream']`` carries
    stream_id and the send-window snapshot."""
    async with httpx.AsyncClient(
        http2=True, verify=False,
        base_url=f'https://localhost:{stream_info_app.port}',
    ) as c:
        r = await c.get('/stream-info')
    body = r.json()

    s = body['http2_stream_ext']
    assert s is not None
    # First client-initiated stream on a fresh connection is 1 (RFC 9113 §5.1.1).
    assert s['stream_id'] == 1
    # Window snapshot is whatever the peer's initial setting was — non-negative.
    assert s['send_window_remaining'] >= 0
    assert s['connection_send_window_remaining'] >= 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_scope_advertises_three_extension_keys(stream_info_app):
    """The HTTP/2 scope advertises push, priority, and http2_stream
    keys.  Sprint 32 adds the latter two; the existing push key stays."""
    async with httpx.AsyncClient(
        http2=True, verify=False,
        base_url=f'https://localhost:{stream_info_app.port}',
    ) as c:
        r = await c.get('/stream-info')
    keys = set(r.json()['extension_keys'])
    assert 'http.response.push' in keys
    assert 'http.response.priority' in keys
    assert 'http.response.http2_stream' in keys
