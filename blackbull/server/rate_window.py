"""Rolling-window count meter.

Do not replace this with a token bucket: burst allowance would change the
meaning of caller limits. Use a separately named primitive for smoothing.
"""
from __future__ import annotations

import time


class RateWindow:
    """Count events per fixed window; report when the budget is spent.

    ``limit`` events are permitted per ``window`` seconds.  A ``limit`` of
    ``0`` disables the meter entirely — [`hit`][] then never reports an
    overrun, which is how every cap knob in this server spells "off".

    One instance per *thing being counted*, per connection: separate
    meters for PING and SETTINGS mean a peer may legitimately send its
    budget of each, and a shared meter would have made the two compete
    for one allowance for no reason a peer could predict.
    """

    __slots__ = ('limit', 'window', '_count', '_started_at')

    def __init__(self, limit: int, window: float = 1.0) -> None:
        self.limit = limit
        self.window = window
        self._count = 0
        self._started_at = 0.0

    @property
    def count(self) -> int:
        """Events counted in the window currently open (diagnostics only)."""
        return self._count

    def hit(self, now: float | None = None) -> bool:
        """Count one event.  Returns ``True`` when the budget is exceeded.

        *now* accepts an injected clock so a test can drive window
        rollover deterministically instead of sleeping through it.
        """
        if not self.limit:
            return False
        if now is None:
            now = time.monotonic()
        if now - self._started_at > self.window:
            self._count = 0
            self._started_at = now
        self._count += 1
        return self._count > self.limit

    def reset(self) -> None:
        """Forget the current window.  For connection reuse, not for callers
        that dislike the answer."""
        self._count = 0
        self._started_at = 0.0


class ByteRateFloor:
    """Enforce a rolling byte-rate floor after a grace period; rate=0 disables.

    waited includes every transport wait, even reads without countable payload.
    nbytes counts wanted payload, excluding padding or framing the peer can
    inflate. Satisfied windows roll so prior progress cannot cover later stalls.
    """

    __slots__ = ('rate', 'grace', '_waited', '_seen')

    def __init__(self, rate: float, grace: float) -> None:
        self.rate = rate
        self.grace = grace
        self._waited = 0.0
        self._seen = 0

    @property
    def observed(self) -> float:
        """Bytes per second in the window currently open (diagnostics only)."""
        return self._seen / self._waited if self._waited else 0.0

    def record(self, nbytes: int, waited: float) -> bool:
        """Add one delivery.  Returns ``True`` when the window came up short."""
        if not self.rate:
            return False
        self._waited += waited
        self._seen += nbytes
        if self._waited <= self.grace:
            return False
        if self._seen < self.rate * self._waited:
            return True
        # The window earned its keep: roll it forward, so the next judgement
        # looks at the next grace period only.
        self._waited = 0.0
        self._seen = 0
        return False

    def reset(self) -> None:
        """Forget the current window.  For reuse, not for callers that dislike
        the answer."""
        self._waited = 0.0
        self._seen = 0
