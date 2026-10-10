import pytest
from unittest.mock import AsyncMock

from blackbull import BlackBull
from blackbull.connection import Connection
from blackbull.headers import Headers
from blackbull.middleware.utils import as_middleware
from blackbull.native import NativeResponse
from blackbull.protocol.frame import FrameFactory
from blackbull.response import wrap_native_send
from blackbull.server.sender import AbstractWriter, HTTP1Sender, HTTP2Sender


class Writer(AbstractWriter):
    def __init__(self):
        self.data = bytearray()

    async def write(self, data):
        self.data.extend(data)


def push_event(native):
    if native:
        return NativeResponse(push='/style.css', header=[(b'accept', b'text/css')])
    return {'type': 'http.response.push', 'path': '/style.css',
            'headers': [(b'accept', b'text/css')]}


@pytest.mark.asyncio
@pytest.mark.parametrize('native', [False, True])
@pytest.mark.parametrize('protocol', ['h1', 'h2'])
async def test_unsupported_push_does_not_change_response(native, protocol, caplog):
    writer = Writer()
    sender = (HTTP1Sender(writer) if protocol == 'h1'
              else HTTP2Sender(writer, FrameFactory(), 1))
    await sender(push_event(native))
    assert not writer.data
    assert 'push' in caplog.text.lower()
    await sender(NativeResponse(header=[], body=b'ok'))
    assert writer.data.endswith(b'ok')


@pytest.mark.asyncio
@pytest.mark.parametrize('native', [False, True])
@pytest.mark.parametrize('phase', ['streaming', 'trailers', 'complete'])
async def test_push_obeys_stream_completion(native, phase):
    writer = Writer()
    callback = AsyncMock()
    sender = HTTP2Sender(writer, FrameFactory(), 1, push_callback=callback)
    await sender(NativeResponse(header=[], body=b'ok',
                                more_body=phase == 'streaming',
                                expects_trailers=phase == 'trailers'))
    before = bytes(writer.data)
    await sender(push_event(native))
    if phase == 'complete':
        callback.assert_not_awaited()
    else:
        callback.assert_awaited_once()
    assert bytes(writer.data) == before


@pytest.mark.asyncio
async def test_push_survives_scope_middleware_and_external_host():
    seen = []

    @as_middleware
    async def middleware(scope, receive, send, call_next):
        async def capture(event):
            if event['type'] == 'http.response.push':
                event['path'] = '/theme.css'
                event['headers'].append((b'x-middleware', b'1'))
            seen.append(event)
            await send(event)
        await call_next(scope, receive, capture)

    app = BlackBull()
    app.use(middleware)

    @app.route(path='/')
    async def handler(conn, receive, send):
        await send(NativeResponse(push='/style.css', header=[(b'accept', b'text/css')]))
        await send(NativeResponse(header=[], body=b'ok'))

    conn = Connection(method='GET', path='/', raw_path=b'/',
                      headers=Headers([]), type='http')
    output = []

    async def send(event):
        output.append(event)

    await app(conn.to_asgi_scope(), AsyncMock(), send)
    assert seen[0] == output[0] == {
        'type': 'http.response.push', 'path': '/theme.css',
        'headers': [(b'accept', b'text/css'), (b'x-middleware', b'1')]}
    assert [event['type'] for event in output] == [
        'http.response.push', 'http.response.start', 'http.response.body']
    assert output[-1]['body'] == b'ok'


@pytest.mark.asyncio
async def test_asgi_push_becomes_native_and_roundtrips_without_aliasing():
    output = []

    async def send(event):
        output.append(event)

    event = push_event(False)
    event['headers'].append((b'accept', b'application/css'))
    await wrap_native_send(send)(event)
    message = output[0]
    assert isinstance(message, NativeResponse)
    assert message.push == '/style.css'
    assert message.to_asgi() == [event]
    message.header.append((b'x-native', b'1'))
    assert event['headers'] == [(b'accept', b'text/css'),
                                (b'accept', b'application/css')]
    expanded = message.to_asgi()[0]
    expanded['headers'].append((b'x-asgi', b'2'))
    assert (b'x-asgi', b'2') not in list(message.header)


@pytest.mark.asyncio
@pytest.mark.parametrize('layer', ['cors', 'compression', 'vary', 'cache', 'route'])
async def test_push_headers_are_not_response_headers(layer):
    import gzip

    from blackbull.app import _inject_response_headers
    from blackbull.middleware.cache import Cache
    from blackbull.middleware.compression import Compression
    from blackbull.middleware.cors import CORS

    request_headers = [(b'accept', b'text/css'), (b'content-type', b'text/plain')]
    message = NativeResponse(push='/style.css', header=list(request_headers))
    output = []

    async def send(event):
        output.extend(event.to_asgi())

    async def handler(conn, receive, send):
        await send(message)
        await send(NativeResponse(header=[(b'content-type', b'text/plain')], body=b'ok'))

    conn = Connection(method='GET', path='/', raw_path=b'/', type='http',
                      headers=Headers([(b'host', b'example.com'),
                                       (b'origin', b'https://example.com'),
                                       (b'accept-encoding', b'gzip')]))
    if layer == 'route':
        await handler(conn, AsyncMock(), _inject_response_headers(send, [(b'x-route', b'1')]))
    else:
        if layer == 'cors':
            middleware = CORS()
        elif layer == 'cache':
            middleware = Cache()
        else:
            middleware = Compression(min_size=1)
            middleware._available = {'gzip': gzip.compress}
            if layer == 'vary':
                conn.headers = Headers([(b'accept-encoding', b'unsupported')])
        await middleware(conn, AsyncMock(), send, handler)
    assert output[0] == {'type': 'http.response.push', 'path': '/style.css',
                         'headers': request_headers}
    assert [e['type'] for e in output] == [
        'http.response.push', 'http.response.start', 'http.response.body']


@pytest.mark.parametrize('kwargs', [
    {'body': b''}, {'status': 201}, {'trailers': []}, {'more_body': True},
    {'more_trailers': True}, {'expects_trailers': True}, {'file_path': '/tmp/file'},
])
def test_push_cannot_also_be_a_response(kwargs):
    with pytest.raises(ValueError, match='push'):
        NativeResponse(push='/style.css', **kwargs)


class TestAPushTakesItsAuthorityFromTheParent:
    def test_the_constructor_refuses_host(self):
        with pytest.raises(ValueError, match='host'):
            NativeResponse(push='/style.css', header=[(b'Host', b'a.example')])

    def test_assigning_or_appending_host_is_refused(self):
        message = NativeResponse(push='/style.css', header=[])
        with pytest.raises(ValueError, match='host'):
            message.header = [(b'host', b'a.example')]
        with pytest.raises(ValueError, match='host'):
            message.header.append(b'Host', b'a.example')
        assert list(message.header) == []

    def test_a_header_with_host_cannot_become_a_push(self):
        message = NativeResponse(header=[(b'host', b'a.example')])
        with pytest.raises(ValueError, match='host'):
            message.push = '/style.css'

    def test_a_view_taken_before_push_still_refuses_host(self):
        message = NativeResponse(header=[])
        view = message.header
        message.push = '/style.css'
        with pytest.raises(ValueError, match='host'):
            view.append(b'host', b'a.example')
