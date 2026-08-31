"""Integration tests for :class:`blackbull.testing.mqtt.MQTTTestBroker`.

The helper feeds PUBLISHes into an app's ``on_message`` taps with no socket —
the application-handler-level counterpart of the conformance suite's wire
harness.  These tests assert the tap side effects a developer would assert:
right payload, ``{name}`` captures bound, non-matching topics silent, and the
missing-extension error.
"""
import pytest

from blackbull import BlackBull
from blackbull.mqtt import MQTTExtension, Message
from blackbull.testing.mqtt import MQTTTestBroker

pytestmark = pytest.mark.integration


def _app_with_tap(topic: str, captured: list):
    app = BlackBull()
    mqtt = app.add_extension(MQTTExtension(port=1883))

    @mqtt.on_message(topic=topic)
    async def _tap(msg: Message, **captures):
        captured.append((msg, captures))

    return app


@pytest.mark.asyncio
async def test_publish_dispatches_to_the_matching_tap():
    captured = []
    app = _app_with_tap('sensors/+/temperature', captured)
    async with MQTTTestBroker(app) as broker:
        await broker.publish(topic='sensors/room1/temperature', payload=b'21.5')
    assert len(captured) == 1
    msg, captures = captured[0]
    assert msg.topic == 'sensors/room1/temperature'
    assert msg.payload == b'21.5'
    assert captures == {}


@pytest.mark.asyncio
async def test_name_captures_are_bound_as_keyword_arguments():
    captured = []
    app = _app_with_tap('sensors/{room}/{metric}', captured)
    async with MQTTTestBroker(app) as broker:
        await broker.publish(topic='sensors/kitchen/humidity', payload=b'42')
    assert len(captured) == 1
    msg, captures = captured[0]
    assert captures == {'room': 'kitchen', 'metric': 'humidity'}


@pytest.mark.asyncio
async def test_non_matching_topic_does_not_fire_the_tap():
    captured = []
    app = _app_with_tap('sensors/+/temperature', captured)
    async with MQTTTestBroker(app) as broker:
        await broker.publish(topic='other/room1/temperature', payload=b'1')
    assert captured == []


@pytest.mark.asyncio
async def test_publish_awaits_the_tap_handler():
    """The side effect is visible the moment publish returns — no flush wait."""
    captured = []

    app = BlackBull()
    mqtt = app.add_extension(MQTTExtension(port=1883))

    @mqtt.on_message(topic='jobs/#')
    async def _tap(msg: Message):
        captured.append(msg.payload)

    async with MQTTTestBroker(app) as broker:
        await broker.publish(topic='jobs/import', payload=b'job-1')
    assert captured == [b'job-1']


@pytest.mark.asyncio
async def test_qos_and_retain_reach_the_message():
    captured = []
    app = _app_with_tap('meta/#', captured)
    async with MQTTTestBroker(app) as broker:
        await broker.publish(topic='meta/flag', payload=b'x', qos=1, retain=True)
    msg, _ = captured[0]
    assert msg.qos == 1 and msg.retain is True


@pytest.mark.asyncio
async def test_missing_extension_raises_a_clear_error():
    app = BlackBull()
    with pytest.raises(RuntimeError, match='MQTT extension'):
        MQTTTestBroker(app)


@pytest.mark.asyncio
async def test_taps_registered_after_construction_are_seen():
    """iter_subscriptions reflects handlers at publish time (at-call-time)."""
    captured = []
    app = BlackBull()
    mqtt = app.add_extension(MQTTExtension(port=1883))

    broker = MQTTTestBroker(app)
    async with broker:
        @mqtt.on_message(topic='late/#')
        async def _late(msg: Message):
            captured.append(msg.payload)
        await broker.publish(topic='late/arrival', payload=b'now')
    assert captured == [b'now']
