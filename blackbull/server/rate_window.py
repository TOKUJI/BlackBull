"""A rolling-window rate meter — the codebase's shared defence primitive.

Several attack shapes are the same shape: a frame that is cheap for a peer
to send and obliges the server to do a small piece of work per frame.  A
PING costs an ACK write, a SETTINGS costs an ACK write, a zero-length
CONTINUATION costs a parse and a loop turn, a WebSocket PING costs a PONG.
None of them is large, so no byte budget sees them; each is unbounded in
*count*, which is the axis this meters.

Deliberately not a token bucket.  The constants its callers pass are
calibrated against observed traffic — the inbound RST_STREAM window
answering Rapid Reset (CVE-2023-44487) among them — and a burst-tolerant
refill curve would change what those limits mean.  A site that needs
smoothing gets a second primitive with its own name, not a quietly
different `hit()`.
"""
from __future__ import annotations

import time


class RateWindow:
    """Count events per fixed window; report when the budget is spent.

    ``limit`` events are permitted per ``window`` seconds.  A ``limit`` of
    ``0`` disables the meter entirely — :meth:`hit` then never reports an
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
    """Minimum sustained *byte* rate over a rolling window.

    :class:`RateWindow` counts events; this weighs octets against the time
    spent waiting for them, which is the other half of the same defence and a
    different question.  A rate, not a deadline, because a one-byte drip
    satisfies every per-read timeout ever set (Kestrel's
    ``MinRequestBodyDataRate`` is the same answer).

    The window is one grace period wide and rolls when it is satisfied, so a
    peer that ran ahead and then stalled is judged on the stall.  Nothing is
    judged before a grace period of waiting has accumulated.

    Both arguments to :meth:`record` are the caller's to define, and the
    difference between them is the whole design.  *waited* should be every
    second the caller sat on the transport, including reads that delivered
    nothing countable — otherwise a peer stalls before the parts that are not
    counted and buys unbounded time.  *nbytes* should be only the octets the
    caller actually wanted; framing a peer can pad at will is not payload.  A
    ``rate`` of ``0`` disables the floor, which is how every cap in this tree
    spells "off".
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
