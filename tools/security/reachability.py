#!/usr/bin/env python3
"""Defense-site reachability for BlackBull (BLA-526 M5, G3-3).

The E1 method extracts defense sites from the ``blackbull/`` tree with
Python's AST — every ``raise``, every defense call (``log_cap_hit``,
``rst_stream``, ``goaway``, ``_connection_error``), and every reference
to a rejection status or an HTTP/2 error code — and excludes startup
configuration validation (``config.py``, ``env.py``, ``_env_vars.py``).
A site counts as reached when measured coverage executed its line.

Usage::

    coverage run --branch --source=blackbull tools/security/fixture_app.py ...
    coverage json -o coverage.json
    python tools/security/reachability.py --coverage-json coverage.json
"""
from __future__ import annotations

import argparse
import ast
import json
from dataclasses import dataclass
from pathlib import Path

#: Startup configuration validation is not a runtime defense site.
SKIP_FILES = frozenset({'config.py', 'env.py', '_env_vars.py'})

#: Probe-side and client libraries are not the server's defense surface.
SKIP_DIRS = frozenset({'client', 'fault_injection'})

#: Runtime defenses invoked to shed load or fail a connection.
DEFENSE_CALLS = frozenset({
    'log_cap_hit', 'rst_stream', 'goaway', '_connection_error',
})

#: Statuses used to refuse input (the rejection surface).
REJECTION_STATUSES = frozenset({
    'BAD_REQUEST', 'LENGTH_REQUIRED', 'REQUEST_HEADER_FIELDS_TOO_LARGE',
    'PAYLOAD_TOO_LARGE', 'URI_TOO_LONG', 'NOT_IMPLEMENTED',
    'METHOD_NOT_ALLOWED', 'HTTP_VERSION_NOT_SUPPORTED',
    'RANGE_NOT_SATISFIABLE', 'TOO_MANY_REQUESTS', 'MISDIRECTED_REQUEST',
    'PRECONDITION_FAILED',
})

#: HTTP/2 error codes used in GOAWAY/RST_STREAM.
ERROR_CODES = frozenset({
    'PROTOCOL_ERROR', 'ENHANCE_YOUR_CALM', 'FLOW_CONTROL_ERROR',
    'FRAME_SIZE_ERROR', 'SETTINGS_TIMEOUT', 'REFUSED_STREAM',
    'COMPRESSION_ERROR', 'CONNECT_ERROR', 'INADEQUATE_SECURITY',
})


@dataclass(frozen=True)
class Site:
    """One defense site: where, what kind, inside which function."""

    path: str
    line: int
    kind: str
    context: str


def _func_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ''


class _Visitor(ast.NodeVisitor):
    def __init__(self, rel: str, tree: ast.AST) -> None:
        self.rel = rel
        self.context = '<module>'
        self.sites: list[Site] = []
        # Coverage records the first line of a statement; an AST node on a
        # continuation line must report that same line or it never matches.
        self._parents: dict[ast.AST, ast.AST] = {}
        for parent in ast.walk(tree):
            for child in ast.iter_child_nodes(parent):
                self._parents[child] = parent

    def _stmt_line(self, node: ast.AST) -> int:
        current: ast.AST | None = node
        while current is not None and not isinstance(current, ast.stmt):
            current = self._parents.get(current)
        return current.lineno if current is not None else node.lineno

    def _add(self, node: ast.AST, kind: str, label: str) -> None:
        self.sites.append(Site(self.rel, self._stmt_line(node), kind, label))

    def visit_FunctionDef(self, node) -> None:
        outer, self.context = self.context, node.name
        self.generic_visit(node)
        self.context = outer

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Raise(self, node) -> None:
        self._add(node, 'raise', self.context)
        self.generic_visit(node)

    def visit_Call(self, node) -> None:
        name = _func_name(node.func)
        if name in DEFENSE_CALLS:
            self._add(node, 'defense-call', name)
        self.generic_visit(node)

    def visit_Attribute(self, node) -> None:
        if node.attr in REJECTION_STATUSES:
            self._add(node, 'rejection-status', node.attr)
        elif node.attr in ERROR_CODES:
            self._add(node, 'error-code', node.attr)
        self.generic_visit(node)


def defense_sites(root: Path) -> list[Site]:
    """All defense sites under *root*, deduplicated per (path, line, kind)."""
    seen: set[tuple[str, int, str]] = set()
    sites: list[Site] = []
    for path in sorted(root.rglob('*.py')):
        if path.name in SKIP_FILES:
            continue
        if SKIP_DIRS.intersection(path.relative_to(root).parts):
            continue
        rel = path.relative_to(root).as_posix()
        try:
            tree = ast.parse(path.read_text(encoding='utf-8'))
        except SyntaxError:
            continue
        visitor = _Visitor(rel, tree)
        visitor.visit(tree)
        for site in visitor.sites:
            key = (site.path, site.line, site.kind)
            if key not in seen:
                seen.add(key)
                sites.append(site)
    return sites


def reached_lines(coverage_json: dict, root: Path) -> set[tuple[str, int]]:
    """``(relative path, line)`` pairs the coverage run executed."""
    reached: set[tuple[str, int]] = set()
    for filename, data in coverage_json.get('files', {}).items():
        path = Path(filename)
        try:
            rel = path.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            rel = path.name
        for line in data.get('executed_lines', ()):
            reached.add((rel, line))
    return reached


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--coverage-json', required=True)
    parser.add_argument('--root', default='blackbull')
    parser.add_argument('--show-unreached', type=int, default=12)
    args = parser.parse_args(argv)

    root = Path(args.root)
    sites = defense_sites(root)
    coverage_json = json.loads(Path(args.coverage_json).read_text())
    reached = reached_lines(coverage_json, root)

    by_kind: dict[str, list[Site]] = {}
    for site in sites:
        by_kind.setdefault(site.kind, []).append(site)

    print('defense-site reachability (G3-3, E1 method)')
    print(f'root: {root}/  sites: {len(sites)}')
    total_hit = 0
    for kind in sorted(by_kind):
        group = by_kind[kind]
        hit = [s for s in group if (s.path, s.line) in reached]
        total_hit += len(hit)
        print(f'  {kind:<16} {len(hit):>4}/{len(group):<4} '
              f'({100 * len(hit) / len(group):5.1f}%)')
    print(f'  {"TOTAL":<16} {total_hit:>4}/{len(sites):<4} '
          f'({100 * total_hit / len(sites):5.1f}%)')

    summary = coverage_json.get('totals') or {}
    if summary:
        covered = summary.get('covered_lines', 0)
        statements = summary.get('num_statements', 0)
        branches = summary.get('num_branches', 0)
        covered_branches = summary.get('covered_branches', 0)
        print(f'lines: {covered}/{statements}'
              + (f'   branches: {covered_branches}/{branches}'
                 if statements and branches else ''))

    unreached = [s for s in sites if (s.path, s.line) not in reached]
    if unreached and args.show_unreached:
        print(f'unreached ({len(unreached)}), first {args.show_unreached}:')
        for site in unreached[:args.show_unreached]:
            print(f'  {site.path}:{site.line} {site.kind} ({site.context})')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
