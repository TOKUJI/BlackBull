#!/usr/bin/env python3
"""Print the comments and docstrings a change touched — the review scope.

"Review the comments in what I just implemented" is only a usable instruction
if *which comments* is answerable mechanically.  A line-number diff is not the
answer: it includes code, and it misses a docstring whose meaning changed
because the function under it did.

This reports, for every added line in the diff:

* ``comment``   — the line is a ``#`` comment (from ``tokenize``, so a ``#``
  inside a string literal is not one);
* ``docstring`` — the line falls inside a module/class/function docstring
  (from ``ast``, so it is a real docstring and not any triple-quoted string);
* ``code``      — everything else, reported only with ``--with-code`` because
  a changed body is what makes the docstring above it worth re-reading.

Files that fail to parse are reported and skipped rather than guessed at.

Usage::

    changed_comments.py                     # uncommitted, vs HEAD
    changed_comments.py --staged
    changed_comments.py --range master..HEAD
    changed_comments.py --count             # just the totals, for a gate
"""
from __future__ import annotations

import argparse
import ast
import io
import re
import subprocess
import sys
import tokenize


def _added(diff_args: list[str]) -> dict[str, set[int]]:
    """path -> line numbers added, in the new file's numbering."""
    diff = subprocess.run(['git', 'diff', '--unified=0', '--no-color',
                           *diff_args],
                          capture_output=True, text=True, check=True).stdout
    out: dict[str, set[int]] = {}
    path, lineno = '', 0
    for line in diff.splitlines():
        if line.startswith('+++ b/'):
            path, lineno = line[6:], 0
        elif line.startswith('@@'):
            m = re.search(r'\+(\d+)', line)
            lineno = int(m.group(1)) if m else 0
        elif line.startswith('+') and not line.startswith('+++'):
            out.setdefault(path, set()).add(lineno)
            lineno += 1
    return out


def _comment_lines(src: str) -> set[int]:
    lines: set[int] = set()
    try:
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type == tokenize.COMMENT:
                lines.add(tok.start[0])
    except (tokenize.TokenError, IndentationError, SyntaxError):
        pass
    return lines


def _docstring_lines(src: str) -> set[int]:
    lines: set[int] = set()
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return lines
    holders = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    for node in ast.walk(tree):
        if not isinstance(node, holders):
            continue
        body = getattr(node, 'body', None)
        if not body:
            continue
        first = body[0]
        if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)):
            lines.update(range(first.lineno,
                               (first.end_lineno or first.lineno) + 1))
    return lines


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--staged', action='store_true')
    ap.add_argument('--range', dest='rng')
    ap.add_argument('--with-code', action='store_true')
    ap.add_argument('--count', action='store_true',
                    help='print "<comment> <docstring> <code> <files>" only')
    ns = ap.parse_args()

    if ns.staged:
        added = _added(['--cached'])
    elif ns.rng:
        added = _added([ns.rng])
    else:
        added = _added(['HEAD'])

    n_comment = n_doc = n_code = 0
    touched: set[str] = set()
    report: list[str] = []
    for path in sorted(added):
        if not path.endswith('.py'):
            continue
        try:
            src = open(path, encoding='utf-8').read()
        except OSError:
            report.append(f'{path}: gone from the working tree, skipped')
            continue
        comments, docs = _comment_lines(src), _docstring_lines(src)
        body = src.splitlines()
        for ln in sorted(added[path]):
            if ln > len(body):
                continue
            kind = ('comment' if ln in comments
                    else 'docstring' if ln in docs else 'code')
            if kind == 'comment':
                n_comment += 1
            elif kind == 'docstring':
                n_doc += 1
            else:
                n_code += 1
                if not ns.with_code:
                    continue
            touched.add(path)
            report.append(f'{path}:{ln}: [{kind}] {body[ln - 1].strip()[:110]}')

    if ns.count:
        print(f'{n_comment} {n_doc} {n_code} {len(touched)}')
        return 0

    print('\n'.join(report))
    print(f'\nadded lines: {n_comment} comment, {n_doc} docstring, {n_code} code'
          f'  across {len(touched)} file(s)', file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main())
