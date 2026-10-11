#!/usr/bin/env python3
"""G3-4: detection power by defense-site mutation (BLA-526 M5).

Each mutation neutralizes one BlackBull defense in a throwaway copy of
the package; the fixture serves from the mutated tree while the probe
runs unchanged from the worktree.  A mutation is DETECTED when at least
one check that passed on the clean baseline flips to FAIL/TIMEOUT — the
harness catching the missing defense is the measurement.

Usage::

    python tools/security/mutation_run.py [--keep]
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

VERDICTS = ('PASS', 'FAIL', 'TIMEOUT', 'SKIP')
PORT, TLS_PORT = 8131, 8451

#: (name, file under blackbull/, exact source removed, replacement)
MUTATIONS = (
    ('cl-te-rejection',
     'server/http1_actor.py',
     """    if cls and tes:
        raise BadRequestError(
            'Content-Length and Transfer-Encoding both present '
            '(smuggling vector)')""",
     """    if cls and tes:
        pass  # MUTATION: CL+TE smuggling rejection neutralized"""),
    ('malformed-content-length',
     'server/http1_actor.py',
     """        except ValueError as exc:
            raise BadRequestError(str(exc)) from exc

    if tes:""",
     """        except ValueError as exc:
            declared = 0  # MUTATION: malformed Content-Length accepted

    if tes:"""),
    ('te-list-rejection',
     'server/http1_actor.py',
     """        if members == [(b'chunked', ())]:
            return declared  # RFC 9112 \u00a76.1 \u2014 the one accepted form""",
     """        return declared  # MUTATION: any TE list accepted"""),
)


def parse_verdicts(text: str) -> dict[str, str]:
    """Map check id to verdict word from the probe's report table."""
    found: dict[str, str] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[2] in VERDICTS:
            found[parts[0]] = parts[2]
    return found


def _wait_port(port: int, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            socket.create_connection(('127.0.0.1', port), 1).close()
            return
        except OSError:
            time.sleep(0.3)
    raise RuntimeError(f'fixture did not open port {port}')


def run_probe(root: Path) -> str:
    return subprocess.run(
        [sys.executable, 'tools/security/probe.py',
         '--base-url', f'http://127.0.0.1:{PORT}',
         '--h2-url', f'https://127.0.0.1:{TLS_PORT}'],
        cwd=root, env=dict(os.environ), capture_output=True, text=True,
        timeout=300,
    ).stdout


def start_fixture(entry: Path, root: Path) -> subprocess.Popen:
    """Run *entry* (a fixture_app.py inside tree or worktree).

    The fixture inserts its own package root at sys.path[0], so the tree
    must CONTAIN the blackbull/ it should serve — a PYTHONPATH override
    alone loses to that insert.
    """
    return subprocess.Popen(
        [sys.executable, str(entry),
         '--port', str(PORT), '--tls-port', str(TLS_PORT)],
        cwd=root, env=dict(os.environ), stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, start_new_session=True)


def stop_fixture(proc: subprocess.Popen) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--keep', action='store_true',
                        help='keep the mutated trees in /tmp')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    work = Path('/tmp/bla526-mut')
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)

    print('G3-4 mutation run (a mutation is DETECTED when a clean PASS flips)')
    entry = root / 'tools/security/fixture_app.py'
    proc = start_fixture(entry, root)
    try:
        _wait_port(PORT)
        baseline = parse_verdicts(run_probe(root))
    finally:
        stop_fixture(proc)
    passed = [cid for cid, v in baseline.items() if v == 'PASS']
    print(f'baseline: {len(baseline)} rows, {len(passed)} PASS')

    detected = 0
    for name, relpath, old, new in MUTATIONS:
        tree = work / name
        shutil.copytree(root / 'blackbull', tree / 'blackbull')
        shutil.copytree(root / 'tools', tree / 'tools')
        target = tree / 'blackbull' / relpath
        source = target.read_text()
        if source.count(old) != 1:
            print(f'  {name:<28} ERROR: source anchor count '
                  f'{source.count(old)} != 1')
            return 2
        target.write_text(source.replace(old, new))
        proc = start_fixture(tree / 'tools/security/fixture_app.py', root)
        try:
            _wait_port(PORT)
            mutated = parse_verdicts(run_probe(root))
        finally:
            stop_fixture(proc)
        flips = sorted(cid for cid in passed
                       if mutated.get(cid) in ('FAIL', 'TIMEOUT'))
        if flips:
            detected += 1
            print(f'  {name:<28} DETECTED by {len(flips)} row(s): '
                  + ', '.join(flips[:6]))
        else:
            print(f'  {name:<28} NOT DETECTED')
    if not args.keep:
        shutil.rmtree(work, ignore_errors=True)
    print(f'{len(MUTATIONS)} mutations, {detected} detected')
    return 0 if detected == len(MUTATIONS) else 1


if __name__ == '__main__':
    raise SystemExit(main())
