"""RFC 9112 request heads: what is refused, and what the application sees.

Every row crosses the wire into the production server.
"""
import asyncio
import logging

import pytest

from blackbull.connection import Connection
from blackbull.env import reset_settings_cache

from tests.harness import served

pytestmark = pytest.mark.asyncio

WS_KEY = b'dGhlIHNhbXBsZSBub25jZQ=='


def _head(target=b'/', *fields, method=b'GET', version=b'HTTP/1.1', host=b'x'):
    lines = [method + b' ' + target + b' ' + version]
    if host is not None:
        lines.append(b'Host: ' + host)
    return b'\r\n'.join([*lines, *fields]) + b'\r\n\r\n'


REFUSED = {
    'missing-host-on-1.1': (_head(host=None), 400),
    'version-9.9': (_head(version=b'HTTP/9.9'), 505),
    'version-0.9': (_head(version=b'HTTP/0.9'), 505),
    'version-grammar': (_head(version=b'HTTP/1.1.1'), 400),
    'asterisk-with-get': (_head(b'*'), 400),
    'connect-tunnel': (_head(b'example.com:80', method=b'CONNECT'), 501),
    'userinfo-in-host': (_head(host=b'user@example.com'), 400),
    'non-ascii-target': (_head(b'/\xc3\xa9'), 400),
    'te-chunked-not-final': (_head(b'/', b'Transfer-Encoding: chunked, gzip', method=b'POST'), 400),
    'te-chunked-twice': (_head(b'/', b'Transfer-Encoding: chunked, chunked', method=b'POST'), 400),
    'te-unknown-coding': (_head(b'/', b'Transfer-Encoding: gzip', method=b'POST'), 501),
    'duplicate-content-type': (_head(b'/', b'Content-Type: text/plain', b'Content-Type: text/html'), 400),
    'underscore-content-length': (_head(b'/', b'Content-Length: 1', b'Content_Length: 99', method=b'POST'), 400),
    'underscore-transfer-encoding': (_head(b'/', b'Content-Length: 1', b'Transfer_Encoding: chunked', method=b'POST'), 400),
    'ws-without-key': (_head(b'/ws', b'Upgrade: websocket', b'Connection: Upgrade',
                             b'Sec-WebSocket-Version: 13'), 400),
    'ws-malformed-key': (_head(b'/ws', b'Upgrade: websocket', b'Connection: Upgrade',
                               b'Sec-WebSocket-Key: not-16-bytes-base64!!',
                               b'Sec-WebSocket-Version: 13'), 400),
    **{f'scheme-{t.decode()}': (_head(t + b'://example.com/echo'), 400)
       for t in (b'1http', b'ht,tp', b'ht_tp', b'-http', b'')},
    **{f'content-length-{name}': (_head(b'/', b'Content-Length:' + v, method=b'POST'), 400)
       for name, v in {'leading-zeros': b' 005', 'double-zero': b' 00', 'octal-looking': b' 010',
                       'trailing-space': b'5 ', 'two-leading-spaces': b'  5', 'tab': b'\t5',
                       'spaces-both-sides': b' 5 '}.items()},
}


@pytest.mark.parametrize('raw,status', REFUSED.values(), ids=REFUSED.keys())
async def test_a_refused_head_is_answered_and_closed(raw, status):
    called = []

    async def app(conn, receive, send):
        called.append(conn)

    async with served(app) as s:
        w = await s.wire()
        await w.send(raw)
        resp = await w.response()
        assert await w.closed()
    assert (resp.status, called) == (status, [])


def _seen(conn) -> Connection:
    return conn if isinstance(conn, Connection) else Connection.from_scope(conn)


async def _observe(raw: bytes, *, tls=False) -> tuple[Connection, int]:
    seen = []

    async def app(conn, receive, send):
        seen.append(_seen(conn))
        while (await receive()).get('more_body'):
            pass
        await send({'type': 'http.response.start', 'status': 200, 'headers': []})
        await send({'type': 'http.response.body', 'body': b''})

    async with served(app, tls=tls) as s:
        w = await s.wire()
        await w.send(raw)
        assert (await w.response()).status == 200
        return seen[0], s.server.port


ACCEPTED = {
    'absolute-form': (_head(b'http://example.com/echo?x=1', host=b'example.com'),
                      {'path': '/echo', 'query_string': b'x=1'}),
    'absolute-form-authority-wins': (_head(b'http://real.example/', host=b'spoofed.example'),
                                     {'host': b'real.example'}),
    'absolute-form-bare-authority': (_head(b'http://example.com'), {'path': '/'}),
    'target-scheme-is-not-the-connection-scheme': (_head(b'https://real.example/'),
                                                   {'scheme': 'http', 'host': b'real.example'}),
    **{f'scheme-{s.decode()}': (_head(s + b'://example.com/echo'),
                                {'path': '/echo', 'host': b'example.com'})
       for s in (b'http', b'HTTPS', b'ht+tp', b'ht-tp', b'ht.tp', b'h1')},
    'http-1.0-without-host': (_head(version=b'HTTP/1.0', host=None), {'http_version': '1.0'}),
    'http-1.2-is-1.x': (_head(version=b'HTTP/1.2'), {'http_version': '1.2'}),
    'content-length-no-space': (_head(b'/', b'Content-Length:5', method=b'POST') + b'hello',
                                {'content-length': b'5'}),
    **{f'content-length-{v.decode()}': (_head(b'/', b'Content-Length: ' + v, method=b'POST')
                                        + b'x' * int(v), {'content-length': v})
       for v in (b'5', b'0', b'123')},
    'plain-chunked': (_head(b'/', b'Transfer-Encoding: chunked', method=b'POST') + b'0\r\n\r\n',
                      {'type': 'http'}),
    'one-content-type': (_head(b'/', b'Content-Type: text/plain'), {'content-type': b'text/plain'}),
    'underscore-name-elsewhere': (_head(b'/', b'x_request_id: abc'), {'x_request_id': b'abc'}),
    'upgrade-h2c-is-ignored': (_head(b'/', b'Connection: Upgrade', b'Upgrade: h2c'), {'type': 'http'}),
    'forwarded-prefix-is-not-trusted': (_head(b'/', b'X-Forwarded-Prefix: /evil'), {'root_path': ''}),
    'connection-header-is-not-the-scheme': (_head(b'/', b'Connection: keep-alive'), {'scheme': 'http'}),
    'host-without-port': (_head(host=b'localhost'), {'server': ('localhost', 80)}),
    'host-with-port': (_head(host=b'example.com:8080'), {'server': ('example.com', 8080)}),
}


@pytest.mark.parametrize('raw,expected', ACCEPTED.values(), ids=ACCEPTED.keys())
async def test_an_accepted_head_reaches_the_app_as(raw, expected):
    conn, _ = await _observe(raw)
    for key, value in expected.items():
        if hasattr(conn, key):
            got = getattr(conn, key)
            assert (tuple(got) if isinstance(got, list) else got) == value, key
        else:
            assert conn.headers.get(key.encode()) == value, key


async def test_hosts_absent_the_server_is_the_bound_socket():
    conn, port = await _observe(_head(version=b'HTTP/1.0', host=None))
    assert conn.server[1] == port and conn.client[0] == '127.0.0.1'


async def test_header_order_and_values_survive():
    fields = [b'X-H-%d: v%d' % (i, i) for i in range(50)]
    conn, _ = await _observe(_head(b'/', *fields))
    assert [(n, v) for n, v in conn.headers if n.startswith(b'x-h-')] == \
        [(b'x-h-%d' % i, b'v%d' % i) for i in range(50)]


@pytest.mark.parametrize('tls', [False, True])
async def test_the_connection_scheme_and_sendfile_offer_follow_tls(tls):
    conn, _ = await _observe(_head(), tls=tls)
    assert conn.scheme == ('https' if tls else 'http')
    assert ('http.response.pathsend' in conn.extensions) is not tls


async def test_options_asterisk_is_answered_by_the_server():
    called = []

    async def app(conn, receive, send):
        called.append(conn)

    async with served(app) as s:
        w = await s.wire()
        await w.send(_head(b'*', method=b'OPTIONS'))
        resp = await w.response()
    assert (resp.status, called) == (204, [])
    assert resp.header(b'allow')


# -- limits ----------------------------------------------------------------

@pytest.fixture
def settings(monkeypatch):
    def apply(**env):
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        reset_settings_cache()
    yield apply
    reset_settings_cache()


@pytest.fixture
def caps(caplog):
    """Each cap hit as (cap, protocol), after checking it reports what was refused."""
    caplog.set_level(logging.WARNING, logger='blackbull.caps')

    def hits():
        records = [r for r in caplog.records if r.name == 'blackbull.caps']
        for r in records:
            assert r.requested is not None and r.limit is not None
            assert r.getMessage().startswith('cap hit: ')
        return [(r.cap, r.protocol) for r in records]
    return hits


async def _noop(conn, receive, send):
    await send({'type': 'http.response.start', 'status': 200, 'headers': []})
    await send({'type': 'http.response.body', 'body': b''})


@pytest.mark.parametrize('env,raw,status,cap', [
    pytest.param({}, _head(b'/', b'X-Big: ' + b'a' * 9000), 431, 'header_max_line', id='line'),
    pytest.param({'BB_HEADER_MAX_TOTAL': '256'},
                 _head(b'/', *[b'X-%d: %s' % (i, b'a' * 40) for i in range(8)]), 431,
                 'header_max_total', id='total'),
    pytest.param({}, _head(b'/', b'X-Ok: ' + b'a' * 100), 200, None, id='under-the-caps'),
])
async def test_head_size_limits(settings, caps, env, raw, status, cap):
    settings(**env)
    async with served(_noop) as s:
        w = await s.wire()
        await w.send(raw)
        assert (await w.response()).status == status
    assert caps() == ([(cap, 'http1')] if cap else [])


async def test_a_head_that_never_completes_times_out(settings, caps):
    settings(BB_HEADER_TIMEOUT='0.05')
    async with served(_noop) as s:
        w = await s.wire()
        await w.send(b'GET / HTTP/1.1\r\nHost: x\r\n')
        assert (await w.response()).status == 408
        assert await w.closed()
    assert caps() == [('header_timeout', 'http1')]


async def test_http_1_0_keep_alive_is_honoured():
    async with served(_noop) as s:
        w = await s.wire()
        for _ in range(2):
            await w.send(_head(b'/', b'Connection: keep-alive', version=b'HTTP/1.0', host=None))
            assert (await w.response()).status == 200


async def test_a_head_cut_short_by_eof_is_a_bad_request():
    async with served(_noop) as s:
        w = await s.wire()
        await w.send(b'GET / HTTP/1.1\r\nHost: localh')
        w.half_close()
        resp = await w.response()
        assert await w.closed()
    assert (resp.status, resp.header(b'connection')) == (400, b'close')


async def test_a_head_split_across_segments_is_reassembled():
    raw = _head(b'/', b'X-Pad: ' + b'a' * 3000)
    async with served(_noop) as s:
        w = await s.wire()
        for i in range(0, len(raw), 137):
            await w.send(raw[i:i + 137])
            await asyncio.sleep(0)
        assert (await w.response()).status == 200
