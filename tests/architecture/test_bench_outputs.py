"""Every bench tool writes to one place: ``bench/results/<tool>/<run>/``.

Tools take their run directory from ``bench/lib/results`` (shell or Python),
keep temporary files under that run's ``tmp/``, and never touch ``/tmp``.
Results are evidence for an issue, not code: they are attached to YouTrack and
never committed.  bench/ holds no loose scripts — every tool sits in a folder
named for its purpose.

``PENDING_COMMITTED`` lists what is not yet moved, with the issue that moves
it, and ``CONTAINER_TMP`` the files whose ``/tmp`` is inside a container.  Both
only shrink: a new violator fails, and so does an entry that no longer matches.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

#: Files whose /tmp is inside a container, not on the host.
CONTAINER_TMP = {
    'bench/conformance/autobahn_heavy.sh',  # the CaseSet resolver in the tester image
}

#: Committed results not yet moved to YouTrack (BLA-543).
PENDING_COMMITTED = {
    'bench/results/v0811-bla341-abba/experiment.json', 'bench/results/v0811-bla341-abba/micro.tsv',
    'bench/results/v0811-bla341-abba/report.md', 'bench/results/v0811-bla341-abba/wire.tsv',
}

#: An absolute /tmp; a run's own tmp/ ($run/tmp, ${X}/tmp) is where temporary files belong.
_TMP = re.compile(r'(?<![\w}])/tmp\b|\bmktemp\b|\btempfile\b|gettempdir')
_RESULTS_DIR = re.compile(r"bench/(?:[\w.-]+/)*results\b(?!\.)|'bench'\s*/\s*'results'|HOME\}?/results")


def _tracked(*paths: str) -> list[str]:
    return subprocess.run(['git', 'ls-files', *paths], cwd=ROOT, capture_output=True,
                          text=True, check=True).stdout.split()


def _tools() -> list[str]:
    """Bench tools and their configs, and the scripts/ entry points that drive them."""
    return [f for f in _tracked('bench', 'scripts/run-*.sh')
            if not f.startswith(('bench/lib/', 'bench/results/'))
            and (f.endswith(('.sh', '.py', '.conf', '.ini', '.js', '.lua', '.toml'))
                 or Path(f).name.startswith('Dockerfile'))]


def _matching(pattern: re.Pattern) -> set[str]:
    return {f for f in _tools() if pattern.search((ROOT / f).read_text(errors='replace'))}


def test_no_tool_writes_to_tmp():
    found = _matching(_TMP)
    assert found - CONTAINER_TMP == set(), 'use the run directory from bench/lib/results'
    assert CONTAINER_TMP - found == set(), 'no longer matches: remove from CONTAINER_TMP'


def test_only_the_helper_chooses_a_results_directory():
    assert _matching(_RESULTS_DIR) == set(), 'ask bench/lib/results for the run directory'


def test_no_result_is_committed():
    committed = set(_tracked('bench/results')) - {'bench/results/README.md'}
    assert committed - PENDING_COMMITTED == set(), 'attach results to the issue instead'
    assert PENDING_COMMITTED - committed == set(), 'moved: remove from PENDING_COMMITTED'


def test_bench_has_no_loose_files():
    loose = {f for f in _tracked('bench') if f.count('/') == 1}
    assert loose <= {'bench/README.md', 'bench/CHARACTERIZATION.md'}, loose
