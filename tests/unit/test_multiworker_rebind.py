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

try:
    from beartype.roar import (BeartypeCallHintParamViolation
                               as _BeartypeViolation)
except ImportError:  # beartype is a test dependency, not a runtime one
    _BeartypeViolation = None

#: Under ``--beartype-packages=blackbull`` the plan's ``port: int`` annotation
#: rejects a unix path's character before the refusal is reached.  Both are
#: loud; the bare ``TypeError`` this file exists to prevent is neither.
_REFUSALS = ((RuntimeError,) if _BeartypeViolation is None
             else (RuntimeError, _BeartypeViolation))


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
    """AF_UNIX under ``REUSEPORT`` refuses loudly and never attempts a bind.

    A unix path has no host to ask for, and its "port" is a character of that
    path: reading the address anyway feeds an ``str`` into the bind and comes
    back as a bare ``TypeError`` — which ``_bind_socket``'s ``except OSError``
    does not catch.  The plan's family filter is what keeps that from
    happening, so this pins the filter, not just the refusal.
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
        with pytest.raises(_REFUSALS) as refusal:
            MultiWorkerServer(BlackBull(), server.bound_listeners, None,
                              workers=2)
    finally:
        server.close_socket()
    assert calls == [], (
        f'a unix listener has no address to re-bind, tried {calls}')
    if isinstance(refusal.value, RuntimeError):
        assert 'BB_SOCKET_REUSEPORT' in str(refusal.value)
