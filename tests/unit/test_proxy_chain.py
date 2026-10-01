"""Forwarding assertions stop at the first unauthenticated hop."""
import pytest

from blackbull.connection import CONNECTION_STASH_KEY, Connection
from blackbull.headers import Headers
from blackbull.middleware.proxy import TrustedProxy


@pytest.fixture(params=['native', 'scope', 'stashed'])
def shape(request):
    return request.param


async def apply(shape, fields, *, trusted=('127.0.0.1', '10.0.0.0/8'),
                peer='127.0.0.1', type_='http'):
    native = Connection(method='GET', path='/', raw_path=b'/',
                        headers=Headers(fields), client=(peer, 12345) if peer else None,
                        scheme='http', root_path='/original', type=type_)
    conn = native if shape == 'native' else {
        'type': type_, 'client': native.client, 'scheme': 'http',
        'root_path': '/original', 'headers': fields,
    }
    if shape == 'stashed':
        conn[CONNECTION_STASH_KEY] = native
    called = []

    async def next_(value, receive, send):
        called.append(value)

    await TrustedProxy(list(trusted))(conn, None, None, next_)
    assert called == [conn]
    actual = ((native.client, native.scheme, native.root_path) if shape == 'native'
              else (conn['client'], conn['scheme'], conn['root_path']))
    actual = (tuple(actual[0]) if actual[0] else None, *actual[1:])
    if shape == 'stashed':
        assert actual == (native.client, native.scheme, native.root_path)
    return actual


@pytest.mark.asyncio
@pytest.mark.parametrize('type_', ['http', 'websocket'])
@pytest.mark.parametrize('name,values', [
    (b'x-forwarded-for', [b'198.51.100.99, 203.0.113.10']),
    (b'X-Forwarded-For', [b'198.51.100.99', b'203.0.113.10']),
    (b'forwarded', [b'for=198.51.100.99;proto=https, for=203.0.113.10;proto=http']),
    (b'Forwarded', [b'for=198.51.100.99;proto=https', b'for=203.0.113.10']),
])
async def test_spoofed_prefix_cannot_cross_untrusted_hop(shape, type_, name, values):
    assert await apply(shape, [(name, v) for v in values], type_=type_) == (
        ('203.0.113.10', 0), 'http', '/original')


@pytest.mark.asyncio
@pytest.mark.parametrize('value,expected', [
    (b'203.0.113.10, 10.0.0.2, 10.0.0.3', '203.0.113.10'),
    (b'10.0.0.2, 10.0.0.3', '10.0.0.2'),
    (b'2001:db8::1, 10.0.0.2', '2001:db8::1'),
])
async def test_verified_xff_suffix(shape, value, expected):
    assert (await apply(shape, [(b'x-forwarded-for', value)]))[0] == (expected, 0)


@pytest.mark.asyncio
@pytest.mark.parametrize('value', [b'unknown', b'_hidden', b'garbage', b'', b'\xff',
                                  b'203.0.113.1:42', b'[2001:db8::1]'])
async def test_invalid_xff_hop_is_barrier(shape, value):
    assert (await apply(shape, [(b'x-forwarded-for',
                                b'198.51.100.99, ' + value + b', 10.0.0.2')]))[0] == ('10.0.0.2', 0)


@pytest.mark.asyncio
@pytest.mark.parametrize('node,expected', [
    (b'203.0.113.10', '203.0.113.10'),
    (b'"203.0.113.10"', '203.0.113.10'),
    (b'"203.0.113.10:443"', '203.0.113.10'),
    (b'"203.0.113.10:_hidden"', '203.0.113.10'),
    (b'"[2001:0db8::1]"', '2001:db8::1'),
    (b'"[2001:db8::1]:443"', '2001:db8::1'),
    (b'"[2001:db8::1]:_hidden"', '2001:db8::1'),
])
async def test_forwarded_node_and_selected_proto(shape, node, expected):
    value = b'for=198.51.100.99;proto=http, for=' + node + b';proto=https, for=10.0.0.2;proto=http'
    assert await apply(shape, [(b'forwarded', value)]) == ((expected, 0), 'https', '/original')


@pytest.mark.asyncio
@pytest.mark.parametrize('element', [b'for=unknown', b'for=_hidden', b'by=10.0.0.1',
                                    b'proto=https', b'for=garbage', b'for="1.2.3.4:bad"',
                                    b'for="[fe80::1%eth0]"'])
async def test_forwarded_semantic_barrier(shape, element):
    value = b'for=198.51.100.99;proto=https, ' + element + b', for=10.0.0.2;proto=https'
    assert await apply(shape, [(b'forwarded', value)]) == (('10.0.0.2', 0), 'http', '/original')


@pytest.mark.asyncio
@pytest.mark.parametrize('value', [
    b'', b'for="203.0.113.10', b'for="203.0.113.10\\',
    b'for=203.0.113.10;FOR=198.51.100.99',
    b'for=203.0.113.10;proto=http;PROTO=https',
    b'for=203.0.113.10=evil', b'for="203.0.113.10\x00"',
    b'for=\xff', b'for=203.0.113.10;proto="https://evil"',
])
async def test_bad_forwarded_never_falls_back(shape, value):
    actual = await apply(shape, [(b'forwarded', value), (b'x-forwarded-for', b'198.51.100.99'),
                                 (b'x-forwarded-proto', b'https')])
    assert actual[1] == 'http'
    assert actual[0][0] != '198.51.100.99'


@pytest.mark.asyncio
async def test_quoted_extensions_do_not_create_hops(shape):
    value = b'for=203.0.113.10;proto=https;ext="a,b;c=\\"d", for=10.0.0.2'
    assert await apply(shape, [(b'forwarded', value)]) == (('203.0.113.10', 0), 'https', '/original')


@pytest.mark.asyncio
@pytest.mark.parametrize('name,values', [
    (b'x-forwarded-proto', [b'https,http']),
    (b'x-forwarded-proto', [b'https', b'http']),
    (b'x-forwarded-proto', [b'https://evil']),
    (b'x-forwarded-proto', [b'\xff']),
    (b'x-forwarded-prefix', [b'/evil,/app']),
    (b'x-forwarded-prefix', [b'/evil', b'/app']),
    (b'x-forwarded-prefix', [b'//evil']),
    (b'x-forwarded-prefix', [b'/app\x00']),
    (b'x-forwarded-prefix', [b'/app?query']),
    (b'x-forwarded-prefix', ['/app\u0085'.encode()]),
    (b'x-forwarded-prefix', ['/app\u00a0'.encode()]),
])
async def test_ambiguous_standalone_assertions_ignored(shape, name, values):
    assert await apply(shape, [(name, v) for v in values]) == (('127.0.0.1', 12345), 'http', '/original')


@pytest.mark.asyncio
@pytest.mark.parametrize('prefix,expected', [(b'/api/', '/api'), (b'/', '')])
async def test_singleton_assertions(shape, prefix, expected):
    assert await apply(shape, [(b'x-forwarded-proto', b'HTTPS'), (b'x-forwarded-prefix', prefix)]) == (
        ('127.0.0.1', 12345), 'https', expected)


@pytest.mark.asyncio
@pytest.mark.parametrize('peer,trusted,type_', [
    ('203.0.113.10', ('127.0.0.1',), 'http'),
    (None, ('127.0.0.1',), 'http'),
    ('127.0.0.1', (), 'http'),
    ('127.0.0.1', ('127.0.0.1',), 'lifespan'),
])
async def test_no_trust_or_non_http_passes_through(shape, peer, trusted, type_):
    assert await apply(shape, [(b'forwarded', b'for=198.51.100.99;proto=https'),
                               (b'x-forwarded-prefix', b'/evil')],
                       peer=peer, trusted=trusted, type_=type_) == (
        (peer, 12345) if peer else None, 'http', '/original')


@pytest.mark.asyncio
async def test_parameterless_forwarded_element_is_barrier(shape):
    value = b'for=198.51.100.99;proto=https, ;, for=10.0.0.2;proto=https'
    assert await apply(shape, [(b'forwarded', value)]) == (('10.0.0.2', 0), 'http', '/original')
