"""The shared rate meter's own contract.

Every site that uses :class:`RateWindow` inherits whatever this class
gets wrong, so its boundaries are asserted here once rather than
re-litigated in each site's tests.  The clock is injected throughout: a
test that sleeps through a window is a test that is slow and flaky in
exchange for asserting nothing extra.
"""
import pytest

from blackbull.server.rate_window import RateWindow


class TestBudget:
    def test_the_limit_is_inclusive(self):
        """*limit* events are allowed; the overrun is the one after."""
        w = RateWindow(limit=3, window=1.0)
        assert [w.hit(now=0.0) for _ in range(3)] == [False, False, False]
        assert w.hit(now=0.0) is True

    def test_zero_disables_the_meter(self):
        w = RateWindow(limit=0, window=1.0)
        assert not any(w.hit(now=0.0) for _ in range(1000))

    def test_a_limit_of_one_permits_exactly_one(self):
        w = RateWindow(limit=1, window=1.0)
        assert w.hit(now=0.0) is False
        assert w.hit(now=0.0) is True


class TestWindowRollover:
    def test_a_new_window_forgives_the_previous_one(self):
        w = RateWindow(limit=2, window=1.0)
        assert w.hit(now=0.0) is False
        assert w.hit(now=0.0) is False
        assert w.hit(now=0.0) is True

        # Past the window: the peer gets its budget back rather than
        # staying condemned by a burst it has since stopped.
        assert w.hit(now=1.5) is False

    def test_the_window_edge_does_not_forgive_early(self):
        """Exactly at the boundary is still the same window.

        The comparison is ``>``, so a peer cannot reset its budget by
        pacing precisely to the window length — an off-by-one here would
        double every limit in the server for anyone who noticed.
        """
        w = RateWindow(limit=1, window=1.0)
        assert w.hit(now=0.0) is False
        assert w.hit(now=1.0) is True
        assert w.hit(now=1.01) is False

    def test_a_sustained_rate_under_the_limit_is_never_flagged(self):
        """The false-positive case: half the budget, forever.

        This is the property that decides whether a default is safe to
        ship — a meter that eventually trips on a well-behaved peer is
        worse than no meter, because it fails a connection nobody can
        diagnose.
        """
        w = RateWindow(limit=20, window=1.0)
        now = 0.0
        for _ in range(10_000):
            now += 0.1          # 10 per second against a budget of 20
            assert w.hit(now=now) is False


class TestIntrospection:
    def test_count_reports_the_open_window(self):
        w = RateWindow(limit=5, window=1.0)
        for _ in range(3):
            w.hit(now=0.0)
        assert w.count == 3

    def test_reset_forgets_the_window(self):
        w = RateWindow(limit=1, window=1.0)
        w.hit(now=0.0)
        w.reset()
        assert w.count == 0
        assert w.hit(now=0.0) is False

    def test_the_default_clock_is_monotonic(self, monkeypatch):
        """Called without *now*, it must read a clock that cannot go backwards."""
        ticks = iter([100.0, 100.0, 100.0])
        monkeypatch.setattr('blackbull.server.rate_window.time.monotonic',
                            lambda: next(ticks))
        w = RateWindow(limit=2, window=1.0)
        assert w.hit() is False
        assert w.hit() is False
        assert w.hit() is True
