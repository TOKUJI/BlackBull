"""Resource-cap refusal records on blackbull.caps.

Use log_cap_hit at enforcement sites. A bound CapHitCounter propagates to
child tasks through context; flush suppressed-hit summaries at teardown.
Threshold and interval flushes cover abnormal closes.
"""
import asyncio
import contextvars
import logging
from typing import Any, Optional, Union

# Refusals and suppressed-hit summaries use WARNING on blackbull.caps.
_logger = logging.getLogger('blackbull.caps')


__all__ = ('log_cap_hit', 'CapHitCounter')


def _gen_connection_id() -> str:
    """Return an opaque id from new_connection_id when accept supplied none.
    """
    from .conn_id import new_connection_id  # noqa: PLC0415
    return new_connection_id()


class CapHitCounter:
    """Per-connection state for cap-hit rate limiting.

    Construct one instance per connection (typically in
    [`ConnectionActor`][blackbull.server.connection_actor.ConnectionActor]),
    install it as the ambient counter for the duration of the
    connection via the [`bind`][] context manager, and call
    ``flush`` once when the connection closes so a single
    summary record reports any suppressed hits.

    Two dirty-flush triggers fire intermediate summaries even when
    ``flush`` is never called (e.g. RST close that skips the
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
        """Record a cap hit; return whether it is first, tally repeats and trigger summaries.
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
        """Emit and reset suppressed totals at the configured threshold.
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
        ``flush`` will cancel us); swallow the
        [`asyncio.CancelledError`][asyncio.CancelledError] so it never propagates out of
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
        """Emit nonzero suppressed totals without clearing them.
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
        """Emit without clearing counts; the caller owns reset.
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


class _LazyCapHitCounter:
    """Create the cap counter only on first use.

    Context children must share this holder so the first hit initializes their
    common counter and connection identity.
    """

    __slots__ = ('_counter', '_kwargs')

    def __init__(self, **kwargs: Any) -> None:
        self._counter: Optional[CapHitCounter] = None
        self._kwargs = kwargs

    def ensure(self) -> CapHitCounter:
        """Return the real counter, constructing it (and its id) on first call."""
        if self._counter is None:
            self._counter = CapHitCounter(**self._kwargs)
        return self._counter

    @property
    def counter(self) -> Optional[CapHitCounter]:
        """The materialised counter, or ``None`` if no cap has fired yet."""
        return self._counter

    def bind(self) -> '_CapHitCounterScope':
        """Install this holder as the ambient counter (mirrors ``CapHitCounter.bind``)."""
        return _CapHitCounterScope(self)

    def flush(self, **kwargs: Any) -> None:
        """Flush the real counter if it was ever materialised; otherwise a no-op."""
        if self._counter is not None:
            self._counter.flush(**kwargs)


class _CapHitCounterScope:
    """Context manager bound to a single [`CapHitCounter`][] or holder."""

    __slots__ = ('_counter', '_token')

    def __init__(self, counter: Union['CapHitCounter', '_LazyCapHitCounter']) -> None:
        self._counter = counter
        self._token = None

    def __enter__(self) -> Union['CapHitCounter', '_LazyCapHitCounter']:
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
    advice: Optional[str] = None,
) -> None:
    """Log a cap refusal with structured fields and operator advice.

    Explicit counter and connection_id override ambient context. Without a
    counter, every call emits. With one, first hits emit and repeats accumulate
    until flush; the connection owner must flush at teardown.
    """
    active = counter if counter is not None else _current_counter.get()
    # A lazily-bound holder materialises its real counter (and its
    # connection id) here — the first cap hit is the first moment the id is
    # actually needed.  Healthy connections never reach this branch.
    if type(active) is _LazyCapHitCounter:
        active = active.ensure()
    if active is not None and not active._first(cap):
        return
    if not _logger.isEnabledFor(logging.WARNING):
        return
    cid = connection_id
    if cid is None and active is not None:
        cid = active._connection_id
    extra = {
        'cap':           cap,
        'requested':     requested,
        'limit':         limit,
        'peer':          peer,
        'scope_path':    scope_path,
        'protocol':      protocol,
        'connection_id': cid,
    }
    # Handlers group by template: a call without advice keeps the original.
    if advice is None:
        _logger.warning('cap hit: %s (requested=%s, limit=%s)',
                        cap, requested, limit, extra=extra)
    else:
        _logger.warning('cap hit: %s (requested=%s, limit=%s): %s',
                        cap, requested, limit, advice, extra=extra)
