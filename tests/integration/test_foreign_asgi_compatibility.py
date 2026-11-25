"""Foreign applications reach the actual lifespan and protocol boundaries."""
import ssl

import pytest

from blackbull import ASGIApp, BlackBull
from blackbull.client import HTTP1Client, HTTP2Client, WebSocketClient
from blackbull.client.exceptions import HandshakeError
from hpack import Decoder, Encoder

from blackbull.protocol.frame_types import FrameTypes
from tests.conformance.http2._harness import _make_h2_frame, h2_connect, h2_section
from tests.harness import read_h2_frame, served
from tests.integration.test_asgi_app import _listen, _misbehaving


pytestmark = [pytest.mark.integration, pytest.mark.asyncio, pytest.mark.timeout(20)]


@pytest.fixture(params=[(HTTP1Client, False), (HTTP1Client, True),
                       (HTTP2Client, False), (HTTP2Client, True)],
                ids=['h1', 'h1tls', 'h2', 'h2tls'])
def transport(request):
    client, tls = request.param
    context = None
    if tls:
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        context.set_alpn_protocols(['h2' if client is HTTP2Client else 'http/1.1'])
    return client, tls, context


class ForeignApp:
    def __init__(self, respond):
        self.respond = respond
        self.phases = []
        self.requests = []
        self.pool = []

    async def __call__(self, scope, receive, send):
        if scope['type'] == 'lifespan':
            scope['state']['pool'] = self.pool
            for phase in ('startup', 'shutdown'):
                message = await receive()
                assert message['type'] == f'lifespan.{phase}'
                self.phases.append(phase)
                await send({'type': f'lifespan.{phase}.complete'})
            return
        assert self.phases == ['startup']
        self.requests.append(scope)
        await self.respond(scope, receive, send)


async def test_scope_streaming_and_lifespan_across_transports(transport):
    client_type, tls, context = transport

    async def echo(scope, receive, send):
        assert scope['type'] == 'http'
        assert 'local' not in scope['state']
        scope['state']['local'] = True
        body = bytearray()
        while True:
            event = await receive()
            assert event['type'] == 'http.request'
            body.extend(event.get('body', b''))
            if not event.get('more_body', False):
                break
        await send({'type': 'http.response.start', 'status': 200,
                    'headers': [(b'x-foreign', b'yes')]})
        await send({'type': 'http.response.body', 'body': bytes(body), 'more_body': True})
        await send({'type': 'http.response.body', 'body': b'!'})

    app = ForeignApp(echo)
    async with served(ASGIApp(app), tls=tls) as server:
        async with client_type('127.0.0.1', server.server.port, ssl=context) as client:
            for _ in range(2):
                response = await client.request('POST', '/echo?q=1', body=b'payload')
                assert (response.status, response.body) == (200, b'payload!')
                assert response.headers.get(b'x-foreign') == b'yes'
    assert app.phases == ['startup', 'shutdown']
    assert len(app.requests) == 2
    for scope in app.requests:
        assert (scope['path'], scope['query_string']) == ('/echo', b'q=1')
        assert scope['scheme'] == ('https' if tls else 'http')
        assert scope['http_version'] == ('2' if client_type is HTTP2Client else '1.1')
        assert scope['state']['pool'] is app.pool
        extensions = scope['extensions']
        assert ('http.response.pathsend' in extensions) == (not tls and client_type is HTTP1Client)
        assert ('http.response.http2_stream' in extensions) == (client_type is HTTP2Client)
        assert 'http.response.trailers' in extensions
        assert 'http.response.push' not in extensions  # Client disables push by default.
    assert app.requests[0]['state'] is not app.requests[1]['state']


@pytest.mark.parametrize('path', ['/raise', '/return'])
async def test_error_before_response_with_foreign_lifespan(transport, path):
    client_type, tls, context = transport
    app = ForeignApp(_misbehaving([]))
    async with served(ASGIApp(app), tls=tls) as server:
        async with client_type('127.0.0.1', server.server.port, ssl=context) as client:
            response = await client.request('GET', path)
            assert response.status == 500
            assert (await client.request('GET', '/after-end')).body == b'ok'
    assert app.phases == ['startup', 'shutdown']


async def test_trailers_with_foreign_lifespan(transport):
    client_type, tls, context = transport

    async def response(scope, receive, send):
        await send({'type': 'http.response.start', 'status': 200, 'trailers': True,
                    'headers': [(b'trailer', b'x-check')]})
        await send({'type': 'http.response.body', 'body': b'data'})
        await send({'type': 'http.response.trailers', 'headers': [(b'x-check', b'ok')]})

    app = ForeignApp(response)
    async with served(ASGIApp(app), tls=tls) as server:
        if client_type is HTTP1Client:
            wire = await server.wire()
            await wire.send(b'GET / HTTP/1.1\r\nhost: localhost\r\nte: trailers\r\n\r\n')
            reply = await wire.response()
            assert dict(reply.trailers)[b'x-check'] == b'ok'
        else:
            async with client_type('127.0.0.1', server.server.port, ssl=context) as client:
                reply = await client.request('GET', '/', headers=[(b'te', b'trailers')])
                assert reply.trailers.get(b'x-check') == b'ok'
        assert reply.body == b'data'
    assert app.phases == ['startup', 'shutdown']


async def test_exception_after_completed_response_preserves_connection(transport):
    client_type, tls, context = transport

    async def response(scope, receive, send):
        await send({'type': 'http.response.start', 'status': 200, 'headers': []})
        await send({'type': 'http.response.body', 'body': b'ok'})
        raise RuntimeError('after completion')

    app = ForeignApp(response)
    async with served(ASGIApp(app), tls=tls) as server:
        async with client_type('127.0.0.1', server.server.port, ssl=context) as client:
            for _ in range(2):
                reply = await client.request('GET', '/')
                assert (reply.status, reply.body) == (200, b'ok')
    assert len(app.requests) == 2
    assert app.requests[0]['client'] == app.requests[1]['client']
    assert app.phases == ['startup', 'shutdown']


async def test_file_response_uses_only_advertised_pathsend(transport, tmp_path):
    client_type, tls, context = transport
    path = tmp_path / 'payload.txt'
    path.write_bytes(b'foreign file response')

    async def response(scope, receive, send):
        await send({'type': 'http.response.start', 'status': 200, 'headers': []})
        if 'http.response.pathsend' in scope['extensions']:
            await send({'type': 'http.response.pathsend', 'path': str(path)})
        else:
            await send({'type': 'http.response.body', 'body': path.read_bytes()})

    app = ForeignApp(response)
    async with served(ASGIApp(app), tls=tls) as server:
        async with client_type('127.0.0.1', server.server.port, ssl=context) as client:
            assert (await client.request('GET', '/')).body == path.read_bytes()
    assert app.phases == ['startup', 'shutdown']


async def test_foreign_websocket_can_reject_with_lifespan_state(transport, monkeypatch):
    monkeypatch.setenv('BB_H2_ENABLE_WEBSOCKET', '1')
    client_type, tls, context = transport

    async def reject(scope, receive, send):
        assert scope['type'] == 'websocket'
        assert scope['state']['pool'] is app.pool
        assert (await receive())['type'] == 'websocket.connect'
        await send({'type': 'websocket.close'})

    app = ForeignApp(reject)
    adapter = ASGIApp(app)
    events = _listen(adapter, ['scope_completed'])
    ws_type = HTTP2Client if client_type is HTTP2Client else WebSocketClient
    async with served(adapter, tls=tls) as server:
        async with ws_type('127.0.0.1', server.server.port, ssl=context) as client:
            if client_type is HTTP2Client:
                reply = await client.request(
                    'CONNECT', '/ws', headers=[(b':protocol', b'websocket')])
                assert reply.status == 403
            else:
                with pytest.raises(HandshakeError, match='status=403'):
                    await client.connect('/ws')
    assert [scope['type'] for scope in app.requests] == ['websocket']
    assert len(events['scope_completed']) == 1
    assert app.phases == ['startup', 'shutdown']


async def test_class_middleware_keeps_actor_events_on_the_adapter():
    inner = BlackBull()

    @inner.route(path='/')
    async def hello():
        return 'ok'

    class Middleware:
        async def __call__(self, scope, receive, send):
            await inner(scope, receive, send)

    adapter = ASGIApp(Middleware())
    inner_events = _listen(inner, ['connection_accepted'])
    adapter_events = _listen(adapter, ['connection_accepted'])
    async with served(adapter) as server:
        async with HTTP1Client('127.0.0.1', server.server.port) as client:
            assert (await client.request('GET', '/')).body == b'ok'
    assert len(adapter_events['connection_accepted']) == 1
    assert inner_events['connection_accepted'] == []


@pytest.mark.parametrize('tls', [False, True], ids=['h2c', 'h2tls'])
async def test_full_duplex_stream_over_h2(tls):
    """Each request chunk is answered before the next is sent, then trailers end the stream."""

    async def bidi(scope, receive, send):
        assert 'http.response.trailers' in scope['extensions']
        await send({'type': 'http.response.start', 'status': 200, 'trailers': True,
                    'headers': [(b'content-type', b'application/connect+proto')]})
        while True:
            event = await receive()
            if event.get('body'):
                await send({'type': 'http.response.body', 'body': event['body'].upper(),
                            'more_body': True})
            if not event.get('more_body', False):
                break
        await send({'type': 'http.response.body', 'body': b''})
        await send({'type': 'http.response.trailers', 'headers': [(b'x-end', b'ok')]})

    async def next_on_stream(wire, wanted):
        while True:
            kind, flags, stream, payload = await read_h2_frame(wire.reader)
            if stream == 1 and kind in wanted:
                return kind, flags, payload

    app = ForeignApp(bidi)
    encoder, decoder = Encoder(), Decoder()
    async with served(ASGIApp(app), tls=tls) as server:
        wire = await h2_connect(server, alpn=['h2'])
        await wire.send(h2_section(encoder.encode([
            (b':method', b'POST'), (b':scheme', b'https' if tls else b'http'),
            (b':path', b'/svc/Bidi'), (b':authority', b'localhost'), (b'te', b'trailers'),
        ]), end_stream=False))
        for i, chunk in enumerate((b'one', b'two', b'three')):
            await wire.send(_make_h2_frame(FrameTypes.DATA, 0, 1, chunk))
            if i == 0:  # The head may wait for the first body chunk.
                kind, _, payload = await next_on_stream(wire, {FrameTypes.HEADERS[0]})
                assert (b':status', b'200') in decoder.decode(payload, raw=True)
            kind, flags, payload = await next_on_stream(wire, {FrameTypes.DATA[0]})
            assert (payload, flags & 1) == (chunk.upper(), 0)
        await wire.send(_make_h2_frame(FrameTypes.DATA, 1, 1))
        kind, flags, payload = await next_on_stream(wire, {FrameTypes.HEADERS[0]})
        assert flags & 1
        assert decoder.decode(payload, raw=True) == [(b'x-end', b'ok')]
    assert app.phases == ['startup', 'shutdown']
