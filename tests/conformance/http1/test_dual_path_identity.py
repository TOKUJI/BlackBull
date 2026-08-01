"""The two dispatch lanes must be indistinguishable on the wire.

BlackBull threads a native :class:`Connection` end to end.  ``BB_FORCE_ASGI_SCOPE=1``
takes the compat lane instead: the server converts ``Connection -> scope`` and the
app converts it back with ``from_scope`` at dispatch, so both directions of the ASGI
conversion are exercised on every request.  The lane exists to keep that conversion
from bitrotting, and it only does its job if the conversion is *invisible*.

So the invariant is not "the compat lane works" but **"the compat lane produces
byte-identical output"** — for every request shape, including the ones that are
rejected before routing.  The `HEAD` regression this file was written after
passed every HEAD test on the native lane and answered 405 on the compat lane,
because the scope snapshot was taken before the HEAD->GET rewrite.  Anything
that mutates the Connection between parse and dispatch can reintroduce that
class of divergence; this asserts the whole corpus rather than one method.
"""
import asyncio

import pytest

from blackbull import BlackBull
from blackbull.env import reset_settings_cache
from blackbull.server.http1_actor import HTTP1Actor

from ._dual_path_corpus import CORPUS, normalise as _normalise
from .test_http1_dispatch import _FakeReader, _FakeWriter


@pytest.fixture
def app():
    a = BlackBull()

    @a.route(path='/')
    async def _root():
        return 'hello'

    @a.route(path='/echo', methods=['POST'])
    async def _echo(conn, receive, send):
        event = await receive()
        await send(event.get('body', b'') or b'(empty)', 200)

    @a.route(path='/café')
    async def _unicode_path():
        return 'cafe'

    return a


async def _drive(app, request: bytes) -> bytes:
    # The head is handed over via ``request=``; the reader carries only the
    # body, so the keep-alive loop sees EOF and serves exactly one response.
    head, _, body = request.partition(b'\r\n\r\n')
    reader, writer = _FakeReader(body), _FakeWriter()
    actor = HTTP1Actor(reader, writer, app, None, request=head + b'\r\n\r\n')
    await asyncio.wait_for(actor.run(), timeout=5.0)
    return bytes(writer.written)


@pytest.mark.asyncio
@pytest.mark.parametrize('name', sorted(CORPUS))
async def test_both_lanes_produce_identical_bytes(app, name, monkeypatch):
    """Native and BB_FORCE_ASGI_SCOPE=1 must agree byte for byte."""
    request = CORPUS[name].raw

    reset_settings_cache()
    native = await _drive(app, request)

    monkeypatch.setenv('BB_FORCE_ASGI_SCOPE', '1')
    reset_settings_cache()
    try:
        forced = await _drive(app, request)
    finally:
        monkeypatch.delenv('BB_FORCE_ASGI_SCOPE', raising=False)
        reset_settings_cache()

    assert _normalise(forced) == _normalise(native), (
        f'{name}: the compat lane diverged from the native lane.\n'
        f'  native: {native[:200]!r}\n'
        f'  forced: {forced[:200]!r}')


@pytest.mark.asyncio
@pytest.mark.parametrize('name', sorted(CORPUS))
async def test_both_lanes_agree_on_status(app, name, monkeypatch):
    """A narrower assertion that survives intentional body changes.

    ``test_both_lanes_produce_identical_bytes`` is the real invariant, but it
    fails wholesale if a response body is ever deliberately changed.  This one
    keeps the status-code half of the guarantee legible on its own.
    """
    request = CORPUS[name].raw

    def status_of(raw: bytes) -> bytes:
        return raw.split(b'\r\n', 1)[0] if raw else b'<no response>'

    reset_settings_cache()
    native = status_of(await _drive(app, request))

    monkeypatch.setenv('BB_FORCE_ASGI_SCOPE', '1')
    reset_settings_cache()
    try:
        forced = status_of(await _drive(app, request))
    finally:
        monkeypatch.delenv('BB_FORCE_ASGI_SCOPE', raising=False)
        reset_settings_cache()

    assert native == forced, f'{name}: {native!r} (native) != {forced!r} (compat)'
