"""A replacement generation that dies is noticed when it exits, not at the deadline.

The control socket's EOF arrives only once every copy of the child's end is
closed, so the supervisor cannot rely on it alone to see a dead candidate.
"""
from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time

import pytest

from blackbull.server import reload_generation
from blackbull.server.reload_generation import ReloadGeneration

pytestmark = pytest.mark.skipif(not os.path.isdir('/proc/self'), reason='reads /proc')


def test_a_candidate_that_exits_is_reported_before_the_deadline(monkeypatch):
    monkeypatch.setattr(reload_generation, 'STARTUP_TIMEOUT', 10.0)
    owner, held_elsewhere = socket.socketpair()
    owner.settimeout(0.05)
    process = subprocess.Popen([sys.executable, '-c', 'pass'])
    process.wait(timeout=10)
    generation = object.__new__(ReloadGeneration)
    generation.process, generation.control = process, owner
    try:
        start = time.monotonic()
        with pytest.raises(RuntimeError, match='exited'):
            generation.activate(lambda: False)
        assert time.monotonic() - start < 2.0
    finally:
        owner.close()
        held_elsewhere.close()


def test_stopping_a_generation_whose_leader_already_exited_reclaims_its_workers():
    """A dead supervisor leaves its workers serving unless its group is killed."""
    leader = subprocess.Popen(
        [sys.executable, '-c',
         'import subprocess, sys; '
         'child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"]); '
         'print(child.pid, flush=True)'],
        stdout=subprocess.PIPE, text=True, start_new_session=True)
    worker = int(leader.stdout.readline())
    leader.wait(timeout=10)                    # reaped: returncode is set
    owner, held_elsewhere = socket.socketpair()
    owner.settimeout(0.05)
    held_elsewhere.close()
    generation = object.__new__(ReloadGeneration)
    generation.process, generation.control = leader, owner
    try:
        generation.stop(1.0)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and _alive(worker):
            time.sleep(0.05)
        assert not _alive(worker), 'the dead supervisor left its worker running'
    finally:
        if _alive(worker):
            os.kill(worker, signal.SIGKILL)
        owner.close()


def _alive(pid: int) -> bool:
    try:
        with open(f'/proc/{pid}/stat') as stat:
            return stat.read().rsplit(')', 1)[1].split()[0] != 'Z'
    except FileNotFoundError:
        return False
