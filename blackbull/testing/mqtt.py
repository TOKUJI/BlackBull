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
returns.  A tap that raises propagates its exception out of ``publish`` —
where production logs and isolates a failing tap (taps are best-effort
observers), a test wants the failure visible.  The extension's own
``tap_mode`` is irrelevant here: a test wants determinism, not the production
decoupling.

This is the application-handler-level counterpart of the conformance suite's
in-process fake-reader harness (``tests/conformance/mqtt/conftest.py``) — that
drives the wire codec, this drives the taps.  Only the tap pipeline is
exercised: broker routing, QoS flows and retained messages are the
conformance suite's territory, not this helper's.
"""
from __future__ import annotations

from typing import Any

from ..mqtt import Message
from ..mqtt.extension import MQTTExtension
from ..mqtt.tap import run_taps


class MQTTTestBroker:
    """Test environment for an app's MQTT taps (no socket, no client).

    Parameters
    ----------
    app:
        The BlackBull app the MQTT extension is registered on.

    The helper is **async-only by design**: taps are coroutines and
    ``publish`` awaits them on the caller's event loop, so tests are written
    as ``async def`` (the suite runs with ``asyncio_mode = strict``).  A
    synchronous façade like :class:`blackbull.testing.native.NativeClient` is
    deliberately not provided — there is no background loop to bridge to, and
    no socket or broker that would need one.

    The ``async with`` form is ceremony: the broker binds no socket, starts no
    task and owns no resource, so entering and exiting are no-ops.  It is kept
    for a consistent test idiom, and so a later version can acquire state here
    without changing call sites.
    """

    def __init__(self, app: Any) -> None:
        ext = app.extensions.get(MQTTExtension.extension_key)
        if not isinstance(ext, MQTTExtension):
            raise RuntimeError(
                'MQTTTestBroker needs the MQTT extension registered on the '
                'app: app.add_extension(MQTTExtension(...)).')
        self._ext = ext

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

        A tap that raises propagates its exception, and the remaining taps in
        this ``publish`` are skipped — the first-failure semantics a
        sequential test loop already has.  Production, by contrast, logs and
        isolates a raising tap; ``publish`` deliberately does not.

        ``qos``, ``retain`` and ``properties`` are passed through to the
        :class:`~blackbull.mqtt.Message` without validation, so a test may
        exercise values the wire layer would reject.  Wire-level QoS
        validation is the conformance suite's territory, not this helper's.
        """
        message = Message(topic=topic, payload=payload, qos=qos,
                          retain=retain, properties=properties or {})
        await run_taps(self._ext.iter_taps(), message, raise_exceptions=True)
