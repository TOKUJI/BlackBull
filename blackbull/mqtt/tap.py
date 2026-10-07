"""Best-effort application taps independent of broker routing.

Actor mode drops newest on bounded-inbox overflow; inline mode backpressures
only the publishing connection. Both share matching. A {name} topic segment
captures one level and supplies a keyword argument.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Iterable

from ..actor import Actor, Message as ActorMessage
from .messages import topic_matches_filter

logger = logging.getLogger(__name__)

_DEFAULT_TAP_QUEUE = 1024


@dataclass(frozen=True)
class Message:
    """Immutable published-message view for on_message taps.
    """
    topic: str
    payload: bytes
    qos: int = 0
    retain: bool = False
    properties: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Tap:
    """Compiled on_message filter with named captures.
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
        ``'sensors/+/temperature'`` stays as ``'sensors/+/temperature'``.
        """
        if not self.captures:
            return self.match_filter
        levels = self.match_filter.split('/')
        for i, name in self.captures:
            if i < len(levels):
                levels[i] = '{' + name + '}'
        return '/'.join(levels)


def compile_tap(topic: str, callback: Any) -> Tap:
    """Compile a topic filter (possibly with ``{name}`` captures) into a [`Tap`][]."""
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
    """Normalise a handler list to [`Tap`][] objects.

    Accepts already-compiled ``Tap`` objects or ``(topic, callback)`` pairs, so
    direct callers (tests, benchmarks) can keep the lightweight tuple form.
    """
    taps = []
    for handler in handlers or ():
        taps.append(handler if isinstance(handler, Tap)
                    else compile_tap(handler[0], handler[1]))
    return taps


async def run_taps(taps: Iterable[Tap], message: Message, *,
                   raise_exceptions: bool = False) -> None:
    """Invoke every tap whose filter matches *message* (sequential, isolated).

    Captured ``{name}`` segments are passed as keyword arguments; a handler
    without captures is simply called ``callback(message)``.

    A handler exception is logged and isolated by default — taps are
    best-effort observers, and one raising handler must not stop the others
    (nor the broker).  ``raise_exceptions=True`` propagates the first one
    instead, so a failing tap fails a test rather than a log line.
    """
    for tap in taps:
        captures = tap.bind(message.topic)
        if captures is None:
            continue
        try:
            await tap.callback(message, **captures)
        except Exception:
            if raise_exceptions:
                raise
            logger.exception('MQTT on_message handler for %r raised',
                             tap.match_filter)


@dataclass
class TapDeliver(ActorMessage):
    """Hand a published [`Message`][] to the [`TapActor`][]."""
    message: Message | None = field(default=None, compare=False, repr=False)


class TapActor(Actor):
    """Decoupled, lifespan-owned consumer of ``on_message`` taps.

    Producers call [`offer`][] (non-blocking); a single consumer task drains
    the bounded inbox and runs the matching taps, so FIFO order of *accepted*
    messages is preserved and tap latency never reaches the connection or broker.
    """

    def __init__(self, handlers, *, queue_size: int = _DEFAULT_TAP_QUEUE) -> None:
        super().__init__(inbox_maxsize=queue_size)
        # *handlers* is held by reference, not compiled once: MQTTExtension
        # constructs this actor in __init__ and ``@mqtt.on_message``
        # registrations append to the same list afterwards.  Compilation is
        # re-done in _current_taps whenever the list has grown, matching the
        # at-call-time semantics documented on iter_subscriptions.
        self._handlers = handlers if handlers is not None else []
        self._taps: list[Tap] = []
        self._compiled_count = -1
        self._dropped = 0

    def offer(self, message: Message) -> None:
        """Enqueue without blocking; drop newest on overflow and log the dropped count.
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

    def _current_taps(self) -> list[Tap]:
        if len(self._handlers) != self._compiled_count:
            self._taps = compile_taps(self._handlers)
            self._compiled_count = len(self._handlers)
        return self._taps

    async def _handle(self, msg: ActorMessage) -> None:
        if isinstance(msg, TapDeliver):
            await run_taps(self._current_taps(), msg.message)
        else:  # pragma: no cover - only TapDeliver is sent
            logger.debug('TapActor ignoring %s', type(msg).__name__)
