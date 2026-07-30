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
import re

import pytest

from blackbull import BlackBull
from blackbull.env import reset_settings_cache
from blackbull.server.http1_actor import HTTP1Actor

from .test_http1_dispatch import _FakeReader, _FakeWriter

# The Date header is whole-second wall clock, so it may legitimately differ
# between two runs of the same request.  Nothing else may.
_DATE_RE = re.compile(rb'^date:.*$', re.IGNORECASE | re.MULTILINE)


def _normalise(raw: bytes) -> bytes:
    return _DATE_RE.sub(b'date: <normalised>', raw)


def _req(line: bytes, *headers: bytes, body: bytes = b'') -> bytes:
    parts = [line, b'Host: localhost', *headers]
    if body:
        parts.append(b'Content-Length: %d' % len(body))
    return b'\r\n'.join(parts) + b'\r\n\r\n' + body


CORPUS = {
    # --- ordinary routing ------------------------------------------------
    'get': _req(b'GET / HTTP/1.1'),
    'get-query': _req(b'GET /?a=1&b=2 HTTP/1.1'),
    'get-encoded-path': _req(b'GET /caf%C3%A9 HTTP/1.1'),
    'get-1.0': _req(b'GET / HTTP/1.0'),
    'post-body': _req(b'POST /echo HTTP/1.1', body=b'hello'),
    'post-empty-body': _req(b'POST /echo HTTP/1.1', body=b''),
    # --- the method rewrite that broke ------------------------------------
    'head': _req(b'HEAD / HTTP/1.1'),
    'head-with-headers': _req(b'HEAD / HTTP/1.1', b'Accept: */*',
                              b'User-Agent: probe'),
    # --- server-level answers, not routed ---------------------------------
    'options-asterisk': _req(b'OPTIONS * HTTP/1.1'),
    'absolute-form': _req(b'GET http://real.example/ HTTP/1.1'),
    # --- misses and rejections --------------------------------------------
    'not-found': _req(b'GET /nope HTTP/1.1'),
    'method-not-allowed': _req(b'DELETE / HTTP/1.1'),
    'bad-header-name': _req(b'GET / HTTP/1.1', b'Bad Name: v'),
    'obs-fold': _req(b'GET / HTTP/1.1', b' folded: v'),
    'bad-content-length': _req(b'POST /echo HTTP/1.1', b'Content-Length: 007'),
    'unsupported-version': _req(b'GET / HTTP/9.9'),
    # --- header shapes ----------------------------------------------------
    'many-headers': _req(
        b'GET / HTTP/1.1',
        b'User-Agent: Mozilla/5.0 (X11; Linux x86_64)',
        b'Accept: text/html,application/xhtml+xml',
        b'Accept-Encoding: gzip, deflate, br',
        b'Accept-Language: en-US,en;q=0.9',
        b'Cookie: session=8f14e45fceea167a5a36dedd4bea2543',
        b'Referer: http://localhost/index.html'),
    'repeated-header': _req(b'GET / HTTP/1.1', b'Accept: a', b'Accept: b'),
    'ows-padded-value': _req(b'GET / HTTP/1.1', b'Accept:    */*   '),
}


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
    request = CORPUS[name]

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
    request = CORPUS[name]

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
