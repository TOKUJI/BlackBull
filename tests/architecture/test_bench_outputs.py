"""Every bench tool writes to one place: ``bench/results/<tool>/<run>/``.

Tools take their run directory from ``bench/lib/results`` (shell or Python),
keep temporary files under that run's ``tmp/``, and never touch ``/tmp``.
Results are evidence for an issue, not code: they are attached to YouTrack and
never committed.  bench/ holds no loose scripts — every tool sits in a folder
named for its purpose.

``PENDING_*`` lists the files not yet migrated, each with the issue that moves
it.  The lists only shrink: a new violator fails, and so does an entry that no
longer violates.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

#: Files that still use /tmp, mktemp or tempfile.
PENDING_TMP = {
    # BLA-542 — the AWS lifecycle and its remote result layout.
    'bench/cloud/ab.sh', 'bench/cloud/down.sh', 'bench/cloud/httparena_compare.sh',
    'bench/cloud/install.sh', 'bench/cloud/profile_lanes.sh', 'bench/cloud/run.sh',
    'bench/cloud/up.sh', 'bench/compare/server_lifecycle_remote.sh',
    'scripts/run-ab-verify.sh', 'scripts/run-httparena-bench.sh',
    # BLA-542 — wraps compare_servers.sh and finds its report by name.
    'scripts/run-bench-compare.sh',
    # BLA-542 — HttpArena runs on the remote instance and in Docker.
    'bench/httparena/Dockerfile.dev', 'bench/httparena/build_wheel.sh',
    'bench/httparena/patch_wsl2_docker.sh', 'bench/httparena/ready_check.sh',
    'bench/httparena/validate_local.sh',
}

#: Files that still choose a results directory themselves instead of asking the helper.
PENDING_ROOT = {
    # BLA-542 — the AWS lifecycle pulls remote results by these paths.
    'bench/cloud/ab.sh', 'bench/cloud/compare_servers_aws.sh', 'bench/cloud/httparena_compare.sh',
    'bench/cloud/install.sh', 'bench/cloud/profile.sh', 'bench/cloud/profile_lanes.sh',
    'bench/cloud/run.sh', 'bench/cloud/sprint104_ab.sh', 'bench/httparena/validate_local.sh',
    'scripts/run-ab-verify.sh', 'scripts/run-httparena-bench.sh',
    'bench/httparena/run_httparena.sh', 'bench/httparena/runner.sh',
    # BLA-542 — local tools whose output the AWS lifecycle fetches by path.
    'bench/ab/ab_commit.sh', 'bench/ab/ab_commit_h2.sh', 'bench/compare/compare_servers.sh',
    'bench/micro/profile_under_load.sh', 'scripts/run-bench-compare.sh',
}

#: Files whose /tmp is inside a container, not on the host.
CONTAINER_TMP = {
    'bench/conformance/autobahn_heavy.sh',  # the CaseSet resolver in the tester image
}

#: Committed results not yet moved to YouTrack (BLA-543).
PENDING_COMMITTED = {
    'bench/results/v0811-bla341-abba/experiment.json', 'bench/results/v0811-bla341-abba/micro.tsv',
    'bench/results/v0811-bla341-abba/report.md', 'bench/results/v0811-bla341-abba/wire.tsv',
}

_TMP = re.compile(r'/tmp\b|\bmktemp\b|\btempfile\b|gettempdir')
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
    assert found - PENDING_TMP - CONTAINER_TMP == set(), 'use the run directory from bench/lib/results'
    assert (PENDING_TMP | CONTAINER_TMP) - found == set(), 'migrated: remove from the list'


def test_only_the_helper_chooses_a_results_directory():
    found = _matching(_RESULTS_DIR)
    assert found - PENDING_ROOT == set(), 'ask bench/lib/results for the run directory'
    assert PENDING_ROOT - found == set(), 'migrated: remove from PENDING_ROOT'


def test_no_result_is_committed():
    committed = set(_tracked('bench/results')) - {'bench/results/README.md'}
    assert committed - PENDING_COMMITTED == set(), 'attach results to the issue instead'
    assert PENDING_COMMITTED - committed == set(), 'moved: remove from PENDING_COMMITTED'


def test_bench_has_no_loose_files():
    loose = {f for f in _tracked('bench') if f.count('/') == 1}
    assert loose <= {'bench/README.md', 'bench/CHARACTERIZATION.md'}, loose
