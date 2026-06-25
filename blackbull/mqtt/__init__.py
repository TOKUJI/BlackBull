"""MQTT 5 broker — a non-core "bridge" protocol shipped with BlackBull.

BlackBull's *core* protocols are the HTTP family (HTTP/1.1, HTTP/2, and — as
they land — gRPC and HTTP/3), which share the from-scratch HTTP stack the
framework exists to implement.  MQTT is a **bridge protocol**: an independent
protocol family that rides the Non-ASGI bridge but shares none of the HTTP
protocol logic.  It lives in its own subpackage so the boundary is explicit and
so it can be extracted to a standalone ``blackbull-mqtt`` distribution later
without touching the core (see ``docs/guide/mqtt.md``).

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
from .tap import Message, TapActor

__all__ = [
    'MQTTExtension', 'MQTTProtocolDetector', 'Message', 'Subscription',
    'AsyncAPIExtension',
    'BrokerActor', 'TapActor', 'serve_connection',
]
