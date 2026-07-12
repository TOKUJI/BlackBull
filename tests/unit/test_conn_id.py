"""Unit tests for ``blackbull.server.conn_id`` — cheap process-unique
connection ids.

Background (limited-conn churn analysis, 2026-07-12): the per-connection
``uuid.uuid4()`` costs ~2 µs (16 urandom bytes + UUID formatting), and the
older 4-byte ``os.urandom`` scheme has real birthday-bound collision odds at
production churn (32-bit ids: ~1.2 % at 10 k concurrent connections, ~50 %
at 65 k — the old docstring's "~10⁻⁹ at 65 k" was wrong).  The replacement
is a per-process random prefix plus a monotonic sequence: unique within a
process by construction, no per-connection entropy syscall.
"""
from __future__ import annotations

import os

import pytest

from blackbull.server.conn_id import new_connection_id


def test_ids_are_unique_at_churn_scale():
    ids = {new_connection_id() for _ in range(20_000)}
    assert len(ids) == 20_000


def test_id_is_opaque_fixed_width_hex():
    cid = new_connection_id()
    assert len(cid) == 20                     # 12-hex prefix + 8-hex sequence
    assert set(cid) <= set('0123456789abcdef')


def test_ids_share_process_prefix_and_increment():
    a, b = new_connection_id(), new_connection_id()
    assert a[:12] == b[:12]
    assert int(b[12:], 16) == int(a[12:], 16) + 1


@pytest.mark.skipif(not hasattr(os, 'fork'), reason='requires os.fork')
def test_forked_child_gets_fresh_prefix():
    parent_prefix = new_connection_id()[:12]
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:                              # child
        os.close(r)
        os.write(w, new_connection_id()[:12].encode())
        os._exit(0)
    os.close(w)
    child_prefix = os.read(r, 32).decode()
    os.close(r)
    os.waitpid(pid, 0)
    assert child_prefix != parent_prefix
