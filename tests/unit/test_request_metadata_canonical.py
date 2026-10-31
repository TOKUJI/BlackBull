"""Request metadata a request nearly always carries is one shared ``str``.

A common method, HTTP/1.x version or HTTP/2 scheme becomes the same object on
every request and on both protocols, instead of a fresh decode; anything else
keeps today's value and handling.
"""
import asyncio

import pytest
from hpack import Decoder, Encoder

from blackbull.protocol.field_grammar import METHOD_TEXT, SCHEME_TEXT, VERSION_TEXT
from blackbull.protocol.frame import FrameFactory
from blackbull.protocol.frame_types import FrameTypes
from blackbull.server.parser import parse_headers
from tests.conformance.http2._harness import h2_connect, h2_section, h2_terminal
from tests.harness import served

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _native_lane(monkeypatch):
    # The shared objects are the native Connection's fields; the ASGI lane
    # hands the application a scope dict instead.
    monkeypatch.setenv('BB_FORCE_ASGI_SCOPE', '0')

COMMON = ['GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS', 'TRACE',
          'CONNECT', 'QUERY']


def _recording():
    seen = []

    async def app(conn, receive, send):
        if isinstance(conn, dict) and conn.get('type') == 'lifespan':
            return
        seen.append((conn.method, conn.http_version, conn.scheme))
        await send({'type': 'http.response.start', 'status': 200, 'headers': []})
        await send({'type': 'http.response.body', 'body': b''})
    return app, seen


async def _h1(methods, version=b'HTTP/1.1'):
    app, seen = _recording()
    async with served(app) as server:
        for method in methods:
            wire = await server.wire()
            await wire.send(method.encode() + b' / ' + version + b'\r\nHost: x\r\n\r\n')
            await wire.response(head=method == 'HEAD')
    return seen


async def _h2(methods, scheme=b'http'):
    app, seen = _recording()
    async with served(app) as server:
        for method in methods:
            wire = await h2_connect(server)
            await wire.send(h2_section(Encoder().encode(
                [(b':method', method.encode()), (b':path', b'/'),
                 (b':scheme', scheme), (b':authority', b'x')])))
            await h2_terminal(wire, 1, Decoder())
    return seen


def test_every_common_value_has_one_shared_str():
    assert sorted(METHOD_TEXT.values()) == sorted(COMMON)
    for table in (METHOD_TEXT, VERSION_TEXT, SCHEME_TEXT):
        assert all(type(text) is str for text in table.values())
    assert VERSION_TEXT == {b'HTTP/1.1': '1.1', b'HTTP/1.0': '1.0'}
    assert SCHEME_TEXT == {b'http': 'http', b'https': 'https'}


@pytest.mark.parametrize('methods', [['GET', 'POST', 'QUERY', 'PUT']])
async def test_both_protocols_share_the_method_version_and_scheme(methods):
    h1 = await _h1(methods)
    h2 = await _h2(methods)
    for (m1, v1, s1), (m2, v2, s2), method in zip(h1, h2, methods):
        assert m1 is m2 is METHOD_TEXT[method.encode()]
        assert v1 is VERSION_TEXT[b'HTTP/1.1']
        assert s1 == 'http'
        assert s2 is SCHEME_TEXT[b'http']
        assert v2 == '2'


async def test_unknown_values_keep_their_spelling():
    [(h1_method, _, _)] = await _h1(['PROPFIND'])
    [(h2_method, _, _)] = await _h2(['PROPFIND'])
    assert h1_method == h2_method == 'PROPFIND'
    [(_, version, _)] = await _h1(['GET'], version=b'HTTP/1.0')
    assert version is VERSION_TEXT[b'HTTP/1.0']
    # An unknown but well-formed scheme is graded and lowercased as before.
    [(_, _, scheme)] = await _h2(['GET'], scheme=b'Foo')
    assert scheme == 'foo'


@pytest.mark.parametrize('authority, host', [
    (b'example.test', b'example.test'),
    (b'example.test:8443', b'example.test:8443'),
    (None, None),
    (b'', 'refused'),
    (b'bad host', 'refused'),
])
async def test_authority_becomes_the_host_header(authority, host):
    hosts = []

    async def app(conn, receive, send):
        if isinstance(conn, dict) and conn.get('type') == 'lifespan':
            return
        hosts.append(conn.headers.get(b'host'))
        await send({'type': 'http.response.start', 'status': 200, 'headers': []})
        await send({'type': 'http.response.body', 'body': b''})

    fields = [(b':method', b'GET'), (b':path', b'/'), (b':scheme', b'http')]
    if authority is not None:
        fields.append((b':authority', authority))
    else:
        fields.append((b'host', b'fallback.test'))
    async with served(app) as server:
        wire = await h2_connect(server)
        await wire.send(h2_section(Encoder().encode(fields)))
        outcome = await asyncio.wait_for(
            h2_terminal(wire, 1, Decoder()), 5)
    if host == 'refused':
        assert hosts == [] and outcome[0] == 3        # RST_STREAM
    else:
        assert hosts == [host if host is not None else b'fallback.test']


def test_the_host_header_is_the_authority_as_received():
    """No decode and re-encode: ``host`` is the very bytes ``:authority`` arrived as."""
    block = Encoder().encode([(b':method', b'GET'), (b':path', b'/'),
                              (b':scheme', b'https'), (b':authority', b'example.test')])
    frame = FrameFactory().load(len(block).to_bytes(3, 'big') + FrameTypes.HEADERS
                                + b'\x05' + (1).to_bytes(4, 'big') + block)
    conn = parse_headers(frame)
    assert conn.headers.get(b'host') is frame.raw_authority == b'example.test'
    assert conn.scheme is SCHEME_TEXT[b'https']
    assert conn.method is METHOD_TEXT[b'GET']
