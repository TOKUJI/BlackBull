"""A replacement generation that dies is noticed when it exits, not at the deadline.

The control socket's EOF arrives only once every copy of the child's end is
closed, so the supervisor cannot rely on it alone to see a dead candidate.
"""
from __future__ import annotations

import socket
import subprocess
import sys
import time

import pytest

from blackbull.server import reload_generation
from blackbull.server.reload_generation import ReloadGeneration


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
