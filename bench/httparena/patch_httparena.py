#!/usr/bin/env python3
"""Patch HttpArena to honour BlackBull env knobs.

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
        # Unquoted on purpose: one of the two sites is inside a
        # double-quoted assignment, where an inner quote would close the
        # string.  The default has no whitespace, so it needs none.
        '--ulimit nofile=${LOADGEN_NOFILE:-1048576}:${LOADGEN_NOFILE:-1048576}',
        # Every load generator's docker command, not one of them: upstream now
        # builds a second (zrk, for latency-1m) with the same literal, and the
        # knob means "the loadgen's nofile is env-driven" wherever that is set.
        'all',
    ),
]


def apply(root: str) -> None:
    for patch in PATCHES:
        relpath, old, new = patch[:3]
        mode = patch[3] if len(patch) > 3 else 'one'
        path = os.path.join(root, relpath)
        with open(path) as f:
            src = f.read()
        n = src.count(old)
        # 'one' asserts the exact count so upstream drift stops the run rather
        # than being patched around; 'all' still needs at least one, and the
        # count is printed so a change upstream is visible in the log.
        if (mode == 'one' and n != 1) or (mode == 'all' and n < 1):
            expected = 'exactly 1' if mode == 'one' else 'at least 1'
            sys.stderr.write(
                f'FAIL: expected {expected} match of pattern in {relpath}, '
                f'found {n}\n'
            )
            sys.exit(1)
        with open(path, 'w') as f:
            f.write(src.replace(old, new))
        print(f'  patched {relpath} ({n} site{"s" if n != 1 else ""})')

    print('  verification grep:')
    for patch in PATCHES:
        relpath = patch[0]
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
