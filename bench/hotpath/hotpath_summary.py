#!/usr/bin/env python3
"""Summarise a hotpath_ab.sh run into RESULT.md.

Reports CPU microseconds per request as the primary figure.  Throughput
alone cannot be compared across phases (different core counts); CPU cost
per request can, and it is what "hot path" actually means.
"""
import collections
import pathlib
import re
import statistics
import sys

RPS = re.compile(r'^Requests/sec:\s*([0-9.]+)', re.M)
P99 = re.compile(r'^\s*99%\s+([0-9.]+)(us|ms|s)\b', re.M)
META = re.compile(
    r'^cpu_seconds=([0-9.]+) requests=(\d+) arm=(\S+) phase=(\S+) '
    r'path=(\S+) conns=(\d+)', re.M)
UNIT = {'us': 1e-3, 'ms': 1.0, 's': 1000.0}

LABEL = {
    'blackbull': 'BlackBull + uvloop',
    'fastapi': 'FastAPI / uvicorn + uvloop + httptools',
    'blackbull-mw': 'BlackBull + uvloop + HttpArena middleware',
    'fastapi-mw': 'FastAPI + HttpArena middleware',
}


def collect(raw: pathlib.Path):
    rows = collections.defaultdict(list)
    for f in sorted(raw.glob('wrk-*.txt')):
        text = f.read_text()
        m = META.search(text)
        r = RPS.search(text)
        if not m or not r:
            continue
        cpu_s, reqs, arm, phase, path, conns = m.groups()
        reqs, cpu_s = int(reqs), float(cpu_s)
        p = P99.search(text)
        rows[(phase, path, conns, arm)].append({
            'rps': float(r.group(1)),
            # CPU microseconds per request: the cost of the hot path itself,
            # independent of how many cores were thrown at it.
            'us_cpu': 1e6 * cpu_s / reqs if reqs else float('nan'),
            'p99_ms': float(p.group(1)) * UNIT[p.group(2)] if p else float('nan'),
        })
    return rows


def agg(vals):
    mean = statistics.fmean(vals)
    spread = (max(vals) - min(vals)) / mean * 100 if mean else 0.0
    return mean, spread


def table(rows, phase, title, note):
    keys = sorted(k for k in rows if k[0] == phase)
    if not keys:
        return ''
    out = [f'## {title}', '', note, '',
           '| endpoint | arm | runs | req/s | CPU µs/req | spread | p99 ms |',
           '|---|---|---:|---:|---:|---:|---:|']
    for phase_, path, conns, arm in keys:
        rs = rows[(phase_, path, conns, arm)]
        rps, rps_spread = agg([r['rps'] for r in rs])
        us, _ = agg([r['us_cpu'] for r in rs])
        p99, _ = agg([r['p99_ms'] for r in rs])
        out.append(f'| `{path}` | {LABEL.get(arm, arm)} | {len(rs)} | '
                   f'{rps:,.0f} | {us:.2f} | {rps_spread:.1f}% | {p99:.2f} |')
    out.append('')
    return '\n'.join(out)


def deltas(rows, phase, pairs):
    """A vs B, in CPU cost per request, with the noise floor beside it."""
    out = ['| endpoint | comparison | A µs/req | B µs/req | B is | worst spread | verdict |',
           '|---|---|---:|---:|---:|---:|---|']
    paths = sorted({(k[1], k[2]) for k in rows if k[0] == phase})
    for path, conns in paths:
        for a, b in pairs:
            ka, kb = (phase, path, conns, a), (phase, path, conns, b)
            if ka not in rows or kb not in rows:
                continue
            ua, sa = agg([r['us_cpu'] for r in rows[ka]])
            ub, sb = agg([r['us_cpu'] for r in rows[kb]])
            worst = max(sa, sb)
            pct = (ub - ua) / ua * 100
            verdict = 'outside noise' if abs(pct) > worst else 'inside noise'
            out.append(f'| `{path}` | {a} → {b} | {ua:.2f} | {ub:.2f} | '
                       f'{pct:+.1f}% | {worst:.1f}% | {verdict} |')
    return '\n'.join(out) + '\n'


TITLES = {
    'p1': ('Phase 1 — one worker on one core, 64 connections',
           'The core saturates, so `CPU µs/req` is the hot-path cost outright.'),
    'p3': ('Phase 3 — 3 workers on 3 physical cores, 512 connections',
           'The shape the leaderboard runs, scaled to this box.'),
    'p5': ('Phase 5 — route position (`/ping` first vs `/pingz` last)',
           'Byte-identical handlers; only their place in the route table differs.'),
    'p4h1': ('Phase 4a — 1 request header (wrk default)', ''),
    'p4h4': ('Phase 4b — 4 request headers', ''),
    'p4h8': ('Phase 4c — 8 request headers (browser-shaped)', ''),
}
PAIRS = [('blackbull', 'fastapi'), ('blackbull', 'blackbull-mw'),
         ('fastapi', 'fastapi-mw'), ('blackbull-mw', 'fastapi-mw')]


def main(d):
    d = pathlib.Path(d)
    rows = collect(d / 'raw')
    parts = ['# BlackBull vs FastAPI — where does a request cost its CPU?', '',
             (d / 'provenance.md').read_text() if (d / 'provenance.md').exists() else '',
             '']
    for phase in sorted({k[0] for k in rows}):
        title, note = TITLES.get(phase, (f'Phase `{phase}`', ''))
        parts.append(table(rows, phase, title, note))
        parts.append(deltas(rows, phase, PAIRS))
    (d / 'RESULT.md').write_text('\n'.join(parts))
    print((d / 'RESULT.md').read_text())


if __name__ == '__main__':
    main(sys.argv[1])
