import pytest
from hpack import Decoder, Encoder

from blackbull.native import NativeResponse
from tests.harness import served
from tests.conformance.http2._harness import h2_connect, h2_section, h2_terminal


@pytest.mark.asyncio
@pytest.mark.parametrize('protocol', ['http1', 'http2'])
@pytest.mark.parametrize('force_asgi', [0, 1], ids=['native', 'compat'])
@pytest.mark.parametrize('method,status,length', [
    ('GET', 200, b'5'), ('HEAD', 200, b'5'), ('HEAD', 200, b'0'),
    ('HEAD', 200, None), ('GET', 204, b'5'), ('GET', 205, b'5'), ('GET', 304, None)])
@pytest.mark.parametrize('form', ['no-body', 'split', 'native-combined', 'empty-body'])
async def test_empty_trailer_response_preserves_head_and_next_request(
        monkeypatch, protocol, force_asgi, method, status, length, form):
    monkeypatch.setenv('BB_FORCE_ASGI_SCOPE', str(force_asgi))
    calls = []
    headers = [(b'x-head', b'present')]
    if length is not None:
        headers.append((b'content-length', length))
    trailers = [(b'x-checksum', b'empty')]

    async def app(conn, receive, send):
        calls.append(conn)
        if len(calls) == 2:
            await send(NativeResponse(header=[], body=b'ok'))
        elif form == 'native-combined':
            await send(NativeResponse(status=status, header=headers, trailers=trailers,
                                      expects_trailers=True))
        else:
            await send({'type': 'http.response.start', 'status': status,
                        'headers': headers, 'trailers': True})
            if form == 'empty-body':
                await send({'type': 'http.response.body', 'body': b''})
            if form == 'split':
                await send({'type': 'http.response.trailers', 'headers': [], 'more_trailers': True})
            await send({'type': 'http.response.trailers', 'headers': trailers})

    async with served(app) as server:
        encoder, decoder = Encoder(), Decoder()
        wire = await (server.wire() if protocol == 'http1' else h2_connect(server))
        for sid, request_method, wanted_status in [(1, method, status), (3, 'GET', 200)]:
            if protocol == 'http1':
                await wire.send(request_method.encode() + b' / HTTP/1.1\r\nHost: x\r\n\r\n')
                response = await wire.response(head=request_method == 'HEAD')
                fields, body, tail = response.headers, response.body, response.trailers
                assert response.status == wanted_status
                if sid == 1 and method == 'GET' and status == 200:
                    assert response.header(b'transfer-encoding') == b'chunked'
                    assert response.header(b'content-length') is None
                if sid == 1 and status in (204, 205, 304):
                    assert response.header(b'transfer-encoding') is None
                    assert response.header(b'content-length') == {
                        204: None, 205: b'0', 304: None}[status]
            else:
                fields = [(b':method', request_method.encode()), (b':path', b'/'),
                          (b':scheme', b'http'), (b':authority', b'x')]
                await wire.send(h2_section(encoder.encode(fields), sid=sid))
                fields, body = await h2_terminal(wire, sid, decoder)
                assert (b':status', str(wanted_status).encode()) in fields
                tail = [(k, v) for k, v in fields if k == b'x-checksum']
            if sid == 1:
                assert (b'x-head', b'present') in fields
                if method == 'HEAD' and length is not None:
                    assert (b'content-length', length) in fields
                assert body == b''
                assert tail == (trailers if method == 'GET' and status == 200 else [])
            else:
                assert body == b'ok' and not tail
    assert len(calls) == 2
