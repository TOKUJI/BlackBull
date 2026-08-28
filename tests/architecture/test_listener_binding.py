"""A Server binds the listeners it was given — all of them.

The defect this closes: ``Server`` bound one HTTP listener (``self.port``,
singular), so a deployment needing cleartext and TLS at once could not say so
and ran one process per port.  These tests assert the sockets that exist after
``open_socket()``, which is what the process count is made of.

Design: `.claude/planning/designs/listener-vocabulary.md`.
"""
from __future__ import annotations

import socket

import pytest

from blackbull import BlackBull
from blackbull.server.listener import InheritedFd, Listener, Tcp, Unix
from blackbull.server.server import Server


@pytest.fixture
def app():
    return BlackBull()


def _bound_ports(server) -> set[int]:
    """The distinct TCP ports the server is actually listening on."""
    return {
        sock.getsockname()[1]
        for _listener, socks in server.bound_listeners
        for sock in socks
        if sock.family in (socket.AF_INET, socket.AF_INET6)
    }


class TestManyListeners:
    def test_four_listeners_bind_four_ports(self, app):
        server = Server(app, listeners=[Listener(Tcp(0)) for _ in range(4)])
        server.open_socket()
        try:
            assert len(server.bound_listeners) == 4
            assert len(_bound_ports(server)) == 4, 'each listener owns its own port'
        finally:
            server.close_socket()

    def test_every_bound_listener_is_dual_stack(self, app):
        server = Server(app, listeners=[Listener(Tcp(0)), Listener(Tcp(0))])
        server.open_socket()
        try:
            for _listener, socks in server.bound_listeners:
                families = {sock.family for sock in socks}
                assert socket.AF_INET in families
        finally:
            server.close_socket()

    def test_the_bound_port_is_read_back_for_an_os_assigned_one(self, app):
        server = Server(app, listeners=[Listener(Tcp(0))])
        server.open_socket()
        try:
            listener, socks = server.bound_listeners[0]
            assert listener.where.port == 0, 'the request is not rewritten'
            assert socks[0].getsockname()[1] != 0, 'the answer is'
            assert server.port == socks[0].getsockname()[1]
        finally:
            server.close_socket()

    def test_close_socket_closes_every_listener(self, app):
        server = Server(app, listeners=[Listener(Tcp(0)) for _ in range(3)])
        server.open_socket()
        socks = [sock for _listener, s in server.bound_listeners for sock in s]
        server.close_socket()
        for sock in socks:
            assert sock.fileno() == -1

    def test_a_port_already_taken_names_itself(self, app):
        # Both families, or the dual-stack bind half-succeeds on the free one.
        v4 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        v4.bind(('0.0.0.0', 0))
        v4.listen(1)
        port = v4.getsockname()[1]
        v6 = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        v6.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        try:
            v6.bind(('::', port))
            v6.listen(1)
        except OSError:
            pytest.skip('no IPv6 on this host')
        try:
            server = Server(app, listeners=[Listener(Tcp(0)), Listener(Tcp(port))])
            with pytest.raises(RuntimeError, match=str(port)):
                server.open_socket()
            server.close_socket()
        finally:
            v4.close()
            v6.close()


class TestAddressKinds:
    def test_a_host_binds_only_that_interface(self, app):
        server = Server(app, listeners=[Listener(Tcp(0, host='127.0.0.1'))])
        server.open_socket()
        try:
            _listener, socks = server.bound_listeners[0]
            assert {sock.getsockname()[0] for sock in socks} == {'127.0.0.1'}
        finally:
            server.close_socket()

    def test_a_unix_path_is_an_address_not_a_special_case(self, app, tmp_path):
        path = str(tmp_path / 'bb.sock')
        server = Server(app, listeners=[Listener(Unix(path))])
        server.open_socket()
        try:
            _listener, socks = server.bound_listeners[0]
            assert socks[0].family == socket.AF_UNIX
            assert server.unix_path == path
            assert server.port is None
        finally:
            server.close_socket()

    def test_an_inherited_fd_is_adopted_not_rebound(self, app):
        listening = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listening.bind(('127.0.0.1', 0))
        listening.listen(16)
        expected = listening.getsockname()[1]
        # detach(): the fd is handed over, and closing it twice is an EBADF.
        fd = listening.detach()
        server = Server(app, listeners=[Listener(InheritedFd(fd))])
        server.open_socket()
        try:
            _listener, socks = server.bound_listeners[0]
            assert socks[0].getsockname()[1] == expected
            assert server.port == expected
        finally:
            server.close_socket()


class TestTheSinglePortPathIsUnchanged:
    """``app.run(port=...)`` must keep behaving exactly as it did."""

    def test_open_socket_with_a_port_produces_one_listener(self, app):
        server = Server(app)
        server.open_socket(0)
        try:
            assert len(server.bound_listeners) == 1
            assert server.port == server.raw_sockets[0].getsockname()[1]
            assert server.unix_path is None
        finally:
            server.close_socket()

    def test_open_socket_with_a_unix_path_still_works(self, app, tmp_path):
        path = str(tmp_path / 'legacy.sock')
        server = Server(app)
        server.open_socket(unix_path=path)
        try:
            assert server.unix_path == path
            assert server.port is None
        finally:
            server.close_socket()

    def test_raw_sockets_still_names_every_http_socket(self, app):
        """What the multi-worker master hands to each worker."""
        server = Server(app, listeners=[Listener(Tcp(0)), Listener(Tcp(0))])
        server.open_socket()
        try:
            expected = [sock for _listener, socks in server.bound_listeners
                        for sock in socks]
            assert server.raw_sockets == expected
        finally:
            server.close_socket()
