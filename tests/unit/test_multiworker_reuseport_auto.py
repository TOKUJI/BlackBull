"""``BB_SOCKET_REUSEPORT``: ``auto`` by default, ``1`` and ``0`` as named.

``auto`` gives each worker its own ``SO_REUSEPORT`` socket only where the
kernel spreads connections across the group (Linux) and every listener is a
TCP socket BlackBull bound itself.  Everything else keeps the shared socket,
so a configuration that starts with the shared socket still starts.  An
explicit ``1`` is followed on every platform.

Each 2-worker server is planned in a forked child, which releases every socket
it bound when it exits.  The kernel is the witness: a fresh ``SO_REUSEPORT``
socket can join the port only when every socket listening on it carries the
option, which per-worker sockets do and the shared one does not.
"""
from __future__ import annotations

import multiprocessing
import os
import socket
import sys
import traceback

import pytest

from blackbull import BlackBull, env
from blackbull.server import multiworker
from blackbull.server.listener import InheritedFd, Listener, Tcp, Unix
from blackbull.server.multiworker import MultiWorkerServer
from blackbull.server.server import Server

pytestmark = [
    pytest.mark.skipif(not hasattr(socket, 'SO_REUSEPORT'),
                       reason='no SO_REUSEPORT on this platform'),
    pytest.mark.skipif(not hasattr(os, 'fork'), reason='plans in a forked child'),
]

SPLIT, SHARED, STARTED = 'split', 'shared', 'started'


@pytest.fixture
def setting(monkeypatch):
    def _set(value):
        if value is None:
            monkeypatch.delenv('BB_SOCKET_REUSEPORT', raising=False)
        else:
            monkeypatch.setenv('BB_SOCKET_REUSEPORT', value)
        env.reset_settings_cache()
    yield _set
    env.reset_settings_cache()


def _binds(port: int, reuseport: bool) -> bool:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if reuseport:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        probe.bind(('127.0.0.1', port))
        return True
    except OSError:
        return False
    finally:
        probe.close()


def _joinable(port: int) -> bool:
    """Something listens on *port*, and every listener carries SO_REUSEPORT."""
    assert not _binds(port, reuseport=False), f'nothing listens on {port}'
    return _binds(port, reuseport=True)


def _in_child(build) -> str:
    """Run *build* in a forked child; it returns the port to probe (or None)
    and whatever must stay open while the port is probed."""
    receive, send = multiprocessing.get_context('fork').Pipe(duplex=False)

    def child():
        try:
            port, _alive = build()
            send.send(STARTED if port is None
                      else SPLIT if _joinable(port) else SHARED)
        except BaseException:
            send.send(traceback.format_exc())

    process = multiprocessing.get_context('fork').Process(target=child)
    process.start()
    try:
        return receive.recv() if receive.poll(30) else 'the child did not answer'
    finally:
        process.join(30)


def _plan(listeners, **kwargs) -> str:
    def build():
        server = Server(BlackBull(), listeners=listeners)
        server.open_socket()
        ports = [sock.getsockname()[1] for _listener, socks in server.bound_listeners
                 for sock in socks if sock.family == socket.AF_INET]
        mws = MultiWorkerServer(BlackBull(), server.bound_listeners, None,
                                workers=2, **kwargs)
        return (ports[0] if ports else None), (server, mws)
    return _in_child(build)


def _tcp():
    return Listener(Tcp(0, host='127.0.0.1'))


@pytest.mark.parametrize('raw, expected', [
    (None, None), ('auto', None), ('AUTO', None), ('', None),
    ('1', True), ('true', True), ('0', False), ('off', False),
])
def test_the_setting_reads_auto_or_a_choice(setting, raw, expected):
    setting(raw)
    assert env.get_settings().socket_reuseport is expected


@pytest.mark.skipif(not sys.platform.startswith('linux'),
                    reason='auto splits only where the kernel balances the group')
def test_auto_gives_each_worker_its_own_socket_on_linux(setting):
    setting(None)
    assert _plan([_tcp()]) == SPLIT


def test_auto_shares_the_socket_where_the_kernel_does_not_balance(
        setting, monkeypatch):
    setting(None)
    monkeypatch.setattr(multiworker, 'REUSEPORT_BALANCES', False)
    assert _plan([_tcp()]) == SHARED


def test_an_explicit_one_splits_even_where_the_kernel_does_not_balance(
        setting, monkeypatch):
    setting('1')
    monkeypatch.setattr(multiworker, 'REUSEPORT_BALANCES', False)
    assert _plan([_tcp()]) == SPLIT


def test_an_explicit_zero_shares_the_socket(setting):
    setting('0')
    assert _plan([_tcp()]) == SHARED


def test_auto_shares_under_reload(setting):
    setting(None)
    assert _plan([_tcp()], reload=True) == SHARED


def test_auto_starts_a_unix_listener_on_the_shared_socket(setting, tmp_path):
    """An explicit ``1`` refuses AF_UNIX; the default must not."""
    setting(None)
    assert _plan([Listener(Unix(str(tmp_path / 's.sock')))]) == STARTED


def test_auto_shares_every_listener_when_one_cannot_split(setting, tmp_path):
    setting(None)
    assert _plan([_tcp(), Listener(Unix(str(tmp_path / 's.sock')))]) == SHARED


def test_auto_shares_an_adopted_socket(setting):
    """A supervisor's socket is the supervisor's: auto neither re-binds nor refuses it."""
    setting(None)

    def build():
        creator = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        creator.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        creator.bind(('127.0.0.1', 0))
        creator.listen(8)
        adopted = socket.socket(fileno=os.dup(creator.fileno()))
        mws = MultiWorkerServer(
            BlackBull(), [(Listener(InheritedFd(adopted.fileno())), [adopted])],
            None, workers=2)
        return creator.getsockname()[1], (creator, adopted, mws)
    assert _in_child(build) == SHARED


def test_a_single_worker_bind_does_not_set_reuseport_by_default(setting):
    """Two instances started by mistake on one port must still collide."""
    setting(None)

    def build():
        server = Server(BlackBull(), listeners=[_tcp()])
        server.open_socket()
        return next(sock.getsockname()[1] for _listener, socks in server.bound_listeners
                    for sock in socks), server
    assert _in_child(build) == SHARED
