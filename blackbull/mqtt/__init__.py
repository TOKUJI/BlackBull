"""MQTT 5 broker through the generic extension mechanism.

Register MQTTExtension with app.add_extension; HTTP dispatch stays independent.
See docs/guide/mqtt.md for broker and tap contracts.
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
