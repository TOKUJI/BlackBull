#!/usr/bin/env python3
"""Patch HttpArena to honour Sprint-34 env knobs.

Run on the EC2 instance after the HttpArena clone, before
`docker build` / `validate.sh` / `benchmark.sh`.  Reads the HttpArena
root from argv[1] (default: ~/HttpArena) and rewrites three files
in-place:

    scripts/lib/common.sh:65   — HARD_NOFILE assignment honours env.
    scripts/lib/framework.sh:71 — inject ``-e WEB_WORKERS=...`` into
                                  the framework container's args=( … ).
    scripts/benchmark.sh:124    — env-drive the loadgen --ulimit
                                  nofile in LOADGEN_DOCKER=true mode.

Each substitution asserts exactly-one match; missing matches abort
with a non-zero exit so the harness teardown fires before the
benchmark sweep wastes EC2 time.  Targets MDA2AV/HttpArena@master
as of 2026-06-09.
"""
from __future__ import annotations

import argparse
import os
import sys


PATCHES = [
    # (relpath, old_substring, new_substring)
    (
        'scripts/lib/common.sh',
        'HARD_NOFILE=$(ulimit -Hn 2>/dev/null || echo 1048576)',
        'HARD_NOFILE="${HARD_NOFILE:-$(ulimit -Hn 2>/dev/null || echo 1048576)}"',
    ),
    (
        'scripts/lib/framework.sh',
        '        --ulimit nofile="$HARD_NOFILE:$HARD_NOFILE"\n',
        '        --ulimit nofile="$HARD_NOFILE:$HARD_NOFILE"\n'
        '        ${WEB_WORKERS:+-e WEB_WORKERS="$WEB_WORKERS"}\n'
        '        ${BB_HTTPARENA_PORTS:+-e BB_HTTPARENA_PORTS="$BB_HTTPARENA_PORTS"}\n',
    ),
    (
        'scripts/benchmark.sh',
        '--ulimit nofile=1048576:1048576',
        '--ulimit nofile="${LOADGEN_NOFILE:-1048576}:${LOADGEN_NOFILE:-1048576}"',
    ),
]


def apply(root: str) -> None:
    for relpath, old, new in PATCHES:
        path = os.path.join(root, relpath)
        with open(path) as f:
            src = f.read()
        n = src.count(old)
        if n != 1:
            sys.stderr.write(
                f'FAIL: expected exactly 1 match of pattern in {relpath}, '
                f'found {n}\n'
            )
            sys.exit(1)
        with open(path, 'w') as f:
            f.write(src.replace(old, new))
        print(f'  patched {relpath}')

    print('  verification grep:')
    for relpath, _, _ in PATCHES:
        path = os.path.join(root, relpath)
        with open(path) as f:
            for i, line in enumerate(f, 1):
                if any(tok in line for tok in (
                    'HARD_NOFILE', 'WEB_WORKERS', 'LOADGEN_NOFILE',
                    'nofile=10485',
                )):
                    print(f'    {relpath}:{i}  {line.rstrip()}')


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        'root', nargs='?', default=os.path.expanduser('~/HttpArena'),
        help='HttpArena clone root (default: ~/HttpArena)',
    )
    apply(p.parse_args().root)
