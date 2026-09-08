#!/usr/bin/env python3
"""How much of this package is prose, and where is it densest.

Comment drift has two causes and they need different answers.  Prose that was
true and stopped being true is caught, crudely, by tools.  Prose that never
kept up with the code it sits beside is not a detection problem at all: it is
a *volume* problem.  Every line of prose is a line that can go stale, so the
surface area is the thing to manage, and a surface nobody measures only grows.

Measured on this package, it has only grown:

======== ======= ======= ======= ==============
release    prose    code   ratio    prose share
======== ======= ======= ======= ==============
v0.31.0     4800    8543   0.56x          36.0%
v0.44.0     7468   12634   0.59x          37.2%
v0.51.0     9780   14179   0.69x          40.8%
v0.73.1    13813   17896   0.77x          43.6%
v0.80.0    17012   20676   0.82x          45.1%
======== ======= ======= ======= ==============

Code grew 2.65x over that span and prose grew 3.88x — prose outgrew code by
46%.  Nearly half of every non-blank line shipped is now prose.

This script is the number that makes "reduce the comments" a claim someone can
check.  It counts, it does not judge: a docstring is not waste, and a package
with no prose would be worse.  What it gives you is the trend and the outliers,
so a reduction pass can be evaluated on something other than the taste of
whoever ran it.

Usage::

    prose_census.py                    # the shipped package
    prose_census.py --top 30           # more outliers
    prose_census.py --rev v0.73.1      # any git revision
    prose_census.py --totals-only      # one line, for a gate
    prose_census.py PATH [PATH ...]    # arbitrary trees
"""
from __future__ import annotations

import argparse
import os
import pathlib
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _pysource import comment_lines, docstring_lines  # noqa: E402


class Counts:
    """Line counts for one file, in four disjoint buckets that must sum."""

    __slots__ = ('comment', 'docstring', 'code', 'blank')

    def __init__(self, comment=0, docstring=0, code=0, blank=0):
        self.comment, self.docstring = comment, docstring
        self.code, self.blank = code, blank

    @property
    def prose(self) -> int:
        return self.comment + self.docstring

    @property
    def total(self) -> int:
        return self.prose + self.code + self.blank

    def __iadd__(self, other: 'Counts') -> 'Counts':
        self.comment += other.comment
        self.docstring += other.docstring
        self.code += other.code
        self.blank += other.blank
        return self


def count(src: str) -> Counts:
    """Classify every line of *src*, disjointly.

    A line carrying both code and a trailing comment counts as a comment: the
    question this answers is how much prose there is to keep true, and a
    trailing comment is prose.
    """
    lines = src.splitlines()
    com = comment_lines(src)
    doc = docstring_lines(src) - com
    blank = sum(1 for i, line in enumerate(lines, 1)
                if not line.strip() and i not in com and i not in doc)
    return Counts(len(com), len(doc), len(lines) - len(com) - len(doc) - blank,
                  blank)


def survey(roots: list[str]) -> list[tuple[str, Counts]]:
    out = []
    for root in roots:
        base = pathlib.Path(root)
        paths = sorted(base.rglob('*.py')) if base.is_dir() else [base]
        for p in paths:
            try:
                out.append((str(p), count(p.read_text(encoding='utf-8'))))
            except (OSError, UnicodeDecodeError):
                continue
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('paths', nargs='*', default=None)
    ap.add_argument('--rev', help='survey a git revision instead of the tree')
    ap.add_argument('--top', type=int, default=20)
    ap.add_argument('--totals-only', action='store_true')
    ap.add_argument('--min-code', type=int, default=20,
                    help='ignore files below this many code lines when ranking')
    ns = ap.parse_args()

    if ns.rev:
        with tempfile.TemporaryDirectory() as td:
            r = subprocess.run(f'git archive {ns.rev} blackbull | tar -x -C {td}',
                               shell=True, capture_output=True)
            if r.returncode != 0:
                print(f'no blackbull/ at {ns.rev}', file=sys.stderr)
                return 1
            rows = survey([td])
            rows = [(p.replace(td + '/', ''), c) for p, c in rows]
    else:
        rows = survey(ns.paths or ['blackbull'])

    total = Counts()
    for _, c in rows:
        total += c
    assert total.total == sum(c.total for _, c in rows), 'line accounting'

    if not rows or total.code == 0:
        print('nothing to count', file=sys.stderr)
        return 1

    if ns.totals_only:
        print(f'{total.prose} {total.code} {total.prose / total.code:.3f}')
        return 0

    ranked = sorted((r for r in rows if r[1].code >= ns.min_code),
                    key=lambda r: -r[1].prose / r[1].code)
    print(f'{"file":54} {"cmt":>5} {"doc":>5} {"code":>5} {"prose/code":>11}')
    print('-' * 86)
    for path, c in ranked[:ns.top]:
        print(f'{path:54} {c.comment:5} {c.docstring:5} {c.code:5} '
              f'{c.prose / c.code:10.2f}x')

    over = [r for r in rows if r[1].prose > r[1].code]
    print()
    print(f'files                                : {len(rows)}')
    print(f'  where prose exceeds code           : {len(over)}')
    print(f'comment lines                        : {total.comment}')
    print(f'docstring lines                      : {total.docstring}')
    print(f'prose                                : {total.prose}')
    print(f'code                                 : {total.code}')
    print(f'blank                                : {total.blank}')
    print(f'total                                : {total.total}')
    print(f'prose : code                         = {total.prose / total.code:.2f} : 1')
    print(f'prose share of non-blank lines       = '
          f'{100 * total.prose / (total.prose + total.code):.1f}%')
    return 0


if __name__ == '__main__':
    sys.exit(main())
