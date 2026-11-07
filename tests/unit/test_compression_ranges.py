import pytest

from blackbull.connection import Connection
from blackbull.middleware.compression import Compression
from blackbull.native import NativeResponse


@pytest.mark.asyncio
@pytest.mark.parametrize('accept', [b'gzip', b'identity'])
@pytest.mark.parametrize('shape', ['complete', 'split', 'streamed', 'pathsend', 'headers'])
@pytest.mark.parametrize('status,content_range', [
    (206, None),
    (206, b'bytes 0-9/1000'),
    (200, b'bytes 0-9/1000'),
    (416, b'bytes */1000'),
])
async def test_range_response_is_not_transformed(accept, shape, status, content_range):
    body = b'0123456789'
    headers = [(b'content-type', b'text/plain'), (b'content-length', b'10'),
               (b'vary', b'Origin')]
    if content_range is not None:
        headers.append((b'content-range', content_range))
    conn = Connection.from_scope({'type': 'http', 'method': 'GET', 'path': '/',
                                  'headers': [(b'accept-encoding', accept)]})
    events = []

    async def send(event):
        events.extend(event.to_asgi())

    async def call_next(conn, receive, send):
        if shape == 'complete':
            await send(NativeResponse(status=status, header=headers, body=body))
            return
        await send(NativeResponse(status=status, header=headers))
        if shape == 'split':
            await send(NativeResponse(body=body))
        elif shape == 'streamed':
            await send(NativeResponse(body=body[:5], more_body=True))
            await send(NativeResponse(body=body[5:]))
        elif shape == 'pathsend':
            await send(NativeResponse(file_path='/asset.txt'))

    await Compression(min_size=1)(conn, None, send, call_next)
    start = next(e for e in events if e['type'] == 'http.response.start')
    assert start['status'] == status
    assert start['headers'] == headers
    if shape == 'pathsend':
        assert events[-1] == {'type': 'http.response.pathsend', 'path': '/asset.txt'}
    elif shape != 'headers':
        assert b''.join(e.get('body', b'') for e in events) == body
