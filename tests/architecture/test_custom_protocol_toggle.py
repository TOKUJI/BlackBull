"""Integration tests for the BB_USE_CUSTOM_PROTOCOL toggle.

Sprint 30 Tier 1.5 Step 3.  Drives a real ``ASGIServer`` on an
ephemeral port with the toggle on and exercises a request/response
through the new ``_BlackBullProtocol`` code path.  Confirms that:

- existing tests keep passing with the toggle off (asserted by the
  global suite; not re-asserted here);
- with the toggle on, the same ASGI app produces the same wire
  output as the default path.

We use ``conftest.set_setting`` to override the env knob in the
forked test subprocess — see ``conftest.py`` for the cache-reset
helper.  Local ``BB_USE_CUSTOM_PROTOCOL`` env injection via
``monkeypatch.setenv`` works because ``Settings.from_env()`` reads
``os.environ`` lazily and we call ``reset_settings_cache`` first.
"""
from __future__ import annotations

import asyncio
import socket

import pytest

from blackbull.env import get_settings, reset_settings_cache


# Minimal ASGI app — returns 200 OK with a constant body.  We don't
# need full BlackBull routing here; we just want to drive the
# protocol stack and confirm bytes go through.
async def _hello_app(scope, receive, send):
    if scope.get('type') != 'http':
        return
    await send({
        'type': 'http.response.start',
        'status': 200,
        'headers': [(b'content-type', b'text/plain'),
                    (b'content-length', b'5')],
    })
    await send({'type': 'http.response.body', 'body': b'hello'})


def _http_get(host: str, port: int, path: str = '/', *,
              timeout: float = 5.0) -> bytes:
    """Synchronous raw-socket HTTP/1.1 GET — returns the raw response
    bytes.  Closes the connection after reading.

    We use a raw socket (not httpx) so the test doesn't pull in async
    plumbing that obscures whether the server path itself is healthy.
    """
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.sendall(
            f'GET {path} HTTP/1.1\r\n'
            f'Host: {host}\r\n'
            f'Connection: close\r\n'
            f'\r\n'.encode())
        chunks: list[bytes] = []
        while True:
            try:
                chunk = sock.recv(4096)
            except socket.timeout:
                break
            if not chunk:
                break
            chunks.append(chunk)
        return b''.join(chunks)


@pytest.fixture
def reset_cache():
    """Reset Settings cache so test env-var overrides take effect."""
    reset_settings_cache()
    yield
    reset_settings_cache()


@pytest.mark.asyncio
async def test_toggle_off_uses_default_path(monkeypatch, reset_cache):
    """Sanity: with the toggle off (default), the server boots and
    responds normally.  Same coverage as the rest of the suite, but
    proves the toggle-off path explicitly in this test file too."""
    monkeypatch.setenv('BB_USE_CUSTOM_PROTOCOL', '0')
    reset_settings_cache()
    cfg = get_settings()
    assert cfg.use_custom_protocol is False

    from blackbull.server.server import ASGIServer
    server = ASGIServer(_hello_app, max_connections=0)
    server.open_socket(port=0)
    # Pull the actual port the kernel assigned.
    assigned_port = server.raw_sockets[0].getsockname()[1]

    serve_task = asyncio.create_task(server.run())
    await asyncio.sleep(0.1)   # let the server start accepting

    try:
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(
            None, lambda: _http_get('127.0.0.1', assigned_port))
        assert b'HTTP/1.1 200 OK' in response
        assert response.endswith(b'hello')
    finally:
        serve_task.cancel()
        try:
            await serve_task
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_toggle_on_uses_custom_protocol_and_responds(
        monkeypatch, reset_cache):
    """With ``BB_USE_CUSTOM_PROTOCOL=1``, requests go through
    ``_BlackBullProtocol`` + ``ProtocolBuffer`` and still produce a
    valid response.  This is the integration check for Step 3's
    wiring."""
    monkeypatch.setenv('BB_USE_CUSTOM_PROTOCOL', '1')
    reset_settings_cache()
    cfg = get_settings()
    assert cfg.use_custom_protocol is True

    from blackbull.server.server import ASGIServer
    server = ASGIServer(_hello_app, max_connections=0)
    server.open_socket(port=0)
    assigned_port = server.raw_sockets[0].getsockname()[1]

    serve_task = asyncio.create_task(server.run())
    await asyncio.sleep(0.1)

    try:
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(
            None, lambda: _http_get('127.0.0.1', assigned_port))
        # Same wire output as the default-path test above.
        assert b'HTTP/1.1 200 OK' in response, \
            f'unexpected response: {response[:120]!r}'
        assert response.endswith(b'hello'), \
            f'response did not end with body: {response[-30:]!r}'
    finally:
        serve_task.cancel()
        try:
            await serve_task
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_toggle_on_multiple_requests_on_separate_connections(
        monkeypatch, reset_cache):
    """Two requests in series on fresh connections — the custom
    protocol must accept N connections, not just one."""
    monkeypatch.setenv('BB_USE_CUSTOM_PROTOCOL', '1')
    reset_settings_cache()

    from blackbull.server.server import ASGIServer
    server = ASGIServer(_hello_app, max_connections=0)
    server.open_socket(port=0)
    assigned_port = server.raw_sockets[0].getsockname()[1]

    serve_task = asyncio.create_task(server.run())
    await asyncio.sleep(0.1)

    try:
        loop = asyncio.get_event_loop()
        for _ in range(3):
            response = await loop.run_in_executor(
                None, lambda: _http_get('127.0.0.1', assigned_port))
            assert b'HTTP/1.1 200 OK' in response
            assert response.endswith(b'hello')
    finally:
        serve_task.cancel()
        try:
            await serve_task
        except asyncio.CancelledError:
            pass
