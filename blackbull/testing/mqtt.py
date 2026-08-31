"""In-process MQTT test environment — drive ``on_message`` taps with no socket.

An application developer testing a ``@mqtt.on_message`` tap used to have to
stand up a real broker and a real MQTT client (``mosquitto_pub``).  This
module feeds PUBLISHes into the app's registered taps directly — topic
matching and ``{name}`` captures included — with no TCP socket, no CONNECT,
and no MQTT client::

    from blackbull import BlackBull
    from blackbull.mqtt import MQTTExtension, Message
    from blackbull.testing.mqtt import MQTTTestBroker

    app = BlackBull()
    mqtt = app.add_extension(MQTTExtension(port=1883))
    captured = []

    @mqtt.on_message(topic='sensors/{room}/temperature')
    async def on_temp(msg: Message, room: str):
        captured.append((room, msg.payload))

    async def test_tap():
        async with MQTTTestBroker(app) as broker:
            await broker.publish(topic='sensors/room1/temperature', payload=b'21.5')
        assert captured == [('room1', b'21.5')]

``publish`` dispatches to the matching taps inline and awaits their
completion, so the test asserts on the side effect the moment ``publish``
returns.  The extension's own ``tap_mode`` is irrelevant here: a test wants
determinism, not the production decoupling.

This is the application-handler-level counterpart of the conformance suite's
in-process fake-reader harness (``tests/conformance/mqtt/conftest.py``) — that
drives the wire codec, this drives the taps.  Only the tap pipeline is
exercised: broker routing, QoS flows and retained messages are the
conformance suite's territory, not this helper's.
"""
from __future__ import annotations

from typing import Any

from ..mqtt import Message
from ..mqtt.tap import compile_taps, run_taps


class MQTTTestBroker:
    """Test environment for an app's MQTT taps (no socket, no client).

    Parameters
    ----------
    app:
        The BlackBull app the MQTT extension is registered on.
    port:
        Accepted for API parity with :class:`MQTTExtension`; no socket is
        bound, so the value is unused.
    """

    def __init__(self, app: Any, *, port: int = 0) -> None:
        ext = app.extensions.get('mqtt')
        if ext is None or not hasattr(ext, 'iter_subscriptions'):
            raise RuntimeError(
                'MQTTTestBroker needs the MQTT extension registered on the '
                'app: app.add_extension(MQTTExtension(...)).')
        self._ext = ext
        self._port = port

    async def __aenter__(self) -> 'MQTTTestBroker':
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        return None

    async def publish(
        self,
        topic: str,
        payload: bytes = b'',
        *,
        qos: int = 0,
        retain: bool = False,
        properties: dict | None = None,
    ) -> None:
        """Dispatch one PUBLISH to every matching tap and await them.

        ``topic`` is matched against the registered filters — ``+`` / ``#``
        wildcards and ``{name}`` captures apply — and captured segments are
        bound as keyword arguments.  Taps run inline and are complete when
        this returns.
        """
        message = Message(topic=topic, payload=payload, qos=qos,
                          retain=retain, properties=properties or {})
        taps = compile_taps(
            (subscription.topic, subscription.callback)
            for subscription in self._ext.iter_subscriptions())
        await run_taps(taps, message)
