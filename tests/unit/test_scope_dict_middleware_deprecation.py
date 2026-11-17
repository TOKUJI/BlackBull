"""Bundled middleware called with a scope dict warn, and behave as before (BLA-588).

BBD015 covers CORS, Compression, TrustedProxy and StaticFiles reached with a
raw scope dict; BBD014 covers StaticFiles used as a standalone ASGI app.  The
native path, a Connection from a BlackBull app, stays silent.
"""
import asyncio
import warnings

import pytest

from blackbull import BlackBull, BlackBullDeprecationWarning, Response
from blackbull.middleware import Compression, CORS, TrustedProxy
from blackbull.middleware.static import StaticFiles
from blackbull.testing.native import NativeClient


def _scope(**extra) -> dict:
    return {'type': 'http', 'method': 'GET', 'path': '/a.txt', 'raw_path': b'/a.txt',
            'headers': [(b'origin', b'https://example.com'),
                        (b'accept-encoding', b'gzip'),
                        (b'x-forwarded-for', b'203.0.113.9')],
            'client': ('127.0.0.1', 5000), 'scheme': 'http', 'root_path': '',
            'query_string': b'', 'server': ('127.0.0.1', 80), **extra}


def _bbd(caught) -> set[str]:
    return {str(w.message).rsplit('(', 1)[-1].rstrip(').') for w in caught
            if issubclass(w.category, BlackBullDeprecationWarning)}


def _run(middleware, scope, *, call_next=True):
    seen, sent = [], []

    async def downstream(conn, receive, send):
        seen.append(conn)

    async def send(event):
        sent.append(event)

    async def receive():
        return {'type': 'http.request', 'body': b'', 'more_body': False}

    args = (scope, receive, send) + ((downstream,) if call_next else ())

    async def caller():   # the user's line the warning should name
        await middleware(*args)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter('always')
        asyncio.run(caller())
    return caught, seen, sent


@pytest.mark.parametrize('make', [
    pytest.param(lambda d: CORS(), id='cors'),
    pytest.param(lambda d: Compression(), id='compression'),
    pytest.param(lambda d: StaticFiles(directory=str(d)), id='static-as-middleware'),
])
def test_a_pass_through_middleware_warns_and_still_passes_the_dict_on(tmp_path, make):
    scope = _scope()
    caught, seen, sent = _run(make(tmp_path), scope)
    assert _bbd(caught) == {'BBD015'}
    assert seen == [scope] and sent == []


def test_trusted_proxy_warns_and_still_rewrites_the_dict():
    scope = _scope()
    caught, seen, _ = _run(TrustedProxy(), scope)
    assert _bbd(caught) == {'BBD015'}
    assert seen[0]['client'][0] == '203.0.113.9'


def test_standalone_static_files_warns_and_still_answers_404(tmp_path):
    (tmp_path / 'a.txt').write_text('alpha')
    caught, _, sent = _run(StaticFiles(directory=str(tmp_path)), _scope(), call_next=False)
    assert _bbd(caught) == {'BBD014'}
    assert sent and getattr(sent[0], 'status', None) == 404


def test_the_native_path_does_not_warn(tmp_path):
    (tmp_path / 'a.txt').write_text('alpha')
    app = BlackBull()
    for middleware in (TrustedProxy(), CORS(), Compression()):
        app.use(middleware)
    app.static('/static', tmp_path)

    @app.route(path='/')
    async def root(conn, receive, send):
        await send(Response(b'ok'))

    headers = {'origin': 'https://example.com', 'accept-encoding': 'gzip'}
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter('always')
        with NativeClient(app) as client:
            assert client.get('/', headers=headers).status == 200
            assert client.get('/static/a.txt', headers=headers).status == 200
    assert _bbd(caught) == set()


@pytest.mark.parametrize('make,call_next', [
    pytest.param(lambda d: CORS(), True, id='cors'),
    pytest.param(lambda d: Compression(), True, id='compression'),
    pytest.param(lambda d: TrustedProxy(), True, id='trusted-proxy'),
    pytest.param(lambda d: StaticFiles(directory=str(d)), True, id='static-as-middleware'),
    pytest.param(lambda d: StaticFiles(directory=str(d)), False, id='static-standalone'),
])
def test_the_warning_names_the_callers_line(tmp_path, make, call_next):
    """Not BlackBull's decorator or a type-checker's wrapper: Python shows a
    DeprecationWarning by default only when it is attributed to __main__."""
    caught, _, _ = _run(make(tmp_path), _scope(), call_next=call_next)
    assert {w.filename for w in caught
            if issubclass(w.category, BlackBullDeprecationWarning)} == {__file__}
