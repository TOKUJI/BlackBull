"""The kernel oracle behind the per-worker ``SO_REUSEPORT`` refusal.

``_held_elsewhere`` answers from the kernel's own list of listening sockets,
and "the kernel could not be asked" must not read as "nobody holds it".
"""
from __future__ import annotations

import os
import socket
from pathlib import Path

import pytest

from blackbull.server import multiworker
from blackbull.server.listener import InheritedFd, Listener
from blackbull.server.multiworker import (
    _PROC_NET, _held_elsewhere, _kernel_listening, _PlannedListener,
)

pytestmark = pytest.mark.skipif(
    not Path('/proc/net/tcp').exists(),
    reason='needs the kernel socket list at /proc/net/tcp')


def _listening_socket():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(('127.0.0.1', 0))
    sock.listen(8)
    return sock


def _plan_for(sock) -> _PlannedListener:
    return _PlannedListener(
        listener=Listener(InheritedFd(sock.fileno())),
        port=sock.getsockname()[1],
        where='127.0.0.1:0',
        reached=frozenset({'v4'}),
        adopted=((sock.family, os.fstat(sock.fileno()).st_ino),),
        flagged=True,
        foreign_netns=False,
    )


def test_the_kernel_lists_a_listening_socket_by_inode():
    sock = _listening_socket()
    try:
        inode = os.fstat(sock.fileno()).st_ino
        assert inode in _kernel_listening(sock.getsockname()[1],
                                          {socket.AF_INET})
    finally:
        sock.close()


def test_the_kernel_drops_the_socket_once_it_is_closed():
    sock = _listening_socket()
    port = sock.getsockname()[1]
    inode = os.fstat(sock.fileno()).st_ino
    sock.close()
    assert inode not in _kernel_listening(port, {socket.AF_INET})


def test_a_family_the_kernel_has_no_list_for_reads_as_unknown():
    # Not the same answer as "no listeners": a caller that cannot ask must not
    # conclude the port is free.
    assert _kernel_listening(12345, {socket.AF_UNIX}) is None


def test_an_unreadable_kernel_list_reads_as_unknown(monkeypatch):
    monkeypatch.setitem(_PROC_NET, socket.AF_INET, '/nonexistent/net/tcp')
    assert _kernel_listening(12345, {socket.AF_INET}) is None


def test_an_empty_kernel_list_reads_as_unknown(monkeypatch, tmp_path):
    # A masked procfs reports nothing at all; that is not "no listeners".
    empty = tmp_path / 'tcp'
    empty.write_text('')
    monkeypatch.setitem(_PROC_NET, socket.AF_INET, str(empty))
    assert _kernel_listening(12345, {socket.AF_INET}) is None


def test_held_elsewhere_follows_the_kernel_not_the_flag():
    sock = _listening_socket()
    plan = _plan_for(sock)
    try:
        assert _held_elsewhere(plan) is True      # this process still holds it
    finally:
        sock.close()
    assert _held_elsewhere(plan) is False         # every copy is gone


def test_held_elsewhere_is_unknown_when_the_kernel_cannot_be_asked(monkeypatch):
    sock = _listening_socket()
    plan = _plan_for(sock)
    try:
        monkeypatch.setitem(_PROC_NET, socket.AF_INET, '/nonexistent/net/tcp')
        assert _held_elsewhere(plan) is None
    finally:
        sock.close()


def test_a_host_that_cannot_name_the_namespace_reads_as_local(monkeypatch):
    # Not answering must not refuse a socket that is simply this namespace's.
    sock = _listening_socket()
    try:
        for unanswerable in (None, -1):  # off Linux / the number is not this option
            monkeypatch.setattr(multiworker, '_SO_NETNS_COOKIE', unanswerable)
            assert multiworker._elsewhere_netns(sock) is False
    finally:
        sock.close()
