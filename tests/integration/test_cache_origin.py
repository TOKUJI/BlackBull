"""Cache response bodies and headers remain local to their effective origin."""
import pytest

from blackbull import BlackBull
from blackbull.middleware.cache import Cache
from blackbull.middleware.proxy import TrustedProxy
from blackbull.native import NativeResponse
from blackbull.testing import TestClient
from blackbull.testing import native

pytestmark = pytest.mark.integration


def make_app(*, max_entries=1024, trusted=False, vary=False):
    app = BlackBull()
    if trusted:
        app.use(TrustedProxy(['127.0.0.1']))
    app.use(Cache(max_age=60, max_entries=max_entries))
    calls = []

    @app.route(path='/item', methods=['GET', 'HEAD'])
    async def item(conn, receive, send):
        authority = conn.headers.get(b'host', b'unknown').decode('ascii')
        origin = f'{conn.scheme}://{authority}'
        language = conn.headers.get(b'accept-language', b'none').decode('ascii')
        calls.append((conn.method, origin, language))
        headers = [(b'x-origin', origin.encode('ascii')),
                   (b'x-call', str(len(calls)).encode('ascii'))]
        if vary:
            headers.append((b'vary', b'Accept-Language'))
        await send(NativeResponse(status=200, header=headers,
                                  body=f'{origin}/{language}'.encode('ascii')))

    return app, calls


def test_distinct_origins_never_replay_each_others_body_or_headers():
    app, calls = make_app()
    origins = ['http://a.example', 'http://b.example', 'https://a.example',
               'http://a.example:8080']
    with TestClient(app) as client:
        for origin in origins:
            first = client.get(origin + '/item')
            second = client.get(origin + '/item')
            assert first.text == second.text == origin + '/none'
            assert first.headers['x-origin'] == second.headers['x-origin'] == origin
            assert first.headers['x-call'] == second.headers['x-call']
    assert len(calls) == len(origins)


def test_foreign_origin_etag_does_not_produce_a_304():
    app, calls = make_app()
    with TestClient(app) as client:
        first = client.get('http://a.example/item')
        foreign = client.get('http://b.example/item', headers={'If-None-Match': first.headers['etag']})
        assert foreign.status_code == 200
        assert foreign.headers['x-origin'] == 'http://b.example'
        matching = client.get('http://b.example/item', headers={'If-None-Match': foreign.headers['etag']})
        assert matching.status_code == 304
    assert len(calls) == 2


def test_vary_variants_remain_separate_inside_each_origin():
    app, calls = make_app(vary=True)
    with TestClient(app) as client:
        for _ in range(2):
            for origin in ['http://a.example', 'http://b.example']:
                for language in ['en', 'ja']:
                    response = client.get(origin + '/item', headers={'Accept-Language': language})
                    assert response.text == f'{origin}/{language}'
                    assert response.headers['x-origin'] == origin
    assert len(calls) == 4


def test_origin_buckets_share_one_lru_bound():
    app, calls = make_app(max_entries=2)
    with TestClient(app) as client:
        a = client.get('http://a.example/item')
        b = client.get('http://b.example/item')
        assert client.get('http://a.example/item').headers['x-call'] == a.headers['x-call']
        client.get('http://c.example/item')
        refreshed_b = client.get('http://b.example/item')
        assert refreshed_b.headers['x-call'] != b.headers['x-call']
    assert [origin for _, origin, _ in calls] == [
        'http://a.example', 'http://b.example', 'http://c.example', 'http://b.example']


def test_get_and_head_remain_separate_per_origin():
    app, calls = make_app()
    with TestClient(app) as client:
        for host in ['a.example', 'b.example']:
            for method in ['GET', 'HEAD', 'GET', 'HEAD']:
                response = client.request(method, f'http://{host}/item')
                assert response.headers['x-origin'] == f'http://{host}'
    assert [(method, origin) for method, origin, _ in calls] == [
        ('GET', 'http://a.example'), ('HEAD', 'http://a.example'),
        ('GET', 'http://b.example'), ('HEAD', 'http://b.example')]


def test_trusted_proxy_scheme_is_resolved_before_cache_lookup():
    app, calls = make_app(trusted=True)
    with TestClient(app) as client:
        for scheme in ['http', 'https', 'http', 'https']:
            response = client.get('http://a.example/item',
                                  headers={'X-Forwarded-Proto': scheme})
            assert response.headers['x-origin'] == f'{scheme}://a.example'
            assert response.text == f'{scheme}://a.example/none'
    assert len(calls) == 2


def test_forwarded_host_is_not_trusted_by_cache_itself():
    app, calls = make_app()
    with TestClient(app) as client:
        first = client.get('http://a.example/item')
        second = client.get('http://a.example/item', headers={
            'Forwarded': 'host=b.example;proto=https',
            'X-Forwarded-Host': 'b.example',
            'X-Forwarded-Proto': 'https'})
    assert first.text == second.text == 'http://a.example/none'
    assert len(calls) == 1


@pytest.mark.parametrize('first_host,second_host,scheme', [
    ('a.example', 'A.EXAMPLE:080', 'http'),
    ('a.example', 'a.example:443', 'https'),
    ('[::1]', '[::1]:80', 'http'),
])
def test_equivalent_origin_spelling_retains_cache_hits(first_host, second_host, scheme):
    app, calls = make_app()
    with TestClient(app) as client:
        first = client.get(f'{scheme}://testserver/item', headers={'Host': first_host})
        second = client.get(f'{scheme}://testserver/item', headers={'Host': second_host})
    assert first.text == second.text
    assert first.headers['x-call'] == second.headers['x-call']
    assert len(calls) == 1


def test_ip_literal_and_registered_name_are_different_origins():
    app, calls = make_app()
    with TestClient(app) as client:
        literal = client.get('/item', headers={'Host': '[v1.example]'})
        name = client.get('/item', headers={'Host': 'v1.example'})
    assert literal.headers['x-origin'] == 'http://[v1.example]'
    assert name.headers['x-origin'] == 'http://v1.example'
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_native_http1_host_isolates_responses_on_a_reused_connection():
    app, calls = make_app()
    async with native.NativeTestServer(app) as server:
        for host in ['a.example', 'b.example', 'a.example', 'b.example']:
            response = await server.client.get('/item', headers={'Host': host})
            assert response.text == f'http://{host}/none'
            assert response.headers['x-origin'] == f'http://{host}'
        assert server.connections_served == 1
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_native_http2_authority_mapping_is_used_instead_of_literal_host():
    from hpack import Encoder
    from blackbull.protocol.frame import FrameFactory
    from blackbull.protocol.frame_types import FrameTypes, HeaderFrameFlags
    from blackbull.server.parser import parse_headers

    app, calls = make_app()
    for authority in ['a.example', 'b.example', 'a.example', 'b.example']:
        block = Encoder().encode([
            (b':method', b'GET'), (b':scheme', b'https'),
            (b':path', b'/item'), (b':authority', authority.encode()),
            (b'host', b'ignored.example')])
        flags = HeaderFrameFlags.END_HEADERS | HeaderFrameFlags.END_STREAM
        wire = (len(block).to_bytes(3, 'big') + FrameTypes.HEADERS
                + bytes([flags]) + (1).to_bytes(4, 'big') + block)
        conn = parse_headers(FrameFactory().load(wire))
        assert conn is not None
        response = await native.request(app, conn)
        assert response.body == f'https://{authority}/none'.encode()
        assert response.headers.get(b'x-origin') == f'https://{authority}'.encode()
    assert len(calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('server', [('a.example', 80), ('a.example', 8080), ('::1', None)])
async def test_hostless_request_can_use_its_server_origin(server):
    from blackbull.connection import Connection
    from blackbull.headers import Headers

    app, calls = make_app()
    conn = Connection(method='GET', path='/item', raw_path=b'/item',
                      headers=Headers([]), http_version='1.0', server=server)
    first = await native.request(app, conn)
    second = await native.request(app, conn)
    assert first.body == second.body
    assert first.headers.get(b'x-call') == second.headers.get(b'x-call')
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_distinct_server_origins_without_host_do_not_share_cache():
    from blackbull.connection import Connection
    from blackbull.headers import Headers

    app, calls = make_app()
    for server in [('a.example', 80), ('b.example', 80), ('a.example', 8080)]:
        conn = Connection(method='GET', path='/item', raw_path=b'/item',
                          headers=Headers([]), http_version='1.0', server=server)
        await native.request(app, conn)
    assert len(calls) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize('headers', [
    [], [(b'host', b'')], [(b'host', b'a.example'), (b'host', b'b.example')],
    [(b'host', b'a.example:invalid')], [(b'host', b'[::1]suffix')],
])
async def test_unresolved_or_ambiguous_origin_bypasses_cache_without_rejecting(headers):
    from blackbull.connection import Connection
    from blackbull.headers import Headers

    app, calls = make_app()
    conn = Connection(method='GET', path='/item', raw_path=b'/item', headers=Headers(headers))
    first = await native.request(app, conn)
    second = await native.request(app, conn)
    assert first.status == second.status == 200
    assert first.body == second.body
    assert first.headers.get(b'x-call') != second.headers.get(b'x-call')
    assert len(calls) == 2
