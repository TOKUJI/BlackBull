"""HEAD must reach the GET handler on *both* dispatch lanes (RFC 9110 §9.3.2).

BlackBull synthesises a HEAD response by rewriting the request's method to
``GET`` and stripping body bytes on the way out.  The native lane threads the
live :class:`Connection`, so the rewrite is visible to the router by reference.
The ``BB_FORCE_ASGI_SCOPE=1`` lane hands the app a *snapshot* dict instead —
so the snapshot has to be taken after the rewrite, not before, or the router
sees ``HEAD``, finds no HEAD route, and answers 405.

Both lanes must produce the same status.  That identity is the whole point of
the dual-path lane: the compat conversion is only faithful if it is invisible.
"""
import asyncio
from http import HTTPStatus

import pytest

from blackbull import BlackBull
from blackbull.env import reset_settings_cache
from blackbull.server.http1_actor import HTTP1Actor

from .test_http1_dispatch import _FakeReader, _FakeWriter


HEAD_REQUEST = b'HEAD / HTTP/1.1\r\nHost: localhost\r\n\r\n'
GET_REQUEST = b'GET / HTTP/1.1\r\nHost: localhost\r\n\r\n'


@pytest.fixture
def app():
    a = BlackBull()

    @a.route(path='/')
    async def _root():
        return 'hello'

    return a


async def _drive(app, request: bytes) -> bytes:
    # The head is handed over via ``request=``; the reader is left empty so the
    # keep-alive loop sees EOF and serves exactly one response.  Seeding the
    # reader with the same bytes would serve the request twice.
    reader, writer = _FakeReader(b''), _FakeWriter()
    actor = HTTP1Actor(reader, writer, app, None, request=request)
    await asyncio.wait_for(actor.run(), timeout=5.0)
    return bytes(writer.written)


def _status(raw: bytes) -> int:
    return int(raw.split(b' ', 2)[1])


@pytest.fixture
def forced_asgi(monkeypatch):
    monkeypatch.setenv('BB_FORCE_ASGI_SCOPE', '1')
    reset_settings_cache()
    yield
    monkeypatch.delenv('BB_FORCE_ASGI_SCOPE', raising=False)
    reset_settings_cache()


@pytest.mark.asyncio
async def test_head_reaches_the_get_handler_natively(app):
    raw = await _drive(app, HEAD_REQUEST)
    assert _status(raw) == HTTPStatus.OK


@pytest.mark.asyncio
async def test_head_reaches_the_get_handler_under_forced_asgi_scope(app, forced_asgi):
    raw = await _drive(app, HEAD_REQUEST)
    assert _status(raw) == HTTPStatus.OK, (
        'HEAD was routed as HEAD on the forced-ASGI lane — the scope snapshot '
        'was taken before the HEAD->GET rewrite')


@pytest.mark.asyncio
async def test_both_lanes_agree_on_the_head_status(app, monkeypatch):
    native = await _drive(app, HEAD_REQUEST)
    monkeypatch.setenv('BB_FORCE_ASGI_SCOPE', '1')
    reset_settings_cache()
    try:
        forced = await _drive(app, HEAD_REQUEST)
    finally:
        monkeypatch.delenv('BB_FORCE_ASGI_SCOPE', raising=False)
        reset_settings_cache()
    assert _status(native) == _status(forced)


@pytest.mark.asyncio
async def test_head_response_carries_no_body_under_forced_asgi_scope(app, forced_asgi):
    """§9.3.2 — headers identical to the GET response, body absent."""
    raw = await _drive(app, HEAD_REQUEST)
    head, _, body = raw.partition(b'\r\n\r\n')
    assert body == b''
    # The Content-Length still describes what a GET would have returned.
    assert b'content-length: 5' in head.lower()
