"""Cap-hit observability.

Every user-tunable resource cap in BlackBull (header sizes, the four
timeouts, connection cap, WS frame cap, WS queue depth, H/2 stream
caps, compression in-flight) is silent by default when it rejects
traffic.  Operators see the consequence — a 503, a CLOSE 1009, a
dropped event — but no record naming the cap.  Regressions like the
1 MiB WS frame default that shipped in v0.35.0 (caught by the Sprint
43 conformance lane in v0.39.0) can hide across releases.

This module emits one ``WARNING``-level record per cap rejection on
the ``blackbull.caps`` logger so a deployment that subscribes to
that logger gets actionable visibility without grep-walking the
framework code.

Surface:

- :func:`log_cap_hit` — the single emission point.  Call from any
  cap-rejection site with the cap name, requested value, and limit.
- :class:`CapHitCounter` — per-connection state for the "first hit
  per cap logs in full; later hits silently counted; summary on
  :meth:`~CapHitCounter.flush`" rate-limit pattern.
- :meth:`CapHitCounter.bind` — context manager that installs the
  counter on a :class:`~contextvars.ContextVar`.  All
  :func:`log_cap_hit` calls inside the ``with`` block (including
  those in child tasks created via :class:`asyncio.TaskGroup`, which
  inherit context) automatically pick up the counter.  Zero
  plumbing through actor constructors.

Design recap in ``.claude/planning/candidates/cap-hit-logging.md``.
"""
import contextvars
import logging
from typing import Any, Optional, Union

# Sub-hierarchy so operators can route / filter independently of the
# rest of ``blackbull.*``.  WARN level is the default emission level;
# operators may raise to ERROR (silence) or drop to INFO (show the
# rate-limit summaries that today fire at WARN too).
_logger = logging.getLogger('blackbull.caps')


__all__ = ('log_cap_hit', 'CapHitCounter')


class CapHitCounter:
    """Per-connection state for cap-hit rate limiting.

    Construct one instance per connection (typically in
    :class:`~blackbull.server.connection_actor.ConnectionActor`),
    install it as the ambient counter for the duration of the
    connection via the :meth:`bind` context manager, and call
    :meth:`flush` once when the connection closes so a single
    summary record reports any suppressed hits.
    """

    __slots__ = ('_suppressed',)

    def __init__(self) -> None:
        # Map from cap name -> count of suppressed (post-first) hits.
        # The first hit registers the cap with count 0; each
        # subsequent hit on the same cap increments by 1.
        self._suppressed: dict = {}

    def _first(self, cap: str) -> bool:
        """Return ``True`` iff *cap* has not been seen on this counter.

        Always updates internal state — the suppression tally
        increments for every subsequent call.
        """
        if cap in self._suppressed:
            self._suppressed[cap] += 1
            return False
        self._suppressed[cap] = 0
        return True

    def bind(self) -> '_CapHitCounterScope':
        """Context manager that installs *self* as the ambient counter.

        Usage::

            counter = CapHitCounter()
            with counter.bind():
                ...                       # all log_cap_hit() in here uses *counter*
                # tasks created via asyncio.TaskGroup inherit it too
        """
        return _CapHitCounterScope(self)

    def flush(
        self,
        *,
        peer: Any = None,
        protocol: Optional[str] = None,
    ) -> None:
        """Emit one summary record per cap that had suppressed hits.

        Caps with zero suppressed hits (fired exactly once) are
        omitted — the first-hit record already named them.  Clears
        state after emission so the same counter can be re-used.
        """
        if not _logger.isEnabledFor(logging.WARNING):
            self._suppressed.clear()
            return
        for cap, suppressed in self._suppressed.items():
            if suppressed > 0:
                _logger.warning(
                    'cap hit summary: %s suppressed=%d more',
                    cap, suppressed,
                    extra={
                        'cap':        cap,
                        'suppressed': suppressed,
                        'peer':       peer,
                        'protocol':   protocol,
                    },
                )
        self._suppressed.clear()


class _CapHitCounterScope:
    """Context manager bound to a single :class:`CapHitCounter`."""

    __slots__ = ('_counter', '_token')

    def __init__(self, counter: CapHitCounter) -> None:
        self._counter = counter
        self._token = None

    def __enter__(self) -> CapHitCounter:
        self._token = _current_counter.set(self._counter)
        return self._counter

    def __exit__(self, *exc: Any) -> None:
        if self._token is not None:
            _current_counter.reset(self._token)
            self._token = None


# Per-task context for the active CapHitCounter.  asyncio's TaskGroup
# copies the parent's context into each spawned task, so a counter
# bound on the ConnectionActor task is visible to every protocol /
# stream / recipient task underneath it without any plumbing.
_current_counter: contextvars.ContextVar = contextvars.ContextVar(
    'blackbull_cap_log_counter', default=None,
)


def log_cap_hit(
    cap: str,
    requested: Union[int, float],
    limit: Union[int, float],
    *,
    counter: Optional[CapHitCounter] = None,
    peer: Any = None,
    scope_path: Optional[str] = None,
    protocol: Optional[str] = None,
) -> None:
    """Emit one cap-hit record on ``blackbull.caps`` at WARN level.

    The structured fields land in ``record.extra`` (``cap``,
    ``requested``, ``limit``, ``peer``, ``scope_path``, ``protocol``)
    so log handlers can route or aggregate without parsing the
    message string.

    Resolution order for the rate-limit counter:

    1. The explicit *counter* keyword argument, if given.
    2. The active :class:`CapHitCounter` from the ambient
       :class:`~contextvars.ContextVar` (set via
       :meth:`CapHitCounter.bind`).
    3. ``None`` — every call emits, no rate limiting.

    The first call per ``(counter, cap)`` emits a full record;
    subsequent calls return silently and increment the counter's
    suppression tally.  ``counter.flush()`` at connection close
    emits one summary per suppressed cap.
    """
    active = counter if counter is not None else _current_counter.get()
    if active is not None and not active._first(cap):
        return
    if not _logger.isEnabledFor(logging.WARNING):
        return
    _logger.warning(
        'cap hit: %s (requested=%s, limit=%s)',
        cap, requested, limit,
        extra={
            'cap':        cap,
            'requested':  requested,
            'limit':      limit,
            'peer':       peer,
            'scope_path': scope_path,
            'protocol':   protocol,
        },
    )
