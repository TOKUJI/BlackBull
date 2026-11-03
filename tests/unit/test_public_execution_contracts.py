import importlib
import asyncio
import json
import logging
import ssl
import subprocess
import sys
import warnings

import pytest

from blackbull import BlackBull, Connection, Headers
from blackbull.asgi import ResponseBody, ResponseStart
from blackbull.logger import ColoredFormatter
from blackbull.native import NativeResponse
from blackbull.testing import NativeClient, NativeTestServer, TestClient
from blackbull.testing.native import get
from blackbull.protocol.structured_fields import Date
from blackbull.extension import Extension


@pytest.mark.parametrize('module_name', [
    'blackbull.connection', 'blackbull.di', 'blackbull.fault_injection._tls',
    'blackbull.fault_injection.catalogue', 'blackbull.fault_injection.catalogue.h1_client',
    'blackbull.fault_injection.h2_server', 'blackbull.fault_injection.oracle_h1',
    'blackbull.fault_injection.scenario_h2', 'blackbull.grpc', 'blackbull.middleware',
    'blackbull.mqtt', 'blackbull.protocol.framing', 'blackbull.protocol.http1',
    'blackbull.server', 'blackbull.server.cap_log', 'blackbull.server.connection_protocol',
    'blackbull.server.listener', 'blackbull.server.read_buffer', 'blackbull.testing',
    'blackbull.testing.native', 'blackbull.websocket', 'blackbull.client.scenario_oracle',
])
def test_declared_star_exports_exist_and_preserve_identity(module_name):
    with warnings.catch_warnings():
        warnings.simplefilter('ignore', DeprecationWarning)
        module = importlib.import_module(module_name)
        namespace = {}
        exec(f'from {module_name} import *', namespace)
    assert set(namespace) - {'__builtins__'} == set(module.__all__)
    assert all(namespace[name] is getattr(module, name) for name in module.__all__)


def test_asgi_adapter_and_test_client_defaults_reach_the_handler():
    app = BlackBull(asgi=True)

    @app.route(path='/')
    async def echo(conn):
        return {'header': conn.headers.get(b'x-default').decode(),
                'cookie': conn.headers.get(b'cookie').decode()}

    with TestClient(app.to_asgi()) as client:
        client.headers['x-default'] = 'visible'
        client.cookies.set('session', 'kept')
        assert client.get('/').json() == {'header': 'visible', 'cookie': 'session=kept'}
    with pytest.raises(RuntimeError, match='asgi=True'):
        BlackBull().to_asgi()


@pytest.mark.parametrize('factory', [NativeClient, NativeTestServer])
@pytest.mark.parametrize('method', ['request', 'head', 'options', 'put', 'patch', 'delete'])
def test_native_sync_facades_preserve_request_methods(factory, method):
    app = BlackBull()

    @app.route(path='/', methods=['GET', 'HEAD', 'OPTIONS', 'PUT', 'PATCH', 'DELETE'])
    async def echo(conn):
        return conn.method

    with factory(app) as owner:
        client = owner if factory is NativeClient else owner.client
        if method == 'request':
            if factory is NativeClient:
                response = client.request(Connection(method='GET', path='/', raw_path=b'/', headers=Headers([])))
            else:
                response = client.request('GET', '/')
        else:
            response = getattr(client, method)('/')
        if factory is NativeClient:
            assert response.status == 200
            body = response.body
        else:
            assert response.status_code == 200
            body = response.content
    expected = b'GET' if method == 'request' else method.upper().encode()
    assert body == (b'' if factory is NativeTestServer and method == 'head' else expected)


def test_colored_formatter_leaves_the_original_record_unmodified():
    formatter = ColoredFormatter('%(levelname)s %(message)s')
    record = logging.LogRecord('app', logging.WARNING, __file__, 1, 'value %s', ('x',), None)
    assert formatter.format(record) == '\033[33mWARNING\033[0m value x'
    assert record.levelname == 'WARNING'
    assert logging.Formatter('%(levelname)s %(message)s').format(record) == 'WARNING value x'


def test_terminal_logging_example_is_executable():
    result = subprocess.run([sys.executable, '-m', 'blackbull.logger'],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert '\033[' in result.stderr
    assert all(level in result.stderr for level in ('DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'))


@pytest.mark.parametrize('body', [None, b'', b'updated'])
def test_response_body_mutation_updates_the_length(body):
    response = NativeResponse(status=200, body=b'old')
    response.body = body
    assert response.body is body
    assert response.content_length == (len(body) if body is not None else 0)


def test_response_event_properties_include_defaults():
    assert ResponseStart().status == 200
    assert ResponseStart(status=202).status == 202
    assert ResponseBody().body == b''
    assert ResponseBody(body=b'data').body == b'data'


def test_date_representation_keeps_the_unix_seconds():
    assert repr(Date(-7)) == 'Date(-7)'


def test_legacy_header_priority_defaults_and_missing_attributes():
    from blackbull.protocol.frame import FrameFactory
    from blackbull.protocol.frame_types import FrameTypes, HeaderFrameFlags
    frame = FrameFactory().create(FrameTypes.HEADERS, HeaderFrameFlags.END_HEADERS, 1)
    assert frame.stream_dependency == 0
    assert frame.priority_weight == 1
    with pytest.raises(AttributeError):
        frame.no_such_field


@pytest.mark.asyncio
async def test_connection_closing_state_tracks_the_live_transport():
    from blackbull.client import ClientConnection
    from tests.harness import peer, Wire

    assert ClientConnection().is_closing()

    async def consume(reader, writer):
        await Wire(reader, writer).recv_all()

    async with peer(consume) as port:
        _, connection = await asyncio.get_running_loop().create_connection(
            ClientConnection, '127.0.0.1', port)
        assert not connection.is_closing()
        connection.close()
        await connection.wait_closed()
        assert connection.is_closing()


def test_extension_default_lifecycle_is_optional():
    class Minimal(Extension):
        extension_key = 'minimal'

        def init_app(self, app):
            app.extensions[self.extension_key] = self

    app = BlackBull()
    extension = Minimal()
    app.add_extension(extension)
    with NativeClient(app):
        assert app.extensions['minimal'] is extension


def test_legacy_middleware_predicate_is_the_current_predicate():
    from blackbull.router import has_inner, has_middleware_param
    assert has_inner is has_middleware_param
    assert has_inner(lambda conn, inner: None)
    assert has_inner(lambda conn, call_next: None)
    assert not has_inner(lambda conn: None)


@pytest.mark.asyncio
@pytest.mark.parametrize('http2', [False, True])
async def test_websocket_sessions_support_their_own_context_manager(http2, monkeypatch):
    from blackbull.client import WebSocketClient, WebSocketH2Client
    from blackbull.server.ws_codec import WSOpcode
    from blackbull.utils import Scheme
    from tests.harness import served

    monkeypatch.setenv('BB_H2_ENABLE_WEBSOCKET', '1')
    app = BlackBull()

    @app.route(path='/ws', scheme=Scheme.websocket)
    async def echo(conn, receive, send):
        await receive()
        await send({'type': 'websocket.accept'})
        while (event := await receive())['type'] != 'websocket.disconnect':
            await send({'type': 'websocket.send', 'text': event['text']})

    async with served(app, tls=http2) as origin:
        factory = WebSocketH2Client if http2 else WebSocketClient
        options = {}
        if http2:
            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            context.set_alpn_protocols(['h2'])
            options['ssl'] = context
        async with factory('127.0.0.1', origin.server.port, **options) as client:
            session = await client.connect('/ws')
            async with session as opened:
                assert opened is session
                await opened.send_text('hello')
                event = await opened.receive()
                if http2:
                    assert event == (WSOpcode.TEXT, b'hello')
                else:
                    assert event['text'] == 'hello'
            with pytest.raises(RuntimeError, match='closed'):
                await session.send_text('after closing')


@pytest.mark.asyncio
@pytest.mark.parametrize('kind,spec_path,docs_path', [
    ('openapi', '/openapi.json', '/docs'), ('asyncapi', '/asyncapi.json', '/asyncapi')])
async def test_documentation_endpoints_serve_their_spec_and_ui(kind, spec_path, docs_path):
    app = BlackBull()
    if kind == 'openapi':
        app.enable_openapi(title='Contract', version='1.2.3')
    else:
        from blackbull.mqtt import MQTTExtension
        from blackbull.mqtt.asyncapi import AsyncAPIExtension
        app.add_extension(MQTTExtension(port=0))
        app.add_extension(AsyncAPIExtension(title='Contract', version='1.2.3'))
    spec = await get(app, spec_path)
    assert spec.status == 200
    info = json.loads(spec.body)['info']
    assert {name: info[name] for name in ('title', 'version')} == {'title': 'Contract', 'version': '1.2.3'}
    page = await get(app, docs_path)
    assert page.status == 200
    assert spec_path.encode() in page.body
