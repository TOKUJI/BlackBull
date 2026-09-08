#!/usr/bin/env python3
"""Refuse source prose that dates a change, or points at a record nobody has.

``git log`` owns the timeline, and the tracker owns the deliberation.  A
comment that says *when* something changed stops being true the moment the
next change lands; a comment that says *which issue* changed it sends a reader
somewhere they cannot go.  Neither survives contact with the next reader, and
nothing in the build notices — which is how a module docstring came to state
three defaults the code had not shipped for several releases.

Two rule families, each with its own scope:

**Timeline vocabulary** — ``.py`` only, and only the spellings with a
*measured* zero false-positive rate.  That restraint is the point: a check
that fires on legitimate prose gets ``--no-verify``'d, and then it protects
nothing.  Measured over ``blackbull/**/*.py``:

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

**Private tracker ids** — the shipped package and every public document.
``BLA-<n>`` names an issue in a tracker the reader of a pure-Python library
cannot open, so in shipped source it is at best noise and at worst a promise:
"Closing that is BLA-325" is a TODO that goes stale the day BLA-325 lands, and
"the defect BLA-269 fixed" is the timeline again wearing an id.  State the
invariant, and cite the *test* that holds it — a test is a pointer every
reader can follow.  Agent-facing files (``AGENTS.md``, ``.claude/``) and the
test suite are exempt: the tracker is their subject, and neither ships.

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
from dataclasses import dataclass

#: Public documents, in which a private tracker id is a dangling reference.
#: Kept as a prefix list rather than "everything not exempt" so that adding a
#: document is a deliberate act, not an accident of where a file landed.
PUBLIC_DOCS = ('README.md', 'SECURITY.md', 'CHANGELOG.md',
               'KNOWN_LIMITATIONS.md', 'CONTRIBUTING.md', 'docs/')


def _is_shipped_source(path: str) -> bool:
    """In the wheel: ``blackbull*`` only (pyproject excludes tests, bench, …)."""
    return path.startswith('blackbull/') and path.endswith('.py')


def _is_public_doc(path: str) -> bool:
    return path.startswith(PUBLIC_DOCS)


def _is_timeline_scope(path: str) -> bool:
    """Any Python we ship or test with — but not a file whose job is history.

    ``CHANGELOG`` and ``docs/about/`` are records of what changed and when;
    refusing a date there would be refusing their content.  This file has to
    spell the refused vocabulary in order to refuse it.
    """
    if not path.endswith('.py'):
        return False
    return not re.search(r'(^|/)(conftest\.py$|check_comment_drift\.py$)', path)


@dataclass(frozen=True)
class Rule:
    pattern: re.Pattern[str]
    scope: object            # callable: path -> bool
    why: str


RULES: list[Rule] = [
    Rule(re.compile(r'\bSprint\s*\d+', re.I), _is_timeline_scope,
         'a sprint number dates the change; state the invariant it established'),
    Rule(re.compile(r'\bpre-\d+\.\d+'), _is_timeline_scope,
         'a version boundary dates the change; name the condition instead '
         '(e.g. "0 disables the cap", not "pre-0.29 behaviour")'),
    Rule(re.compile(r'\bas of version\b', re.I), _is_timeline_scope,
         'git log owns the version timeline'),
    Rule(re.compile(r'\b(?:Refactor|Review)\s+\d+\.\d+|\bbugs?\s+\d+\.\d+[a-z]?'),
         _is_timeline_scope,
         'an internal tracker id no reader outside the project can resolve'),
    Rule(re.compile(r'\bBLA-(?:A-)?\d+'),
         lambda p: _is_shipped_source(p) or _is_public_doc(p),
         'a private tracker id in shipped source or a public document.  State '
         'the invariant here and cite the test that holds it; the tracker is '
         'not a reference the reader can follow'),
]


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
        for rule in RULES:
            if not rule.scope(path):
                continue
            m = rule.pattern.search(text)
            if m:
                findings.append((path, lineno, m.group(0), rule.why, text.strip()))

    if not findings:
        return 0

    print('Prose that dates a change, or points where the reader cannot go:\n',
          file=sys.stderr)
    for path, lineno, hit, why, text in findings:
        print(f'  {path}:{lineno}: {hit!r} — {why}', file=sys.stderr)
        print(f'      {text[:100]}', file=sys.stderr)
    print(f'\n{len(findings)} to fix.  Keep the reason, drop the date and the '
          'id.  If a line here is\ngenuinely a deprecation contract or a cited '
          'measurement, rewrite it so the date\nis the evidence rather than the '
          'timeline.', file=sys.stderr)
    return 1


if __name__ == '__main__':
    sys.exit(main())
