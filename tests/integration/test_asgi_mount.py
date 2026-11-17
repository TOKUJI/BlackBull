import asyncio

import pytest

from blackbull import ASGIApp, BlackBull
from blackbull.client import HTTP1Client, HTTP2Client
from blackbull.client.exceptions import ClientError
from blackbull.connection import Connection
from blackbull.headers import Headers
from blackbull.testing import NativeTestServer
from tests.harness import served
from tests.integration.test_asgi_app import _listen
from tests.integration.test_foreign_asgi_compatibility import ForeignApp, transport


pytestmark = [pytest.mark.integration, pytest.mark.asyncio, pytest.mark.timeout(20)]
EVENTS = ['request_received', 'before_handler', 'after_handler',
          'request_completed', 'scope_completed']


async def _ok(scope, receive, send):
    await send({'type': 'http.response.start', 'status': 200, 'headers': []})
    await send({'type': 'http.response.body', 'body': b'foreign'})


async def test_mount_routing_lifespan_and_event_ownership(transport):
    client_type, tls, context = transport
    foreign = ForeignApp(_ok)
    adapter = ASGIApp(foreign, mounted=True)
    host = BlackBull()
    host.mount('/foreign', adapter)
    host.mount('/alias', adapter)
    native_connections = []

    async def middleware(conn, receive, send, call_next):
        assert isinstance(conn, Connection)
        native_connections.append(conn)
        before = conn.path, conn.raw_path, conn.root_path
        await call_next(conn, receive, send)
        assert (conn.path, conn.raw_path, conn.root_path) == before

    host.use(middleware)

    @host.route(path='/native')
    async def native():
        return 'native'

    assert [route.path for route in host.get_routes()] == ['/native']
    events = _listen(host, EVENTS)
    adapter_events = _listen(adapter, EVENTS)
    urls = ['/foreign', '/foreign/', '/foreign/deep%20path?q=1', '/alias/child']
    async with served(host, tls=tls) as server:
        async with client_type('127.0.0.1', server.server.port, ssl=context) as client:
            for url in urls:
                response = await client.request('BREW', url)
                assert (response.status, response.body) == (200, b'foreign')
            assert (await client.request('GET', '/foreigner')).status == 404
            assert (await client.request('GET', '/native')).body == b'native'
    assert foreign.phases == ['startup', 'shutdown']
    assert [s['root_path'] for s in foreign.requests] == ['/foreign'] * 3 + ['/alias']
    assert [s['path'] for s in foreign.requests] == [
        '/foreign', '/foreign/', '/foreign/deep path', '/alias/child']
    assert foreign.requests[2]['raw_path'] == b'/foreign/deep%20path'
    assert foreign.requests[2]['query_string'] == b'q=1'
    assert all(scope['state']['pool'] is foreign.pool for scope in foreign.requests)
    assert all(len(events[name]) == (6 if name != 'before_handler' and name != 'after_handler' else 5)
               for name in EVENTS)
    assert all(details == [] for details in adapter_events.values())
    assert all(not detail['conn'].path_params for detail in events['scope_completed'])
    assert len(native_connections) == 6


@pytest.mark.parametrize('failure', ['before', 'after', 'complete', 'return'])
async def test_mounted_errors_are_reported_by_host_once(failure, transport):
    client_type, tls, context = transport
    async def response(scope, receive, send):
        if failure == 'return':
            return
        if failure != 'before':
            await send({'type': 'http.response.start', 'status': 200, 'headers': []})
            await send({'type': 'http.response.body', 'body': b'ok',
                        'more_body': failure == 'after'})
        raise ValueError('foreign failure')

    host = BlackBull()
    foreign = ForeignApp(response)
    adapter = ASGIApp(foreign, mounted=True)
    host.mount('/foreign', adapter)
    events = _listen(host, EVENTS)
    adapter_events = _listen(adapter, EVENTS)
    @host.route(path='/native')
    async def native():
        return 'native'

    async with served(host, tls=tls) as server:
        if client_type is HTTP1Client:
            wire = await server.wire()
            await wire.send(b'GET /foreign HTTP/1.1\r\nhost: localhost\r\n\r\n')
            if failure == 'after':
                data = await wire.recv_all()
                assert data.count(b'HTTP/1.1') == 1
                assert b'500' not in data
            else:
                reply = await wire.response()
                assert reply.status == (200 if failure == 'complete' else 500)
        else:
            async with HTTP2Client('127.0.0.1', server.server.port, ssl=context) as client:
                if failure == 'after':
                    with pytest.raises(ClientError):
                        await client.request('GET', '/foreign')
                else:
                    reply = await client.request('GET', '/foreign')
                    assert reply.status == (200 if failure == 'complete' else 500)
                assert (await client.request('GET', '/native')).body == b'native'
    assert all(len(v) == (1 if client_type is HTTP1Client else 2) for v in events.values())
    if failure != 'return':
        assert isinstance(events['after_handler'][0]['exception'], ValueError)
        assert isinstance(events['scope_completed'][0]['exception'], ValueError)
    assert all(details == [] for details in adapter_events.values())


async def test_mount_registration_contract():
    host = BlackBull()
    with pytest.raises(ValueError, match='mounted'):
        host.mount('/foreign', ASGIApp(_ok))
    with pytest.raises(TypeError, match='foreign'):
        ASGIApp(BlackBull(), mounted=True)
    adapter = ASGIApp(_ok, mounted=True, lifespan=False)
    host.mount('/foreign', adapter)
    with pytest.raises(ValueError, match='host'):
        BlackBull().mount('/other', adapter)
    async with NativeTestServer(host):
        with pytest.raises(RuntimeError, match='frozen'):
            host.mount('/late', ASGIApp(_ok, mounted=True))


async def test_mount_lifespan_is_reclaimed_when_later_host_startup_fails():
    foreign = ForeignApp(_ok)
    host = BlackBull()
    host.mount('/foreign', ASGIApp(foreign, mounted=True))

    @host.on_startup
    async def fail():
        raise ValueError('host startup failed')

    with pytest.raises(RuntimeError, match='host startup failed'):
        async with NativeTestServer(host):
            pytest.fail('startup must fail')
    assert foreign.phases == ['startup', 'shutdown']


async def test_mounted_placement_cannot_be_used_as_a_route_or_server_app():
    adapter = ASGIApp(_ok, mounted=True)
    with pytest.raises(RuntimeError, match='host'):
        async with NativeTestServer(adapter):
            pytest.fail('mounted adapter requires a host')
    host = BlackBull()
    host.route(path='/wrong')(adapter)
    async with served(host) as server:
        async with HTTP1Client('127.0.0.1', server.server.port) as client:
            assert (await client.request('GET', '/wrong')).status == 500


async def test_root_mount_preserves_more_specific_native_routes():
    foreign = ForeignApp(_ok)
    host = BlackBull()
    host.mount('/', ASGIApp(foreign, mounted=True))

    @host.route(path='/native')
    async def native():
        return 'native'

    async with served(host) as server:
        async with HTTP1Client('127.0.0.1', server.server.port) as client:
            assert (await client.request('GET', '/native')).body == b'native'
            for path in ['/', '/deep/path']:
                assert (await client.request('GET', path)).body == b'foreign'
    assert [s['root_path'] for s in foreign.requests] == ['', '']


async def test_all_mounted_lifespans_are_closed_when_one_shutdown_fails():
    async def fail_shutdown(scope, receive, send):
        await receive()
        await send({'type': 'lifespan.startup.complete'})
        await receive()
        await send({'type': 'lifespan.shutdown.failed', 'message': 'shutdown failed'})

    host = BlackBull()
    foreign = ForeignApp(_ok)
    host.mount('/failing', ASGIApp(fail_shutdown, mounted=True))
    host.mount('/healthy', ASGIApp(foreign, mounted=True))
    with pytest.raises(Exception, match='shutdown'):
        async with NativeTestServer(host):
            pass
    assert foreign.phases == ['startup', 'shutdown']


@pytest.mark.parametrize('path', ['', 'relative', '//foreign', '/foreign/{path}', '/foreign?query'])
async def test_invalid_mount_prefix_does_not_register_a_lifespan(path):
    host = BlackBull()
    foreign = ForeignApp(_ok)
    adapter = ASGIApp(foreign, mounted=True)
    with pytest.raises(ValueError):
        host.mount(path, adapter)
    async with NativeTestServer(host):
        pass
    assert foreign.phases == []


async def test_mount_cancellation_keeps_host_connection_metadata():
    entered = asyncio.Event()

    async def response(scope, receive, send):
        entered.set()
        await asyncio.Event().wait()

    host = BlackBull()
    adapter = ASGIApp(response, mounted=True, lifespan=False)
    host.mount('/foreign', adapter)
    conn = Connection(path='/foreign/item', raw_path=b'/foreign/item', method='GET',
                      headers=Headers([]))
    events = _listen(host, EVENTS)

    async def send(event):
        pass

    task = asyncio.create_task(host(conn, None, send))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (conn.path, conn.raw_path, conn.root_path) == ('/foreign/item', b'/foreign/item', '')
    assert isinstance(events['scope_completed'][0]['exception'], asyncio.CancelledError)


async def test_head_reaches_foreign_app_without_changing_native_dispatch(transport):
    client_type, tls, context = transport
    foreign = ForeignApp(_ok)
    host = BlackBull()
    host.mount('/foreign', ASGIApp(foreign, mounted=True))
    methods = []

    @host.route(path='/native')
    async def native(conn):
        methods.append(conn.method)
        return 'native'

    async with served(host, tls=tls) as server:
        async with client_type('127.0.0.1', server.server.port, ssl=context) as client:
            assert (await client.request('HEAD', '/foreign')).body == b''
            assert (await client.request('HEAD', '/native')).body == b''
    assert foreign.requests[0]['method'] == 'HEAD'
    assert methods == ['GET']


async def test_mounted_grpc_body_and_trailers(transport):
    client_type, tls, context = transport
    if client_type is HTTP1Client:
        pytest.skip('gRPC uses HTTP/2')
    payload = b'\x00\x00\x00\x00\x02ok'

    async def response(scope, receive, send):
        assert (b'content-type', b'application/grpc') in scope['headers']
        body = bytearray()
        while True:
            event = await receive()
            body.extend(event.get('body', b''))
            if not event.get('more_body'):
                break
        assert bytes(body) == payload
        await send({'type': 'http.response.start', 'status': 200, 'trailers': True,
                    'headers': [(b'content-type', b'application/grpc')]})
        await send({'type': 'http.response.body', 'body': payload})
        await send({'type': 'http.response.trailers', 'headers': [(b'grpc-status', b'0')]})

    host = BlackBull()
    host.mount('/grpc', ASGIApp(ForeignApp(response), mounted=True))
    async with served(host, tls=tls) as server:
        async with HTTP2Client('127.0.0.1', server.server.port, ssl=context) as client:
            for _ in range(2):
                reply = await client.request('POST', '/grpc/svc/Call', body=payload,
                    headers=[(b'content-type', b'application/grpc')])
                assert (reply.status, reply.body, reply.trailers.get(b'grpc-status')) == (
                    200, payload, b'0')


async def test_standalone_foreign_app_also_receives_head(transport):
    client_type, tls, context = transport
    foreign = ForeignApp(_ok)
    async with served(ASGIApp(foreign), tls=tls) as server:
        async with client_type('127.0.0.1', server.server.port, ssl=context) as client:
            reply = await client.request('HEAD', '/')
            assert (reply.status, reply.body) == (200, b'')
    assert foreign.requests[0]['method'] == 'HEAD'


async def test_host_grpc_policy_still_precedes_mount_routing():
    from blackbull.grpc import GrpcServiceRegistry

    host = BlackBull()
    host.enable_grpc(GrpcServiceRegistry())
    foreign = ForeignApp(_ok)
    host.mount('/foreign', ASGIApp(foreign, mounted=True))
    async with served(host) as server:
        async with HTTP2Client('127.0.0.1', server.server.port) as client:
            reply = await client.request('POST', '/foreign/svc/Call',
                headers=[(b'content-type', b'application/grpc')], body=b'\x00\x00\x00\x00\x00')
            assert reply.trailers.get(b'grpc-status') == b'12'
    assert foreign.requests == []
