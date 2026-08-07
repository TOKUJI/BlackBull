#!/usr/bin/env python3
"""Generate a 3-way HttpArena comparison table: two BlackBull result dirs plus
an optional peer (FastAPI) — every subscribed profile/conns cell, missing arms
left blank.

Usage:
  python3 bench/httparena/compare_3way.py <dir-v067-peer> <dir-pr223> [--out FILE]

Both dirs come from the SAME EC2 instance (same hardware), so cross-run deltas
are directly comparable.  rps is the best-of-3 value recorded in each result
JSON (what HttpArena's benchmark.sh saves).
"""
import argparse
import glob
import json
import sys

PROFILES = [
    'baseline', 'json', 'json-tls', 'static', 'baseline-h2', 'static-h2',
    'echo-ws', 'echo-ws-pipeline', 'pipelined', 'limited-conn', 'json-comp',
    'upload', 'crud', 'async-db', 'api-4', 'api-16', 'unary-grpc',
    'unary-grpc-tls', 'stream-grpc', 'stream-grpc-tls',
]


def cells(d: str, fw: str) -> dict[str, int]:
    """profile/conns -> best-of-3 rps for one framework in one result dir."""
    out = {}
    for f in glob.glob(f'{d}/httparena-tree/results/*/*/{fw}.json'):
        parts = f.replace(f'{d}/httparena-tree/results/', '').split('/')
        out[f'{parts[0]}/{parts[1]}'] = json.load(open(f)).get('rps', 0)
    return out


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('v067_dir', help='result dir containing v0.67.0 + fastapi')
    p.add_argument('pr_dir', help='result dir containing PR#223 (blackbull)')
    p.add_argument('--out', help='write the markdown table to this file')
    args = p.parse_args()

    v067 = cells(args.v067_dir, 'blackbull')
    pr = cells(args.pr_dir, 'blackbull')
    fa = cells(args.v067_dir, 'fastapi')

    lines = []
    lines.append('# HttpArena 3-way comparison — BlackBull v0.67.0 vs PR#223 vs FastAPI')
    lines.append('')
    lines.append(f'- source v0.67.0+fastapi: `{args.v067_dir}`')
    lines.append(f'- source PR#223:        `{args.pr_dir}`')
    lines.append('- same EC2 instance (m7a.8xlarge, 16 workers); rps = best-of-3.')
    lines.append('- blank = profile not subscribed / no result for that arm.')
    lines.append('')
    lines.append('| profile/conns | v0.67.0 | PR #223 | Δ% (PR#223 vs v0.67.0) | FastAPI | PR#223/fastapi |')
    lines.append('|---|---:|---:|---:|---:|---:|')

    # One row per profile/conns cell present in ANY arm, grouped by profile.
    all_cells = sorted(set(v067) | set(pr) | set(fa))
    for key in all_cells:
        av = v067.get(key)
        bv = pr.get(key)
        fv = fa.get(key)
        cell_delta = ''
        if av and bv:
            cell_delta = f'{(bv - av) / av * 100:+.1f}%'
        ratio = ''
        if bv and fv:
            ratio = f'{bv / fv:.2f}x'
        a_s = f'{av:,}' if av else ''
        b_s = f'{bv:,}' if bv else ''
        f_s = f'{fv:,}' if fv else ''
        lines.append(f'| {key} | {a_s} | {b_s} | {cell_delta} | {f_s} | {ratio} |')

    # Per-profile mean delta (PR#223 vs v0.67.0) over shared conns cells.
    lines.append('')
    lines.append('## Per-profile Δ% (mean of shared cells, PR#223 vs v0.67.0)')
    lines.append('')
    lines.append('| profile | cells | mean Δ% |')
    lines.append('|---|---:|---:|')
    for prof in PROFILES:
        ks = [c for c in all_cells if c.split('/')[0] == prof
              and v067.get(c) and pr.get(c)]
        if not ks:
            continue
        mean = sum((pr[c] - v067[c]) / v067[c] * 100 for c in ks) / len(ks)
        lines.append(f'| {prof} | {len(ks)} | {mean:+.1f}% |')

    table = '\n'.join(lines) + '\n'
    print(table)
    if args.out:
        with open(args.out, 'w') as f:
            f.write(table)
        print(f'written: {args.out}', file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main())
