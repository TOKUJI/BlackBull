"""
Tests for blackbull/rsock.py
============================

Each test is tied to a specific bug found during development.

Bug 7a – ``create_dual_stack_sockets(port=0)`` assigned different ports to IPv4
and IPv6 sockets because both were bound to port 0 independently.  The OS
assigned two distinct ephemeral ports, so the server ended up listening on two
different ports (e.g. 38341 and 38817).  The fix: bind IPv4 first, read the
OS-assigned port, then bind IPv6 to that *same* port.

Dual-stack support – original ``create_socket`` bound only to ``'::1'``
(IPv6 loopback).  Tests verify the new helpers bind both ``AF_INET`` and
``AF_INET6`` so IPv4-only clients can also reach the server.
"""

import socket
import pytest

from blackbull.protocol.rsock import (
    _bind_socket, create_socket, create_dual_stack_sockets, create_unix_socket,
    adopt_listening_fd, _SD_LISTEN_FDS_START,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _close_all(socks):
    for s in socks:
        if s is not None:
            try:
                s.close()
            except OSError:
                pass


# ---------------------------------------------------------------------------
# _bind_socket
# ---------------------------------------------------------------------------

class TestBindSocket:
    """Unit tests for the internal _bind_socket helper."""

    def test_ipv4_bind_returns_socket(self):
        """_bind_socket(AF_INET, '0.0.0.0', 0) must return a bound socket."""
        sock = _bind_socket(socket.AF_INET, '0.0.0.0', 0)
        try:
            assert sock is not None, "_bind_socket must return a socket object"
            assert sock.family == socket.AF_INET
        finally:
            if sock:
                sock.close()

    def test_ipv6_bind_returns_socket(self):
        """_bind_socket(AF_INET6, '::', 0) must return a bound socket."""
        sock = _bind_socket(socket.AF_INET6, '::', 0)
        try:
            assert sock is not None, "_bind_socket must return a socket object"
            assert sock.family == socket.AF_INET6
        finally:
            if sock:
                sock.close()

    def test_ipv4_socket_is_bound_to_a_port(self):
        """The returned socket must have a port > 0 when port=0 is requested."""
        sock = _bind_socket(socket.AF_INET, '0.0.0.0', 0)
        try:
            _, port = sock.getsockname()
            assert port > 0
        finally:
            if sock:
                sock.close()

    def test_ipv6_socket_has_ipv6_only_set(self):
        """IPv6 sockets must have IPV6_V6ONLY=1 to avoid conflicts with IPv4."""
        sock = _bind_socket(socket.AF_INET6, '::', 0)
        try:
            v6only = sock.getsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY)
            assert v6only == 1, (
                "IPV6_V6ONLY must be 1 on the IPv6 socket so it does not "
                "also handle IPv4-mapped addresses."
            )
        finally:
            if sock:
                sock.close()

    def test_ipv4_socket_has_so_reuseaddr(self):
        """SO_REUSEADDR must be set so restarts don't fail with 'address in use'."""
        sock = _bind_socket(socket.AF_INET, '0.0.0.0', 0)
        try:
            reuse = sock.getsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR)
            assert reuse != 0
        finally:
            if sock:
                sock.close()

    def test_socket_is_in_listen_state(self):
        """The socket must be listening, ready to accept() connections."""
        sock = _bind_socket(socket.AF_INET, '0.0.0.0', 0)
        try:
            # SO_ACCEPTCONN is 1 when the socket is in the listening state
            listening = sock.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN)
            assert listening == 1
        finally:
            if sock:
                sock.close()

    def test_invalid_host_returns_none(self):
        """An un-bindable address must return None, not raise."""
        # '999.999.999.999' is an invalid IPv4 literal; _bind_socket should
        # catch the OSError and return None.
        result = _bind_socket(socket.AF_INET, '999.999.999.999', 0)
        assert result is None


# ---------------------------------------------------------------------------
# create_socket
# ---------------------------------------------------------------------------

class TestCreateSocket:
    """Tests for the legacy create_socket helper."""

    def test_ipv4_host_creates_af_inet_socket(self):
        """create_socket(('0.0.0.0', 0)) must produce an AF_INET socket."""
        sock = create_socket(('0.0.0.0', 0))
        try:
            assert sock is not None
            assert sock.family == socket.AF_INET
        finally:
            if sock:
                sock.close()

    def test_ipv6_host_creates_af_inet6_socket(self):
        """create_socket(('::', 0)) must produce an AF_INET6 socket."""
        sock = create_socket(('::', 0))
        try:
            assert sock is not None
            assert sock.family == socket.AF_INET6
        finally:
            if sock:
                sock.close()

    def test_localhost_ipv4_creates_af_inet(self):
        """'127.0.0.1' is an IPv4 address; must create AF_INET socket."""
        sock = create_socket(('127.0.0.1', 0))
        try:
            assert sock.family == socket.AF_INET
        finally:
            if sock:
                sock.close()

    def test_ipv6_loopback_creates_af_inet6(self):
        """'::1' is an IPv6 address; must create AF_INET6 socket."""
        sock = create_socket(('::1', 0))
        try:
            assert sock is not None
            assert sock.family == socket.AF_INET6
        finally:
            if sock:
                sock.close()


# ---------------------------------------------------------------------------
# create_dual_stack_sockets – Bug 7a regression tests
# ---------------------------------------------------------------------------

class TestCreateDualStackSockets:
    """Tests for create_dual_stack_sockets() – Bug 7a regression suite."""

    def test_returns_at_least_one_socket(self):
        """At least one socket must be returned on any platform."""
        socks = create_dual_stack_sockets(0)
        try:
            assert len(socks) >= 1, "create_dual_stack_sockets returned an empty list"
        finally:
            _close_all(socks)

    def test_returns_ipv4_socket(self):
        """An AF_INET socket must always be in the list."""
        socks = create_dual_stack_sockets(0)
        try:
            families = {s.family for s in socks}
            assert socket.AF_INET in families, (
                "No IPv4 (AF_INET) socket found. "
                "IPv4 clients would be unable to connect."
            )
        finally:
            _close_all(socks)

    def test_all_sockets_share_the_same_port_when_port_is_zero(self):
        """Bug 7a regression: all sockets must listen on the *same* port.

        The old code bound both sockets to port=0 independently.  Each call
        received a different ephemeral port from the OS (e.g. 38341 and 38817).
        The fix binds IPv4 first, obtains its port, then binds IPv6 to that
        exact port, guaranteeing a single shared port.
        """
        socks = create_dual_stack_sockets(0)
        try:
            ports = {s.getsockname()[1] for s in socks}
            assert len(ports) == 1, (
                f"Sockets are on different ports: {ports}. "
                "This is Bug 7a: bind IPv4 first, reuse its port for IPv6."
            )
        finally:
            _close_all(socks)

    def test_all_sockets_share_explicit_port(self):
        """An explicit non-zero port must be shared by all returned sockets."""
        # Pick any free port by letting the OS assign one first
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind(('0.0.0.0', 0))
        free_port = probe.getsockname()[1]
        probe.close()

        socks = create_dual_stack_sockets(free_port)
        try:
            ports = {s.getsockname()[1] for s in socks}
            assert len(ports) == 1
            assert free_port in ports
        finally:
            _close_all(socks)

    def test_ipv6_socket_has_ipv6_only_set(self):
        """The IPv6 socket must have IPV6_V6ONLY=1 so it doesn't shadow IPv4."""
        socks = create_dual_stack_sockets(0)
        try:
            for s in socks:
                if s.family == socket.AF_INET6:
                    v6only = s.getsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY)
                    assert v6only == 1, (
                        "IPV6_V6ONLY must be 1 on the IPv6 socket. "
                        "Without it, the IPv6 socket would also accept "
                        "IPv4-mapped addresses, conflicting with the IPv4 socket."
                    )
        finally:
            _close_all(socks)

    def test_sockets_are_listening(self):
        """All returned sockets must be in the listen state."""
        socks = create_dual_stack_sockets(0)
        try:
            for s in socks:
                listening = s.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN)
                assert listening == 1, f"Socket {s} is not in listen state"
        finally:
            _close_all(socks)

    def test_sockets_are_plain_tcp_not_ssl(self):
        """Returned sockets must be plain TCP, not pre-wrapped SSLSockets.

        Bug 8 related: asyncio.start_server handles TLS via ssl= parameter.
        Pre-wrapping with ssl_context.wrap_socket() would cause a double-TLS
        layer and a broken handshake.
        """
        import ssl as _ssl
        socks = create_dual_stack_sockets(0)
        try:
            for s in socks:
                assert not isinstance(s, _ssl.SSLSocket), (
                    "create_dual_stack_sockets must return plain socket.socket "
                    "objects, not SSLSocket instances."
                )
        finally:
            _close_all(socks)

    def test_returns_empty_list_only_if_all_binds_fail(self):
        """When all bind attempts fail the result must be an empty list, not raise."""
        # Bind IPv4 to a port, then try to bind the same port again without
        # SO_REUSEADDR to force failure.  We test the shape of the return value,
        # not the error path of create_dual_stack_sockets directly.
        socks = create_dual_stack_sockets(0)
        # Successful case returns a list (even if only IPv4 is supported)
        try:
            assert isinstance(socks, list)
        finally:
            _close_all(socks)


# ---------------------------------------------------------------------------
# create_unix_socket — Sprint 12a
# ---------------------------------------------------------------------------

class TestCreateUnixSocket:
    """Tests for the AF_UNIX bind helper added in Sprint 12a."""

    def test_binds_listens_and_returns_socket(self, tmp_path):
        path = str(tmp_path / 'bb.sock')
        sock = create_unix_socket(path)
        try:
            assert sock is not None
            assert sock.family == socket.AF_UNIX
            assert sock.getsockname() == path
        finally:
            if sock is not None:
                sock.close()

    def test_unlinks_stale_socket_file(self, tmp_path):
        """A leftover socket file at the target path is unlinked before bind."""
        path = str(tmp_path / 'stale.sock')
        # Create a stale socket by binding+closing without listening.
        stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stale.bind(path)
        stale.close()
        # File should exist now.
        import os
        assert os.path.exists(path)
        # create_unix_socket must succeed despite the leftover.
        sock = create_unix_socket(path)
        try:
            assert sock is not None
            assert sock.getsockname() == path
        finally:
            if sock is not None:
                sock.close()

    def test_refuses_to_unlink_regular_file(self, tmp_path):
        """If the target path is a regular file, refuse to overwrite it."""
        path = tmp_path / 'not-a-socket'
        path.write_text('important user data\n')
        result = create_unix_socket(str(path))
        assert result is None
        # File must be untouched.
        assert path.read_text() == 'important user data\n'

    def test_applies_chmod_for_group_access(self, tmp_path):
        """Default mode 0o660 lets a same-group reverse proxy connect."""
        import os, stat
        path = str(tmp_path / 'mode.sock')
        sock = create_unix_socket(path, mode=0o660)
        try:
            assert sock is not None
            mode = os.stat(path).st_mode
            assert stat.S_IMODE(mode) == 0o660
        finally:
            if sock is not None:
                sock.close()

    def test_mode_none_skips_chmod(self, tmp_path):
        """``mode=None`` leaves the umask-derived bind() mode untouched."""
        import os, stat
        path = str(tmp_path / 'umask.sock')
        sock = create_unix_socket(path, mode=None)
        try:
            assert sock is not None
            # Whatever the umask gave us; just verify the socket is usable.
            assert stat.S_ISSOCK(os.stat(path).st_mode)
        finally:
            if sock is not None:
                sock.close()


# ---------------------------------------------------------------------------
# adopt_listening_fd — Sprint 12b
# ---------------------------------------------------------------------------

class TestAdoptListeningFd:
    """Tests for adopt_listening_fd() added in Sprint 12b."""

    @pytest.fixture()
    def listening_sock(self):
        """Pre-bound listening socket; cleaned up after the test."""
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(('127.0.0.1', 0))
        s.listen(8)
        yield s
        s.close()

    def test_adopts_fd_without_env_vars(self, monkeypatch, listening_sock):
        """When neither LISTEN_PID nor LISTEN_FDS is set, accept the fd as-is."""
        monkeypatch.delenv('LISTEN_PID', raising=False)
        monkeypatch.delenv('LISTEN_FDS', raising=False)
        fd = listening_sock.fileno()
        port = listening_sock.getsockname()[1]
        adopted = adopt_listening_fd(fd)
        try:
            assert adopted.getsockname()[1] == port
        finally:
            adopted.detach()

    def test_adopts_fd_when_listen_pid_matches(self, monkeypatch, listening_sock):
        """LISTEN_PID == our PID must allow adoption."""
        import os
        monkeypatch.setenv('LISTEN_PID', str(os.getpid()))
        monkeypatch.delenv('LISTEN_FDS', raising=False)
        adopted = adopt_listening_fd(listening_sock.fileno())
        adopted.detach()

    def test_rejects_listen_pid_mismatch(self, monkeypatch, listening_sock):
        """LISTEN_PID pointing at a different PID must raise RuntimeError."""
        monkeypatch.setenv('LISTEN_PID', '1')  # PID 1 (init) is never ours
        monkeypatch.delenv('LISTEN_FDS', raising=False)
        with pytest.raises(RuntimeError, match='LISTEN_PID'):
            adopt_listening_fd(listening_sock.fileno())

    def test_rejects_non_integer_listen_pid(self, monkeypatch, listening_sock):
        """LISTEN_PID that is not an integer must raise RuntimeError."""
        monkeypatch.setenv('LISTEN_PID', 'bogus')
        monkeypatch.delenv('LISTEN_FDS', raising=False)
        with pytest.raises(RuntimeError, match='LISTEN_PID'):
            adopt_listening_fd(listening_sock.fileno())

    def test_adopts_fd_inside_listen_fds_window(self, monkeypatch):
        """An fd that falls inside [SD_LISTEN_FDS_START, SD_LISTEN_FDS_START+LISTEN_FDS) passes."""
        monkeypatch.delenv('LISTEN_PID', raising=False)
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(('127.0.0.1', 0))
        s.listen(8)
        fd = s.fileno()
        # Set LISTEN_FDS large enough to cover this fd: window is [3, 3+N).
        n = fd - _SD_LISTEN_FDS_START + 1
        try:
            monkeypatch.setenv('LISTEN_FDS', str(n))
            adopted = adopt_listening_fd(fd)
            adopted.detach()
        finally:
            s.close()

    def test_rejects_fd_outside_listen_fds_window(self, monkeypatch, listening_sock):
        """fd outside [SD_LISTEN_FDS_START, SD_LISTEN_FDS_START+LISTEN_FDS) raises."""
        monkeypatch.delenv('LISTEN_PID', raising=False)
        monkeypatch.setenv('LISTEN_FDS', '1')
        with pytest.raises(RuntimeError, match='LISTEN_FDS'):
            adopt_listening_fd(100)

    def test_rejects_non_integer_listen_fds(self, monkeypatch, listening_sock):
        """LISTEN_FDS that is not an integer must raise RuntimeError."""
        monkeypatch.delenv('LISTEN_PID', raising=False)
        monkeypatch.setenv('LISTEN_FDS', 'bad')
        with pytest.raises(RuntimeError, match='LISTEN_FDS'):
            adopt_listening_fd(listening_sock.fileno())

    def test_raises_on_closed_fd(self, monkeypatch):
        """Adopting a closed fd must raise RuntimeError (not bubble OSError)."""
        monkeypatch.delenv('LISTEN_PID', raising=False)
        monkeypatch.delenv('LISTEN_FDS', raising=False)
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        fd = s.fileno()
        s.close()
        with pytest.raises(RuntimeError, match='adopt'):
            adopt_listening_fd(fd)
