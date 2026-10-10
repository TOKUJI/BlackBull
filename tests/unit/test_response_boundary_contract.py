"""Every NativeResponse carries one header contract, however it was made.

Names are lowercase tchar and values carry no CTL from the moment a
NativeResponse exists — built directly, set later, appended to, converted
from a ``Response`` or from an ASGI dict, or emitted by any middleware layer.
Middleware and senders read that contract and do not normalise again.
"""
from __future__ import annotations

import gzip

import pytest

from blackbull import BlackBull, Response
from blackbull.native import NativeResponse


class TestEveryWayInKeepsTheContract:
    def test_the_constructor_lowercases_names(self):
        resp = NativeResponse(status=200, header=[(b'Content-Type', b'text/plain')])

        assert list(resp.header) == [(b'content-type', b'text/plain')]

    @pytest.mark.parametrize('field', [(b'x-a', b'a\r\nb'), (b'x a', b'1'),
                                       (b'x-a', b'\x00')])
    def test_the_constructor_refuses_a_field_that_breaks_it(self, field):
        with pytest.raises(ValueError):
            NativeResponse(status=200, header=[field])

    def test_the_setter_lowercases_names(self):
        resp = NativeResponse(status=200, header=[])
        resp.header = [(b'X-A', b'1')]

        assert list(resp.header) == [(b'x-a', b'1')]

    def test_the_trailers_setter_lowercases_and_validates(self):
        resp = NativeResponse(status=200, header=[])
        resp.trailers = [(b'X-T', b'1')]

        assert list(resp.trailers) == [(b'x-t', b'1')]
        with pytest.raises(ValueError):
            resp.trailers = [(b'x-t', b'a\nb')]

    def test_appending_a_trailer_lowercases_and_validates(self):
        resp = NativeResponse(trailers=[])
        resp.trailers.append(b'X-T', b'1')

        assert list(resp.trailers) == [(b'x-t', b'1')]
        with pytest.raises(ValueError):
            resp.trailers.append(b'x-u', b'v\r\nInjected: yes')

    def test_append_lowercases_and_validates(self):
        resp = NativeResponse(status=200, header=[])
        resp.header.append(b'X-B', b'2')

        assert list(resp.header) == [(b'x-b', b'2')]
        with pytest.raises(ValueError):
            resp.header.append(b'x-c', b'a\nb')

    def test_a_response_converts_with_lowercase_names(self):
        native = Response(b'ok', headers=[(b'X-Trace', b'1')]).to_native()

        assert (b'x-trace', b'1') in list(native.header)


def _scope(headers):
    return {'type': 'http', 'http_version': '1.1', 'method': 'GET',
            'scheme': 'http', 'path': '/', 'raw_path': b'/', 'query_string': b'',
            'headers': [(b'host', b'a.example'), *headers]}


@pytest.mark.asyncio
async def test_a_dict_from_an_inner_middleware_reaches_outer_middleware_as_native():
    """Compression stamps and encodes a response an inner layer sent as dicts."""
    from blackbull.middleware.compression import Compression

    app = BlackBull()
    app.use(Compression(min_size=1))

    async def raw_responder(conn, receive, send, call_next):
        await send({'type': 'http.response.start', 'status': 200,
                    'headers': [(b'Content-Type', b'text/plain')]})
        await send({'type': 'http.response.body', 'body': b'x' * 64})

    app.use(raw_responder)

    @app.route(path='/')
    async def root():  # pragma: no cover - the inner layer answers first
        return 'unreached'

    sent = []

    async def receive():
        return {'type': 'http.request', 'body': b'', 'more_body': False}

    async def send(event):
        sent.append(event)

    await app(_scope([(b'accept-encoding', b'gzip')]), receive, send)

    start = next(e for e in sent if e['type'] == 'http.response.start')
    headers = dict(start['headers'])
    body = b''.join(e.get('body', b'') for e in sent
                    if e['type'] == 'http.response.body')
    assert headers[b'content-encoding'] == b'gzip'
    assert gzip.decompress(body) == b'x' * 64
