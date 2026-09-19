"""What the per-worker re-bind asks for, per shape of master socket.

The socket is the fact: ``Listener.where`` has dropped a named host, and the
wildcard ``::`` is the one address a single bind cannot repeat — a fresh ``::``
comes back with ``IPV6_V6ONLY`` set, which drops the IPv4 reach the master's
socket had.  A socket with no IP address is not re-bound at all.
"""
from __future__ import annotations

import socket

import pytest

from blackbull import BlackBull, env as _env
from blackbull.server import multiworker
from blackbull.server.listener import Listener, Unix
from blackbull.server.multiworker import MultiWorkerServer, _rebind_address
from blackbull.server.server import Server


def _bound(family, host, v6only):
    sock = socket.socket(family, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if v6only is not None:
        sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, v6only)
    sock.bind((host, 0))
    sock.listen(8)
    return sock


@pytest.mark.skipif(not hasattr(socket, 'IPV6_V6ONLY'),
                    reason='IPv6 socket options not supported on this platform')
@pytest.mark.parametrize('host, v6only, expected', [
    ('127.0.0.1', None, '127.0.0.1'),
    ('::1', 0, '::1'),
    ('::', 1, '::'),
    ('::', 0, None),
], ids=['v4-named', 'v6-named', 'v6-wildcard-only', 'v6-wildcard-dual'])
def test_the_target_repeats_the_address_the_socket_was_bound_to(
        host, v6only, expected):
    """``None`` is the dual-stack pair, and only for the wildcard that maps it."""
    family = socket.AF_INET if ':' not in host else socket.AF_INET6
    sock = _bound(family, host, v6only)
    try:
        assert _rebind_address(sock) == expected
    finally:
        sock.close()


def test_a_unix_listener_gets_no_rebind_target(tmp_path, monkeypatch):
    """AF_UNIX under ``REUSEPORT`` refuses, naming the real incompatibility.

    ``SO_REUSEPORT`` is an IP-socket option, so a unix listener has no address
    to re-bind and cannot be given a per-worker socket.  The refusal is raised
    before the plan is built: reading the path's second character as a port is
    what used to come back as a bare ``TypeError``, or as an annotation
    violation under beartype.
    """
    monkeypatch.setenv('BB_SOCKET_REUSEPORT', '1')
    _env.reset_settings_cache()
    calls = []
    monkeypatch.setattr(multiworker, 'create_configured_sockets',
                        lambda port, cfg, **kwargs:
                        calls.append((port, kwargs)) or [])

    server = Server(BlackBull(),
                    listeners=[Listener(Unix(str(tmp_path / 's.sock')))])
    server.open_socket()
    try:
        with pytest.raises(RuntimeError) as refusal:
            MultiWorkerServer(BlackBull(), server.bound_listeners, None,
                              workers=2)
    finally:
        server.close_socket()
    message = str(refusal.value)
    assert calls == [], (
        f'a unix listener has no address to re-bind, tried {calls}')
    assert 'BB_SOCKET_REUSEPORT' in message
    assert str(tmp_path / 's.sock') in message
    assert 'SO_REUSEPORT is an IP-socket option' in message
    assert 'IPv6' not in message and 'another process' not in message


def test_a_unix_socket_claims_no_ip_stack(tmp_path):
    """``_reaches`` names the stacks a socket answers on; a unix socket is on
    none, and claiming one is what misdiagnosed the refusal as an IPv6 gap."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.bind(str(tmp_path / 's.sock'))
        sock.listen(8)
        assert multiworker._reaches([sock]) == frozenset()
    finally:
        sock.close()
