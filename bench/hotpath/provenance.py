#!/usr/bin/env python3
"""One environment stamp, shared by every hot-path harness.

A microbenchmark number without its machine, interpreter and tree is not a
result — it is an anecdote.  `bench/aws/httparena_compare.sh` already writes a
`provenance.md` for cloud runs; the local harnesses had nothing, so numbers
from them reached the docs with no way to check what produced them.

Import and call :func:`stamp` (human-readable) or :func:`as_dict` (for JSON
output).  Every harness in this directory prints it, so a pasted result carries
its own provenance.

    python bench/hotpath/provenance.py        # print the current stamp
"""
from __future__ import annotations

import datetime
import os
import pathlib
import platform
import re
import subprocess
import sys


def _run(cmd: list[str]) -> str:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ''


def _cpu_model() -> str:
    try:
        text = pathlib.Path('/proc/cpuinfo').read_text()
        m = re.search(r'^model name\s*:\s*(.+)$', text, re.MULTILINE)
        if m:
            return m.group(1).strip()
    except OSError:
        pass
    return platform.processor() or 'unknown'


def _git(repo: pathlib.Path) -> tuple[str, bool]:
    """(short commit, dirty).  A dirty tree makes the run unreproducible from
    a commit alone, which is worth saying out loud rather than hiding."""
    commit = _run(['git', '-C', str(repo), 'rev-parse', '--short', 'HEAD'])
    status = _run(['git', '-C', str(repo), 'status', '--porcelain'])
    tracked_dirty = any(not ln.startswith('??') for ln in status.splitlines())
    return commit or 'unknown', tracked_dirty


def _is_wsl() -> bool:
    return pathlib.Path('/proc/sys/fs/binfmt_misc/WSLInterop').exists() \
        or 'microsoft' in platform.release().lower()


def as_dict() -> dict:
    repo = pathlib.Path(__file__).resolve().parents[2]
    commit, dirty = _git(repo)
    try:
        import blackbull
        version = blackbull.__version__
    except Exception:                                    # noqa: BLE001
        version = 'unknown'
    return {
        'timestamp': datetime.datetime.now(datetime.UTC)
                     .strftime('%Y-%m-%dT%H:%M:%SZ'),
        'cpu': _cpu_model(),
        'cpus_online': os.cpu_count(),
        'affinity': len(os.sched_getaffinity(0))
                    if hasattr(os, 'sched_getaffinity') else None,
        'kernel': f'{platform.system()} {platform.release()}',
        'wsl2': _is_wsl(),
        'python': sys.version.split()[0],
        'python_build': platform.python_compiler(),
        'blackbull': version,
        'commit': commit,
        'dirty': dirty,
        'uvloop': os.environ.get('BB_UVLOOP', '0') == '1',
    }


def stamp(prefix: str = '  ') -> str:
    d = as_dict()
    tree = f"{d['blackbull']} @ {d['commit']}"
    if d['dirty']:
        tree += ' + UNCOMMITTED CHANGES'
    lines = [
        f"{d['cpu']}  ({d['cpus_online']} logical"
        + (f", {d['affinity']} in affinity mask" if d['affinity'] else '')
        + ')',
        f"{d['kernel']}" + ('  [WSL2 — not an isolated bench host]'
                            if d['wsl2'] else ''),
        f"Python {d['python']}  ({d['python_build']})",
        f"BlackBull {tree}",
        f"loop: {'uvloop' if d['uvloop'] else 'stock asyncio'}   "
        f"{d['timestamp']}",
    ]
    return '\n'.join(prefix + ln for ln in lines)


if __name__ == '__main__':
    print(stamp(''))
