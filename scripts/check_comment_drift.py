#!/usr/bin/env python3
"""Refuse source comments that date a change instead of explaining one.

``git log`` owns the timeline.  A comment that says *when* something changed
stops being true the moment the next change lands, and nothing in the build
notices — which is how a docstring came to state three defaults the code had
not shipped for several releases.

Only the vocabulary with a **measured zero false-positive rate** is refused.
That restraint is the point: a check that fires on legitimate prose gets
``--no-verify``'d, and then it protects nothing.  Measured over
``blackbull/**/*.py``:

===================  =====  ==================================================
pattern              hits   verdict
===================  =====  ==================================================
``Sprint <n>``          18  all genuine history          -> refused
``pre-<n>.<n>``          2  all genuine history          -> refused
``as of version``        0  none, but the same shape     -> refused
``still``              251  almost all present-tense     -> not checked
``used to``             28  mostly purpose ("used to     -> not checked
                            validate"), not history
``no longer``           20  present-tense state          -> not checked
``legacy``              18  names a live opt-out mode    -> not checked
===================  =====  ==================================================

The four unchecked classes need a reader, not a regex.  They are what the
comment review at the end of an implementation is for.

Usage::

    check_comment_drift.py --staged        # added lines in the staged diff
    check_comment_drift.py --range A..B    # added lines in a commit range
    check_comment_drift.py PATH [PATH...]  # whole files
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys

#: Each entry is (compiled pattern, what to write instead).  A pattern earns
#: its place by having no legitimate use in a source comment, not by being
#: suggestive — see the table above.
RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r'\bSprint\s*\d+', re.I),
     'a sprint number dates the change; state the invariant it established'),
    (re.compile(r'\bpre-\d+\.\d+'),
     'a version boundary dates the change; name the condition instead '
     '(e.g. "0 disables the cap", not "pre-0.29 behaviour")'),
    (re.compile(r'\bas of version\b', re.I),
     'git log owns the version timeline'),
    (re.compile(r'\b(?:Refactor|Review)\s+\d+\.\d+|\bbugs?\s+\d+\.\d+[a-z]?'),
     'an internal tracker id no reader outside the project can resolve'),
]

#: Files whose whole job is to record history — plus this one, which has to
#: spell the refused vocabulary in order to refuse it.
EXEMPT = re.compile(r'(^|/)(CHANGELOG|docs/about/|tests?/.*conftest'
                    r'|scripts/check_comment_drift\.py$)')


def _added_lines(args: list[str]) -> list[tuple[str, int, str]]:
    """(path, line number in the new file, text) for every added line."""
    diff = subprocess.run(['git', 'diff', '--unified=0', '--no-color', *args],
                          capture_output=True, text=True, check=True).stdout
    out: list[tuple[str, int, str]] = []
    path, lineno = '', 0
    for line in diff.splitlines():
        if line.startswith('+++ b/'):
            path, lineno = line[6:], 0
        elif line.startswith('@@'):
            m = re.search(r'\+(\d+)', line)
            lineno = int(m.group(1)) if m else 0
        elif line.startswith('+') and not line.startswith('+++'):
            out.append((path, lineno, line[1:]))
            lineno += 1
    return out


def _whole_files(paths: list[str]) -> list[tuple[str, int, str]]:
    out: list[tuple[str, int, str]] = []
    for p in paths:
        try:
            for i, line in enumerate(open(p, encoding='utf-8'), 1):
                out.append((p, i, line.rstrip('\n')))
        except (OSError, UnicodeDecodeError):
            continue
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--staged', action='store_true')
    ap.add_argument('--range', dest='rng')
    ap.add_argument('paths', nargs='*')
    ns = ap.parse_args()

    if ns.staged:
        lines = _added_lines(['--cached'])
    elif ns.rng:
        lines = _added_lines([ns.rng])
    elif ns.paths:
        lines = _whole_files(ns.paths)
    else:
        ap.error('one of --staged, --range, or PATH is required')

    findings = []
    for path, lineno, text in lines:
        if not path.endswith('.py') or EXEMPT.search(path):
            continue
        for pattern, why in RULES:
            m = pattern.search(text)
            if m:
                findings.append((path, lineno, m.group(0), why, text.strip()))

    if not findings:
        return 0

    print('Comments that date a change rather than explain one:\n',
          file=sys.stderr)
    for path, lineno, hit, why, text in findings:
        print(f'  {path}:{lineno}: {hit!r} — {why}', file=sys.stderr)
        print(f'      {text[:100]}', file=sys.stderr)
    print(f'\n{len(findings)} to fix.  Keep the reason, drop the date.  If a '
          'line here is genuinely\na deprecation contract or a cited '
          'measurement, say so in the comment and\nrewrite it so the date is '
          'the evidence, not the timeline.', file=sys.stderr)
    return 1


if __name__ == '__main__':
    sys.exit(main())
