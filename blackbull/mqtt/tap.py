"""Application taps on broker routing — the ``on_message`` observability layer.

A *tap* is an async ``(message, **captures) -> None`` callback registered via
:meth:`blackbull.mqtt.MQTTExtension.on_message` for a topic filter.  Taps are
best-effort observers on top of normal broker routing; the broker runs whether
or not any tap is registered.

Two dispatch engines share one code path (:func:`run_taps`):

* **actor** (default) — :class:`TapActor` is a single, lifespan-owned consumer.
  A connection actor hands it a :class:`Message` with :meth:`TapActor.offer`
  (non-blocking) and returns immediately, so a slow tap can never back-pressure
  the connection or the broker.  Its inbox is **bounded**; on overflow the
  *newest* message is dropped and a running dropped-count is logged (taps are
  best-effort, but silent loss is unacceptable).
* **inline** — the connection actor awaits the callbacks itself.  This is the
  Sprint 53 contract, retained as an internal option so the perf comparison
  (``bench/mqtt/tap_throughput.py``) stays reproducible.

A topic filter may carry ``{name}`` capture segments
(``'sensors/{room}/temperature'``); ``{name}`` matches one level like ``+`` and
binds it as a keyword argument to the callback, mirroring HTTP path params.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from ..actor import Actor, Message as ActorMessage
from .messages import topic_matches_filter

logger = logging.getLogger(__name__)

_DEFAULT_TAP_QUEUE = 1024


@dataclass(frozen=True)
class Message:
    """A published message handed to an ``on_message`` tap.

    The user-facing read-model — a plain, immutable view of one PUBLISH,
    deliberately distinct from the wire codec ``MQTTPublish`` (which carries the
    ``__iter__``/``__getitem__`` tuple-magic) and from the actor inbox
    ``Message`` base.  Named ``Message`` for ecosystem consistency (aiomqtt,
    paho): users write ``from blackbull.mqtt import Message``.
    """
    topic: str
    payload: bytes
    qos: int = 0
    retain: bool = False
    properties: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Tap:
    """A compiled ``on_message`` registration.

    ``match_filter`` is the topic filter with each ``{name}`` segment rewritten
    to ``+`` (so the validated :func:`topic_matches_filter` does the matching);
    ``captures`` records the ``(level_index, name)`` of each ``{name}`` segment
    for binding once a topic matches.
    """
    match_filter: str
    captures: tuple[tuple[int, str], ...]
    callback: Any

    def bind(self, topic: str) -> dict[str, str] | None:
        """Return captured ``{name: value}`` if *topic* matches, else ``None``."""
        if not topic_matches_filter(topic, self.match_filter):
            return None
        if not self.captures:
            return {}
        levels = topic.split('/')
        return {name: levels[i] for i, name in self.captures if i < len(levels)}

    @property
    def display_filter(self) -> str:
        """The topic filter as originally written, with ``{name}`` captures
        restored (``match_filter`` rewrites each to ``+`` for matching).

        ``'sensors/{room}/temperature'`` round-trips back to itself; a plain
        ``'sensors/+/temperature'`` stays as ``'sensors/+/temperature'``.  Used
        by documentation tooling (``AsyncAPIExtension``) that wants to show the
        filter the application declared rather than the internal match form.
        """
        if not self.captures:
            return self.match_filter
        levels = self.match_filter.split('/')
        for i, name in self.captures:
            if i < len(levels):
                levels[i] = '{' + name + '}'
        return '/'.join(levels)


def compile_tap(topic: str, callback: Any) -> Tap:
    """Compile a topic filter (possibly with ``{name}`` captures) into a :class:`Tap`."""
    captures = []
    out_levels = []
    for index, level in enumerate(topic.split('/')):
        if len(level) >= 2 and level[0] == '{' and level[-1] == '}':
            captures.append((index, level[1:-1]))
            out_levels.append('+')
        else:
            out_levels.append(level)
    return Tap(match_filter='/'.join(out_levels),
               captures=tuple(captures), callback=callback)


def compile_taps(handlers) -> list[Tap]:
    """Normalise a handler list to :class:`Tap` objects.

    Accepts already-compiled ``Tap`` objects or ``(topic, callback)`` pairs, so
    direct callers (tests, benchmarks) can keep the lightweight tuple form.
    """
    taps = []
    for handler in handlers or ():
        taps.append(handler if isinstance(handler, Tap)
                    else compile_tap(handler[0], handler[1]))
    return taps


async def run_taps(taps: list[Tap], message: Message) -> None:
    """Invoke every tap whose filter matches *message* (sequential, isolated).

    Captured ``{name}`` segments are passed as keyword arguments; a handler
    without captures is simply called ``callback(message)``.
    """
    for tap in taps:
        captures = tap.bind(message.topic)
        if captures is None:
            continue
        try:
            await tap.callback(message, **captures)
        except Exception:  # pragma: no cover - user handler isolation
            logger.exception('MQTT on_message handler for %r raised',
                             tap.match_filter)


@dataclass
class TapDeliver(ActorMessage):
    """Hand a published :class:`Message` to the :class:`TapActor`."""
    message: Message | None = field(default=None, compare=False, repr=False)


class TapActor(Actor):
    """Decoupled, lifespan-owned consumer of ``on_message`` taps.

    Producers call :meth:`offer` (non-blocking); a single consumer task drains
    the bounded inbox and runs the matching taps, so FIFO order of *accepted*
    messages is preserved and tap latency never reaches the connection or broker.
    """

    def __init__(self, handlers, *, queue_size: int = _DEFAULT_TAP_QUEUE) -> None:
        super().__init__(inbox_maxsize=queue_size)
        self._taps = compile_taps(handlers)
        self._dropped = 0

    def offer(self, message: Message) -> None:
        """Enqueue *message* without blocking; drop-newest on overflow.

        Taps are best-effort observability, so a full queue drops the incoming
        message rather than back-pressuring the publisher — but the running
        dropped count is always logged, since silent loss is the one
        unacceptable outcome.
        """
        try:
            self._inbox.put_nowait(TapDeliver(message=message))
        except asyncio.QueueFull:
            self._dropped += 1
            logger.warning(
                'MQTT tap queue full (size=%d); dropped newest message '
                '(%d dropped total)', self._inbox.maxsize, self._dropped)

    @property
    def dropped(self) -> int:
        """Number of messages dropped on overflow so far (best-effort metric)."""
        return self._dropped

    async def _handle(self, msg: ActorMessage) -> None:
        if isinstance(msg, TapDeliver):
            await run_taps(self._taps, msg.message)
        else:  # pragma: no cover - only TapDeliver is sent
            logger.debug('TapActor ignoring %s', type(msg).__name__)
