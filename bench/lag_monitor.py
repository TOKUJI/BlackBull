"""Event loop lag monitor for BlackBull benchmarking.

Usage in a BlackBull app::

    from bench.lag_monitor import LoopLagMonitor

    monitor = LoopLagMonitor()

    @app.on_startup
    async def _start():
        monitor.start()

    @app.on_shutdown
    async def _stop():
        monitor.stop()

    @app.route('/metrics')
    async def metrics():
        import json
        return json.dumps(monitor.snapshot()).encode()
"""
import asyncio
import time


class LoopLagMonitor:
    """Measures asyncio event loop lag by sampling sleep overshoot.

    Schedules ``asyncio.sleep(interval)`` repeatedly and measures how long
    the actual wakeup takes.  The excess is the time the event loop was
    blocked processing other callbacks — i.e. the lag.

    Samples are kept in a fixed-size ring buffer (newest overwrites oldest).
    ``snapshot()`` returns percentile statistics in milliseconds.
    """

    def __init__(self, interval: float = 0.05, window: int = 200) -> None:
        self._interval = interval
        self._window = window
        self._samples: list[float] = []
        self._task: asyncio.Task | None = None

    async def _run(self) -> None:
        while True:
            t0 = time.monotonic()
            await asyncio.sleep(self._interval)
            lag = time.monotonic() - t0 - self._interval
            self._samples.append(lag * 1000.0)  # store as ms
            if len(self._samples) > self._window:
                del self._samples[0]

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None

    def snapshot(self) -> dict:
        """Return lag statistics in milliseconds."""
        if not self._samples:
            return {'p50': 0.0, 'p95': 0.0, 'p99': 0.0, 'max': 0.0,
                    'samples': 0, 'interval_ms': self._interval * 1000}
        s = sorted(self._samples)
        n = len(s)

        def _pct(p: float) -> float:
            idx = min(int(n * p), n - 1)
            return round(s[idx], 3)

        return {
            'p50': _pct(0.50),
            'p95': _pct(0.95),
            'p99': _pct(0.99),
            'max': round(max(s), 3),
            'samples': n,
            'interval_ms': self._interval * 1000,
        }
