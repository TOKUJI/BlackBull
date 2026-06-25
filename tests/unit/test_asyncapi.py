"""Tests for AsyncAPIExtension — AsyncAPI 3.0 docs for the MQTT broker taps.

Mirrors the early OpenAPI tests: shape assertions over the generated document
(0 / 1 / N handlers), the missing-MQTT error, lazy inclusion of taps registered
after the documenting extension, route wiring, the HTML viewer, and coexistence
with OpenAPI.
"""
import json

import pytest

from blackbull import BlackBull
from blackbull.mqtt import MQTTExtension, AsyncAPIExtension, Message
from blackbull.mqtt.asyncapi import (
    generate_asyncapi, asyncapi_html, _channel_id, _operation_id,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def app_with_mqtt():
    app = BlackBull()
    mqtt = app.add_extension(MQTTExtension(port=1883))

    @mqtt.on_message(topic='sensors/{room}/temperature')
    async def on_temp(msg: Message, room: str):
        """Temperature readings per room."""

    @mqtt.on_message(topic='alerts/#')
    async def on_alerts(msg: Message):
        ...

    return app, mqtt


def _gen(mqtt, **kw):
    kw.setdefault('title', 'T')
    kw.setdefault('version', '1.0.0')
    kw.setdefault('description', None)
    kw.setdefault('server_host', f'localhost:{mqtt.port}')
    return generate_asyncapi(mqtt, **kw)


# ---------------------------------------------------------------------------
# Envelope / servers
# ---------------------------------------------------------------------------

def test_envelope_is_asyncapi_3_0(app_with_mqtt):
    _app, mqtt = app_with_mqtt
    doc = _gen(mqtt, title='Sensor Gateway', version='2.3.4')
    assert doc['asyncapi'] == '3.0.0'
    assert doc['info']['title'] == 'Sensor Gateway'
    assert doc['info']['version'] == '2.3.4'
    assert doc['info']['description']  # default caveat blurb present


def test_server_describes_the_mqtt_broker(app_with_mqtt):
    _app, mqtt = app_with_mqtt
    broker = _gen(mqtt)['servers']['broker']
    assert broker == {
        'host': 'localhost:1883',
        'protocol': 'mqtt',
        'protocolVersion': '5.0',
    }


def test_custom_server_host_is_used(app_with_mqtt):
    _app, mqtt = app_with_mqtt
    doc = _gen(mqtt, server_host='mqtt.example.com:8883')
    assert doc['servers']['broker']['host'] == 'mqtt.example.com:8883'


def test_custom_description_overrides_default(app_with_mqtt):
    _app, mqtt = app_with_mqtt
    doc = _gen(mqtt, description='My bespoke description.')
    assert doc['info']['description'] == 'My bespoke description.'


# ---------------------------------------------------------------------------
# 0 / 1 / N handlers
# ---------------------------------------------------------------------------

def test_zero_handlers_has_empty_channels_and_operations():
    app = BlackBull()
    mqtt = app.add_extension(MQTTExtension())
    doc = _gen(mqtt)
    assert doc['channels'] == {}
    assert doc['operations'] == {}
    # No component message is declared when nothing references it.
    assert 'components' not in doc


def test_one_handler_emits_channel_operation_and_message():
    app = BlackBull()
    mqtt = app.add_extension(MQTTExtension())

    @mqtt.on_message(topic='sensors/+/temperature')
    async def on_temp(msg: Message):
        """One reading."""

    doc = _gen(mqtt)
    assert len(doc['channels']) == 1
    assert len(doc['operations']) == 1

    (chan_id, chan), = doc['channels'].items()
    assert chan['address'] == 'sensors/+/temperature'
    assert chan['messages']['message']['$ref'] == '#/components/messages/bytesPayload'

    (op_id, op), = doc['operations'].items()
    assert op_id == 'on_temp'
    assert op['action'] == 'receive'
    assert op['channel']['$ref'] == f'#/channels/{chan_id}'
    assert op['summary'] == 'One reading.'

    msg = doc['components']['messages']['bytesPayload']
    assert msg['contentType'] == 'application/octet-stream'
    assert msg['payload'] == {
        'type': 'string', 'format': 'binary',
        'description': 'Raw MQTT payload bytes (no schema declared).',
    }


def test_n_handlers_emit_n_channels_and_operations(app_with_mqtt):
    _app, mqtt = app_with_mqtt
    doc = _gen(mqtt)
    assert len(doc['channels']) == 2
    assert len(doc['operations']) == 2
    assert set(doc['operations']) == {'on_temp', 'on_alerts'}


def test_capture_segment_restored_in_channel_address(app_with_mqtt):
    _app, mqtt = app_with_mqtt
    addresses = {c['address'] for c in _gen(mqtt)['channels'].values()}
    assert 'sensors/{room}/temperature' in addresses
    assert 'alerts/#' in addresses


def test_operation_summary_omitted_without_docstring():
    app = BlackBull()
    mqtt = app.add_extension(MQTTExtension())

    @mqtt.on_message(topic='x/y')
    async def no_doc(msg: Message):
        pass

    op = next(iter(_gen(mqtt)['operations'].values()))
    assert 'summary' not in op


# ---------------------------------------------------------------------------
# id derivation + deduplication
# ---------------------------------------------------------------------------

def test_channel_id_derivation():
    assert _channel_id('sensors/{room}/temperature') == 'sensorsRoomTemperature'
    assert _channel_id('alerts/#') == 'alerts'
    assert _channel_id('sensors/+/temperature') == 'sensorsTemperature'
    assert _channel_id('#') == 'root'


def test_operation_id_from_callback_name():
    async def my_handler(msg):
        ...
    assert _operation_id(my_handler) == 'my_handler'
    assert _operation_id(lambda m: None) in ('onMessage', '<lambda>'.strip('<>'))


def test_duplicate_ids_are_deduplicated():
    app = BlackBull()
    mqtt = app.add_extension(MQTTExtension())

    # Two filters that derive the same channel id, and two callbacks with the
    # same __name__ — both must be made unique, not collide.
    @mqtt.on_message(topic='a/+')
    async def handler(msg: Message):
        ...

    # Re-bind the same name to a second registration with a colliding channel id.
    async def handler2(msg):  # noqa: D401
        ...
    handler2.__name__ = 'handler'
    mqtt.on_message(topic='a/+')(handler2)

    doc = _gen(mqtt)
    assert len(doc['channels']) == 2, 'colliding channel ids must be uniquified'
    assert len(doc['operations']) == 2, 'colliding operation ids must be uniquified'


# ---------------------------------------------------------------------------
# Extension wiring, lazy generation, errors
# ---------------------------------------------------------------------------

def test_extension_registers_spec_and_docs_routes(app_with_mqtt):
    app, _mqtt = app_with_mqtt
    app.add_extension(AsyncAPIExtension(title='X'))
    templates = {ri.template for ri in app._router._route_info}
    assert '/asyncapi.json' in templates
    assert '/asyncapi' in templates


def test_docs_path_can_be_disabled(app_with_mqtt):
    app, _mqtt = app_with_mqtt
    app.add_extension(AsyncAPIExtension(docs_path=None))
    templates = {ri.template for ri in app._router._route_info}
    assert '/asyncapi.json' in templates
    assert '/asyncapi' not in templates


def test_custom_spec_path(app_with_mqtt):
    app, _mqtt = app_with_mqtt
    app.add_extension(AsyncAPIExtension(spec_path='/docs/asyncapi.json', docs_path=None))
    templates = {ri.template for ri in app._router._route_info}
    assert '/docs/asyncapi.json' in templates


def test_missing_mqtt_raises_runtimeerror():
    app = BlackBull()
    ext = AsyncAPIExtension()
    ext.init_app(app)  # no MQTTExtension registered
    with pytest.raises(RuntimeError, match='requires an MQTTExtension'):
        ext._generate(app)


def test_lazy_generation_includes_handlers_added_after_extension():
    app = BlackBull()
    mqtt = app.add_extension(MQTTExtension())
    ext = app.add_extension(AsyncAPIExtension())

    # Register a tap AFTER the documenting extension was wired in.
    @mqtt.on_message(topic='late/topic')
    async def late(msg: Message):
        ...

    doc = ext._generate(app)
    assert any(c['address'] == 'late/topic' for c in doc['channels'].values())


def test_document_is_json_serializable(app_with_mqtt):
    _app, mqtt = app_with_mqtt
    doc = _gen(mqtt)
    assert json.loads(json.dumps(doc)) == doc


# ---------------------------------------------------------------------------
# HTML viewer + coexistence with OpenAPI
# ---------------------------------------------------------------------------

def test_asyncapi_html_contains_spec_url_and_renderer():
    html = asyncapi_html('/custom.json', title='Hello AsyncAPI')
    assert '<title>Hello AsyncAPI</title>' in html
    assert 'url: "/custom.json"' in html
    assert '@asyncapi/react-component' in html
    assert 'AsyncApiStandalone.render' in html


def test_coexists_with_openapi(app_with_mqtt):
    app, _mqtt = app_with_mqtt
    app.enable_openapi(title='HTTP API')
    app.add_extension(AsyncAPIExtension(title='MQTT API'))
    templates = {ri.template for ri in app._router._route_info}
    assert {'/openapi.json', '/docs', '/asyncapi.json', '/asyncapi'} <= templates
    assert app.extensions['openapi'] is not app.extensions['asyncapi']
