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

Every emitted record carries a ``connection_id`` so SIEM /
log-aggregation pipelines can correlate first-hit, intermediate
summary, and graceful-close summary records that all belong to the
same connection — useful when ``peer`` is shared across many
clients behind a NAT.

The counter also runs two dirty-flush triggers so a connection
torn down by RST (or any abnormal path that skips graceful
:meth:`flush`) still emits a summary for any suppressed hits:

- **Threshold trigger**: after ``flush_threshold`` suppressed hits
  on any single cap, emit an intermediate summary and reset counts.
  Default 100; set to 0 to disable.
- **Interval trigger**: an asyncio timer task started on the first
  suppressed hit emits an intermediate summary after
  ``flush_interval`` seconds if any cap still has suppressed
  hits.  Default 60.0 s; set to 0 to disable.

Design recap in ``.claude/planning/candidates/cap-hit-logging.md``.
"""
import asyncio
import contextvars
import logging
import os
from typing import Any, Optional, Union

# Sub-hierarchy so operators can route / filter independently of the
# rest of ``blackbull.*``.  WARN level is the default emission level;
# operators may raise to ERROR (silence) or drop to INFO (show the
# rate-limit summaries that today fire at WARN too).
_logger = logging.getLogger('blackbull.caps')


__all__ = ('log_cap_hit', 'CapHitCounter')


def _gen_connection_id() -> str:
    """Cheap opaque per-connection id.

    4 random bytes hex-encoded is 8 hex characters — collision-resistant
    across the connections a single process holds open at once (~10^9
    collision probability at 65k concurrent IDs by the birthday bound),
    which is the only correlation window the cap-hit log needs.
    """
    return os.urandom(4).hex()


class CapHitCounter:
    """Per-connection state for cap-hit rate limiting.

    Construct one instance per connection (typically in
    :class:`~blackbull.server.connection_actor.ConnectionActor`),
    install it as the ambient counter for the duration of the
    connection via the :meth:`bind` context manager, and call
    :meth:`flush` once when the connection closes so a single
    summary record reports any suppressed hits.

    Two dirty-flush triggers fire intermediate summaries even when
    :meth:`flush` is never called (e.g. RST close that skips the
    graceful path):

    - ``flush_threshold`` (default 100) — after this many suppressed
      hits on any single cap, emit and reset.  Set to 0 to disable.
    - ``flush_interval`` (default 60.0 s) — an asyncio timer that
      emits and resets after this many seconds if any cap has
      suppressed hits.  Set to 0 to disable.
    """

    __slots__ = ('_suppressed', '_connection_id', '_flush_threshold',
                 '_flush_interval', '_timer_task')

    def __init__(
        self,
        *,
        connection_id: Optional[str] = None,
        flush_threshold: int = 100,
        flush_interval: Union[int, float] = 60.0,
    ) -> None:
        # Map from cap name -> count of suppressed (post-first) hits.
        # The first hit registers the cap with count 0; each
        # subsequent hit on the same cap increments by 1.
        self._suppressed: dict = {}
        # Opaque id so log aggregators can correlate first-hit /
        # intermediate / graceful summary records for the same
        # connection even when peer is shared (NAT / CGNAT).  Auto-
        # generated when not supplied.
        self._connection_id: str = (
            connection_id if connection_id is not None else _gen_connection_id()
        )
        self._flush_threshold: int = flush_threshold
        self._flush_interval: float = float(flush_interval)
        # asyncio timer task — lazily created when the first suppressed
        # hit lands, cancelled on every reset (threshold trigger,
        # timer fire, or graceful flush).
        self._timer_task: Optional[asyncio.Task] = None

    @property
    def connection_id(self) -> str:
        """Read-only access to the counter's connection id."""
        return self._connection_id

    def _first(self, cap: str) -> bool:
        """Return ``True`` iff *cap* has not been seen on this counter.

        Always updates internal state.  Suppression tally increments
        for every subsequent call.  After incrementing, dispatches to
        the dirty-flush triggers so a long-lived (or abnormally
        terminated) connection still gets aggregate visibility
        without :meth:`flush`.
        """
        if cap in self._suppressed:
            was_zero = self._suppressed[cap] == 0
            self._suppressed[cap] += 1
            self._maybe_dirty_flush()
            # If we did not just trigger the threshold (which clears
            # _suppressed[cap] back to 0), and this was the first
            # suppressed hit overall on this cap, arm the interval
            # timer.
            if self._suppressed.get(cap, 0) > 0 and was_zero:
                self._start_timer_if_needed()
            return False
        self._suppressed[cap] = 0
        return True

    def _maybe_dirty_flush(self) -> None:
        """Threshold-driven intermediate summary.

        If any cap's suppressed count has reached :attr:`_flush_threshold`,
        emit one summary per cap with non-zero counts, then reset all
        counts to 0 and cancel the interval timer (a fresh hit will
        re-arm it).
        """
        if self._flush_threshold <= 0:
            return
        if not any(c >= self._flush_threshold for c in self._suppressed.values()):
            return
        self._emit_intermediate_summary()
        # Reset counts to 0 — keep the keys so later hits on the same
        # cap stay on the "suppressed" path rather than re-logging
        # first-hit records.
        for cap in self._suppressed:
            self._suppressed[cap] = 0
        self._cancel_timer()

    def _start_timer_if_needed(self) -> None:
        """Lazily arm the interval-trigger timer task.

        No-op when the timer is disabled (``flush_interval <= 0``),
        when a task is already running, or when there is no current
        asyncio event loop (synchronous test contexts).
        """
        if self._flush_interval <= 0:
            return
        if self._timer_task is not None and not self._timer_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._timer_task = loop.create_task(self._timer_run())

    def _cancel_timer(self) -> None:
        if self._timer_task is not None and not self._timer_task.done():
            self._timer_task.cancel()
        self._timer_task = None

    async def _timer_run(self) -> None:
        """Sleep ``flush_interval`` then emit + reset if anything pending.

        Cancellation is the normal exit path (threshold trigger or
        :meth:`flush` will cancel us); swallow the
        :class:`asyncio.CancelledError` so it never propagates out of
        the background task.
        """
        try:
            await asyncio.sleep(self._flush_interval)
        except asyncio.CancelledError:
            return
        if any(c > 0 for c in self._suppressed.values()):
            self._emit_intermediate_summary()
            for cap in self._suppressed:
                self._suppressed[cap] = 0
        self._timer_task = None

    def _emit_summary_records(
        self,
        *,
        peer: Any,
        protocol: Optional[str],
        connection_id: Optional[str],
        message: str,
    ) -> None:
        """Common emission path shared by graceful flush and the dirty triggers.

        Iterates ``_suppressed`` (which the caller has not yet cleared);
        emits one record per cap with a non-zero count.
        """
        if not _logger.isEnabledFor(logging.WARNING):
            return
        cid = connection_id if connection_id is not None else self._connection_id
        for cap, suppressed in self._suppressed.items():
            if suppressed > 0:
                _logger.warning(
                    message, cap, suppressed,
                    extra={
                        'cap':           cap,
                        'suppressed':    suppressed,
                        'peer':          peer,
                        'protocol':      protocol,
                        'connection_id': cid,
                    },
                )

    def _emit_intermediate_summary(self) -> None:
        """Dirty-flush emission — does not clear state.

        The caller (:meth:`_maybe_dirty_flush` or :meth:`_timer_run`)
        resets the counts immediately afterwards, so this method just
        emits without touching state.
        """
        self._emit_summary_records(
            peer=None, protocol=None, connection_id=None,
            message='cap hit summary: %s suppressed=%d more (connection still open)',
        )

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
        connection_id: Optional[str] = None,
    ) -> None:
        """Emit one summary record per cap that had suppressed hits.

        Caps with zero suppressed hits (fired exactly once, or already
        emitted via an intermediate summary) are omitted.  Cancels the
        interval timer and clears all state — the counter is safe to
        reuse afterwards if the holder is pooled.
        """
        self._cancel_timer()
        self._emit_summary_records(
            peer=peer, protocol=protocol, connection_id=connection_id,
            message='cap hit summary: %s suppressed=%d more',
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
    connection_id: Optional[str] = None,
) -> None:
    """Emit one cap-hit record on ``blackbull.caps`` at WARN level.

    The structured fields land in ``record.extra`` (``cap``,
    ``requested``, ``limit``, ``peer``, ``scope_path``, ``protocol``,
    ``connection_id``) so log handlers can route or aggregate
    without parsing the message string.

    Resolution order for the rate-limit counter:

    1. The explicit *counter* keyword argument, if given.
    2. The active :class:`CapHitCounter` from the ambient
       :class:`~contextvars.ContextVar` (set via
       :meth:`CapHitCounter.bind`).
    3. ``None`` — every call emits, no rate limiting.

    ``connection_id`` resolution mirrors the counter chain:

    1. The explicit ``connection_id`` keyword argument, if given.
    2. The active counter's :attr:`CapHitCounter.connection_id`.
    3. ``None`` — record carries ``connection_id=None``.

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
    cid = connection_id
    if cid is None and active is not None:
        cid = active._connection_id
    _logger.warning(
        'cap hit: %s (requested=%s, limit=%s)',
        cap, requested, limit,
        extra={
            'cap':           cap,
            'requested':     requested,
            'limit':         limit,
            'peer':          peer,
            'scope_path':    scope_path,
            'protocol':      protocol,
            'connection_id': cid,
        },
    )
