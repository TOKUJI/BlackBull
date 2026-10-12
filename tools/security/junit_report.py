#!/usr/bin/env python3
"""Aggregate every BLA-526 security tool's verdicts into ONE JUnit XML file.

The judgement-producing tools (the probe, the catalogue run, the mutation
run) map their rows onto a common case schema and merge them into one
results file — default ``bench/results/security/results.xml`` — so one
glance (or one CI upload) shows every check, catalogue scenario and
mutation across runs.  Merging replaces whole suites by name: rerunning
one tool refreshes its own suite and leaves the others untouched.

Verdict mapping (JUnit has no known-failure state):

- ``PASS`` / ``FIXED`` -> ``<testcase>`` (FIXED's issue link rides in the
  message body as ``system-out``)
- ``FAIL`` / ``TIMEOUT`` -> ``<failure message=... type=verdict>``
- ``SKIP`` -> ``<skipped/>``
- ``KNOWN`` -> ``<skipped message="known: …">`` — a filed, open defect is
  visible but does not fail the consuming CI
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import xml.etree.ElementTree as ET

DEFAULT_JUNIT_PATH = Path('bench/results/security/results.xml')

_FAILURES = frozenset({'FAIL', 'TIMEOUT'})


@dataclass(frozen=True)
class Case:
    """One judgement row in the common schema."""

    suite: str
    classname: str
    name: str
    verdict: str
    detail: str = ''
    time_s: float = 0.0


def _case_element(case: Case) -> ET.Element:
    el = ET.Element('testcase', {
        'classname': case.classname,
        'name': case.name,
        'time': f'{case.time_s:.3f}',
    })
    if case.verdict in _FAILURES:
        child = ET.SubElement(el, 'failure', {
            'message': case.detail or case.verdict,
            'type': case.verdict,
        })
        child.text = case.detail
    elif case.verdict == 'KNOWN':
        ET.SubElement(el, 'skipped', {'message': f'known: {case.detail}'})
    elif case.verdict == 'SKIP':
        ET.SubElement(el, 'skipped', {'message': case.detail or 'SKIP'})
    elif case.verdict == 'FIXED':
        out = ET.SubElement(el, 'system-out')
        out.text = f'fixed: {case.detail}'
    elif case.detail:
        out = ET.SubElement(el, 'system-out')
        out.text = case.detail
    return el


def _suite_element(name: str, cases: list[Case]) -> ET.Element:
    failures = sum(1 for c in cases if c.verdict in _FAILURES)
    skipped = sum(1 for c in cases if c.verdict in ('SKIP', 'KNOWN'))
    suite = ET.Element('testsuite', {
        'name': name,
        'tests': str(len(cases)),
        'failures': str(failures),
        'skipped': str(skipped),
        'errors': '0',
        'time': f'{sum(c.time_s for c in cases):.3f}',
    })
    for case in cases:
        suite.append(_case_element(case))
    return suite


def write_junit(path: Path, cases: list[Case]) -> Path:
    """Merge *cases* into the JUnit XML at *path*, replacing whole suites.

    Suites named in *cases* are rewritten from those cases; suites already
    in the file but not named are kept verbatim.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fresh: dict[str, list[Case]] = {}
    for case in cases:
        fresh.setdefault(case.suite, []).append(case)

    root = ET.Element('testsuites', {'name': 'bla-526'})
    if path.exists():
        try:
            old = ET.parse(path).getroot()
        except ET.ParseError:
            old = None
        if old is not None:
            for suite in old.findall('testsuite'):
                if suite.get('name') not in fresh:
                    root.append(suite)
    for name in sorted(fresh):
        root.append(_suite_element(name, fresh[name]))
    ET.ElementTree(root).write(path, encoding='unicode', xml_declaration=True)
    return path
