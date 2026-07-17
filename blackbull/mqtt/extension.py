"""User-facing wiring for the MQTT 5 broker — detector + :class:`MQTTExtension`."""
from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any, Callable, Iterator, NamedTuple

from ..extension import Extension
from ..server.protocol_registry import ProtocolDetector
from .broker import BrokerActor
from .connection import serve_connection
from .tap import TapActor, compile_tap

logger = logging.getLogger(__name__)


class Subscription(NamedTuple):
    """A read-only view of one registered ``on_message`` tap.

    Yielded by :meth:`MQTTExtension.iter_subscriptions` so documentation tools
    (``AsyncAPIExtension``) can describe an app's taps without reaching into the
    private ``_handlers`` list.  ``topic`` is the filter as the application
    wrote it (``{name}`` captures restored); ``callback`` is the registered
    coroutine function (its ``__name__``/docstring name the AsyncAPI operation).
    """
    topic: str
    callback: Callable[..., Any]


class MQTTProtocolDetector(ProtocolDetector):
    """Recognise an MQTT connection from its first byte.

    Every MQTT session opens with a CONNECT packet whose fixed-header first
    byte is ``0x10`` (type 1, flags 0).  That is unambiguous against the HTTP
    request line and the HTTP/2 preface, both of which start with ASCII text.
    """

    def detect(self, first_bytes: bytes, alpn: str | None) -> bool:
        return bool(first_bytes) and first_bytes[0] == 0x10

    @property
    def protocol_name(self) -> str:
        return 'mqtt'


class MQTTExtension(Extension):
    """Wires the MQTT 5 broker into a BlackBull app as a non-ASGI protocol.

    Register it once and tap the broker's routing with :meth:`on_message`::

        from blackbull.mqtt import MQTTExtension, Message

        mqtt = app.add_extension(MQTTExtension(port=1883))

        @mqtt.on_message(topic='sensors/{room}/temperature')
        async def on_temp(msg: Message, room: str):
            print(room, msg.payload)

    The broker itself (CONNECT/SUBSCRIBE/PUBLISH/QoS flows, retained messages,
    Will delivery) runs whether or not any handler is registered; handlers are
    an application-level tap on top of normal broker routing.

    This instance owns a single :class:`BrokerActor` and a single
    :class:`TapActor` (both started on app startup, stopped on shutdown) — one
    broker and one tap consumer per worker, no module globals.  The class is
    self-contained and importable from a future ``blackbull-mqtt`` package
    without touching the core.

    ``tap_mode`` selects how taps are dispatched: ``'actor'`` (default) runs
    them on the decoupled :class:`TapActor` so a slow tap never back-pressures
    delivery; ``'inline'`` reproduces the Sprint 53 connection-actor dispatch
    and exists mainly so ``bench/mqtt/tap_throughput.py`` can compare the two on
    one build.  ``tap_queue_size`` bounds the actor-mode inbox (drop-newest on
    overflow).
    """

    extension_key = 'mqtt'

    def __init__(self, *, port: int = 1883, tls: bool = False,
                 tap_mode: str = 'actor', tap_queue_size: int = 1024) -> None:
        if tap_mode not in ('actor', 'inline'):
            raise ValueError(f"tap_mode must be 'actor' or 'inline', got {tap_mode!r}")
        self.port = port
        # Sprint 75 — serve the broker port over TLS (mqtts://, conventionally
        # port 8883).  Requires the server to have a certificate configured.
        self.tls = tls
        self.tap_mode = tap_mode
        self.tap_queue_size = tap_queue_size
        self._handlers: list[Any] = []   # compiled Tap objects
        self._broker = BrokerActor()
        self._tap = TapActor(self._handlers, queue_size=tap_queue_size)
        self._broker_task = None
        self._tap_task = None

    def on_message(self, topic: str = '#'):
        """Decorator: register an async ``(message, **captures) -> None`` tap for
        every PUBLISH whose topic matches *topic* (an MQTT topic filter, so ``+``
        and ``#`` wildcards apply, plus ``{name}`` capture segments).  The
        callback receives a :class:`~blackbull.mqtt.Message`."""
        def decorator(callback):
            self._handlers.append(compile_tap(topic, callback))
            return callback
        return decorator

    def iter_subscriptions(self) -> Iterator[Subscription]:
        """Yield a :class:`Subscription` for each registered ``on_message`` tap.

        A stable, public, read-only accessor over the compiled handlers — the
        seam ``AsyncAPIExtension`` reads instead of touching ``_handlers``.
        Reflects the handlers registered *at call time*, so a documentation
        endpoint that calls this per request picks up taps added after the
        documenting extension was wired in.
        """
        for tap in self._handlers:
            yield Subscription(topic=tap.display_filter, callback=tap.callback)

    def init_app(self, app: Any) -> None:
        broker = self._broker
        # In actor mode the connection offers to the shared TapActor; in inline
        # mode it runs the compiled handlers itself.
        tap = self._tap if self.tap_mode == 'actor' else None
        inline = None if self.tap_mode == 'actor' else self._handlers

        async def _serve(reader, writer, ctx):
            await serve_connection(reader, writer, ctx, broker,
                                   app_handlers=inline, tap=tap)

        app.register_protocol_handler(
            'mqtt', _serve, detector=MQTTProtocolDetector(), port=self.port,
            tls=self.tls)
        self._register(app)

    async def startup(self, app: Any) -> None:
        """Start the broker (and, in actor mode, the tap) inbox loops."""
        if self._broker_task is None:
            self._broker_task = asyncio.create_task(self._broker.run())
        if self.tap_mode == 'actor' and self._tap_task is None:
            self._tap_task = asyncio.create_task(self._tap.run())

    async def shutdown(self, app: Any) -> None:
        """Stop the broker and tap actors on lifespan shutdown."""
        for attr in ('_broker_task', '_tap_task'):
            task = getattr(self, attr)
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await asyncio.gather(task)
                setattr(self, attr, None)
