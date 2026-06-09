"""Parse a BlackBull access log and compute latency percentiles for
a measurement window (skipping warmup).

Sprint 30 Tier 2 probe.  The access log format is:

    YYYY-MM-DDTHH:MM:SS.mmm {ip} "{method} {path} HTTP/{ver}" {status} {bytes} {ms}ms

Usage:
    python3 parse_access_log.py <log_path> <warmup_end_iso> <window_end_iso>

Outputs a markdown-friendly table of percentiles + sample count.
"""
from __future__ import annotations

import re
import sys
from datetime import datetime
from typing import Iterable


# Capture the timestamp prefix and the ``N.NNNms`` (or integer ``Nms``)
# duration suffix.  The sub-ms format lands via access_log_server.py's
# monkeypatch; the integer format is what the production access log
# emits.
_LINE = re.compile(
    r'^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}) '
    r'.+ (?P<duration>\d+(?:\.\d+)?)ms\s*$'
)


def _parse_window(path: str, t0: datetime, t1: datetime) -> list[float]:
    durations: list[float] = []
    with open(path, 'r', encoding='utf-8') as f:
        for line in f:
            m = _LINE.match(line.rstrip('\n'))
            if not m:
                continue
            ts = datetime.strptime(m.group('ts'), '%Y-%m-%dT%H:%M:%S.%f')
            if ts < t0 or ts > t1:
                continue
            durations.append(float(m.group('duration')))
    return durations


def _percentile(sorted_durations: list[float], p: float) -> float:
    """Linear-interpolated percentile, p ∈ [0, 100]."""
    if not sorted_durations:
        return float('nan')
    if p <= 0:
        return sorted_durations[0]
    if p >= 100:
        return sorted_durations[-1]
    idx = (len(sorted_durations) - 1) * (p / 100.0)
    lo, hi = int(idx), int(idx) + 1
    if hi >= len(sorted_durations):
        return sorted_durations[lo]
    frac = idx - lo
    return sorted_durations[lo] + (sorted_durations[hi] - sorted_durations[lo]) * frac


def report(durations: Iterable[int]) -> None:
    ds = sorted(durations)
    n = len(ds)
    if n == 0:
        print('No samples in window.')
        return
    avg = sum(ds) / n
    p50 = _percentile(ds, 50)
    p90 = _percentile(ds, 90)
    p99 = _percentile(ds, 99)
    p999 = _percentile(ds, 99.9)
    print(f'samples       : {n}')
    print(f'min  (ms)     : {ds[0]}')
    print(f'avg  (ms)     : {avg:.2f}')
    print(f'p50  (ms)     : {p50:.2f}')
    print(f'p90  (ms)     : {p90:.2f}')
    print(f'p99  (ms)     : {p99:.2f}')
    print(f'p99.9 (ms)    : {p999:.2f}')
    print(f'max  (ms)     : {ds[-1]}')


def main() -> None:
    if len(sys.argv) != 4:
        print(__doc__, file=sys.stderr)
        sys.exit(1)
    log_path = sys.argv[1]
    t0 = datetime.fromisoformat(sys.argv[2])
    t1 = datetime.fromisoformat(sys.argv[3])
    print(f'Window: {t0.isoformat()} → {t1.isoformat()}')
    report(_parse_window(log_path, t0, t1))


if __name__ == '__main__':
    main()
