"""MQTT 5 broker — a non-core "bridge" protocol shipped with BlackBull.

A **bridge protocol**: it rides the Non-ASGI bridge and shares none of the
HTTP stack the framework exists to implement.  Its own subpackage keeps that
boundary explicit and leaves it extractable as a standalone
``blackbull-mqtt`` distribution.  See ``docs/guide/mqtt.md``.

Wire it in through the generic extension seam::

    from blackbull import BlackBull
    from blackbull.mqtt import MQTTExtension

    app = BlackBull()
    mqtt = app.add_extension(MQTTExtension(port=1883))

    @mqtt.on_message(topic='sensors/+/temperature')
    async def on_temp(msg: Message):
        print(msg.topic, msg.payload)
"""
from .asyncapi import AsyncAPIExtension
from .broker import BrokerActor
from .connection import serve_connection
from .extension import MQTTExtension, MQTTProtocolDetector, Subscription
from .tap import Message, Tap, TapActor

__all__ = [
    'MQTTExtension', 'MQTTProtocolDetector', 'Message', 'Subscription', 'Tap',
    'AsyncAPIExtension',
    'BrokerActor', 'TapActor', 'serve_connection',
]
