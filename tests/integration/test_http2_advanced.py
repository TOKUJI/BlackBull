"""Integration tests for HTTP/2 advanced features.

Server-push negotiation and priority hints require a real TLS + h2 connection
and cannot be verified over HTTP/1.1.
"""
import asyncio
import pathlib
import ssl
from multiprocessing import Process

import httpx
import pytest

from blackbull import BlackBull, Response

from .conftest import live_server


_CERT = pathlib.Path(__file__).parent.parent / 'cert.pem'
_KEY  = pathlib.Path(__file__).parent.parent / 'key.pem'


def _test_ssl_context() -> ssl.SSLContext:
    """SSLContext that validates against the self-signed test CA.

    Replaces ``verify=False`` in httpx calls: hostname checks and
    chain verification still run (against ``tests/cert.pem``), so
    CodeQL no longer flags the call as "Request without certificate
    validation" and we still get the protections cert validation
    is meant to provide — limited to talking to our own test server.
    """
    return ssl.create_default_context(cafile=str(_CERT))


def _make_push_app() -> BlackBull:
    app = BlackBull()

    @app.route(path='/page')
    async def page(conn, receive, send):
        # Push /style.css before sending the HTML response
        if 'http.response.push' in conn.extensions:
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
    async def priority_route(conn, receive, send):
        hint = conn.extensions.get('http.response.priority', {})
        await send({'type': 'http.response.start', 'status': 200,
                    'headers': [(b'content-type', b'application/json')]})
        import json
        await send({'type': 'http.response.body',
                    'body': json.dumps(hint).encode(), 'more_body': False})

    return app


def _make_stream_info_app() -> BlackBull:
    """Echoes the H/2 extensions surface a native handler sees.

    The legacy top-level ``http2_priority`` key is not read here because it
    no longer exists — removed in v0.75.0, and never present on the native
    lane before that.  Its absence is pinned in
    ``tests/unit/test_http2_extensions.py::TestLegacyAliasIsGone``.
    """
    app = BlackBull()

    @app.route(path='/stream-info')
    async def stream_info_route(conn, receive, send):
        import json
        ext = conn.extensions
        payload = {
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
    """The resource an application might push remains directly reachable.

    httpx refuses server push with SETTINGS_ENABLE_PUSH=0, so the application
    does not attempt a PUSH_PROMISE on this connection.
    """
    base = f'https://127.0.0.1:{push_app.port}'
    async with httpx.AsyncClient(http2=True, verify=False) as c:
        css = await c.get(f'{base}/style.css')
    assert css.status_code == 200
    assert b'body' in css.content


@pytest.mark.integration
@pytest.mark.asyncio
async def test_priority_hint_present(priority_app):
    async with httpx.AsyncClient(
        http2=True, verify=_test_ssl_context(),
        base_url=f'https://127.0.0.1:{priority_app.port}',
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
# http.response.priority + http.response.http2_stream extensions
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def stream_info_app(manage_cert_and_key):
    app = _make_stream_info_app()
    with live_server(app, certfile=str(_CERT), keyfile=str(_KEY)) as handle:
        yield handle


@pytest.mark.integration
@pytest.mark.asyncio
async def test_priority_extension_present(stream_info_app):
    """``conn.extensions['http.response.priority']`` is populated for every
    HTTP/2 request and carries the RFC 9218 urgency/incremental values."""
    async with httpx.AsyncClient(
        http2=True, verify=_test_ssl_context(),
        base_url=f'https://127.0.0.1:{stream_info_app.port}',
    ) as c:
        r = await c.get('/stream-info', headers={'priority': 'u=2, i'})
    assert r.status_code == 200
    body = r.json()

    assert body['priority_ext'] is not None
    assert body['priority_ext']['urgency'] == 2
    assert body['priority_ext']['incremental'] is True


@pytest.mark.integration
@pytest.mark.asyncio
async def test_http2_stream_extension_present(stream_info_app):
    """``conn.extensions['http.response.http2_stream']`` carries stream_id
    and the send-window snapshot."""
    async with httpx.AsyncClient(
        http2=True, verify=_test_ssl_context(),
        base_url=f'https://127.0.0.1:{stream_info_app.port}',
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
async def test_httpx_disable_push_omits_only_push_extension(stream_info_app):
    """A real peer refusal removes push without hiding other H/2 extensions.

    httpcore sends SETTINGS_ENABLE_PUSH=0 because it does not consume pushed
    responses.  This pins the server side of that real negotiation rather than
    assuming every HTTP/2 peer permits push.
    """
    async with httpx.AsyncClient(
        http2=True, verify=_test_ssl_context(),
        base_url=f'https://127.0.0.1:{stream_info_app.port}',
    ) as c:
        r = await c.get('/stream-info')
    keys = set(r.json()['extension_keys'])
    assert 'http.response.push' not in keys
    assert 'http.response.priority' in keys
    assert 'http.response.http2_stream' in keys
