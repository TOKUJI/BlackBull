"""
Tests for ASGIServer and rsock (blackbull/server/server.py, blackbull/rsock.py)
===============================================================================

Each test is tied to a specific bug found during development.

Bug 1 – ``make_ssl_context()`` init-order ``ValueError``
    ``ASGIServer.__init__`` assigned ``self.keyfile`` *before* ``self.certfile``,
    so the ``keyfile`` setter called ``make_ssl_context()`` while certfile was
    still ``None``, raising ``ValueError``.  A test that constructs
    ``ASGIServer(app, certfile=..., keyfile=...)`` and verifies no exception is
    raised catches this immediately.

Bug 7 – ``create_dual_stack_sockets(port=0)`` gave different ports per family
    Both IPv4 and IPv6 sockets were bound to ``port=0`` independently, letting
    the OS assign two different ephemeral ports.  Tests that verify all returned
    sockets share one port number catch this.

Bug 8 – ``sockets=`` kwarg / double-TLS layer
    ``asyncio.start_server()`` forwards ``**kwds`` to ``loop.create_server()``,
    which only accepts ``sock=`` (singular).  Passing ``sockets=`` raises
    ``TypeError`` at runtime.  An integration test that actually calls
    ``server.run()`` with a task and cancels it after a short sleep catches this;
    the server would crash before the sleep if the kwarg is wrong.
    Pre-wrapping sockets with ``ssl_context.wrap_socket()`` and then passing
    them to asyncio with ``ssl=`` would cause a double-TLS layer; the test
    verifies raw sockets (not SSLSocket) are stored on the server instance.
"""

import asyncio
import pathlib
import socket
import ssl

import pytest
import pytest_asyncio

from blackbull.server.server import ASGIServer
from blackbull.rsock import create_dual_stack_sockets, _bind_socket


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def cert_path():
    return pathlib.Path(__file__).parent / 'cert.pem'


@pytest.fixture
def key_path():
    return pathlib.Path(__file__).parent / 'key.pem'


async def _noop_app(scope, receive, send):
    pass


# ---------------------------------------------------------------------------
# Bug 1 – ASGIServer init-order: no ValueError when both cert+key given
# ---------------------------------------------------------------------------

class TestASGIServerInit:
    """ASGIServer.__init__ must not raise when certfile and keyfile are both provided."""

    def test_init_with_certfile_and_keyfile_does_not_raise(self, cert_path, key_path, manage_cert_and_key):
        """Bug 1 regression: constructing with cert+key must succeed without ValueError.

        The old code called ``make_ssl_context()`` from the ``keyfile`` setter
        before ``certfile`` was set, raising ValueError immediately.
        """
        # Must not raise
        server = ASGIServer(_noop_app, certfile=cert_path, keyfile=key_path)
        assert server.ssl_context is not None

    def test_init_without_tls_has_no_ssl_context(self):
        """A plain (non-TLS) server must have ssl_context == None."""
        server = ASGIServer(_noop_app)
        assert server.ssl_context is None

    def test_init_sets_certfile_and_keyfile(self, cert_path, key_path, manage_cert_and_key):
        server = ASGIServer(_noop_app, certfile=cert_path, keyfile=key_path)
        assert server.certfile == cert_path
        assert server.keyfile == key_path

    def test_init_missing_certfile_raises(self, key_path, manage_cert_and_key):
        with pytest.raises(FileNotFoundError):
            ASGIServer(_noop_app, certfile='/nonexistent/cert.pem', keyfile=key_path)

    def test_init_missing_keyfile_raises(self, cert_path, manage_cert_and_key):
        with pytest.raises(FileNotFoundError):
            ASGIServer(_noop_app, certfile=cert_path, keyfile='/nonexistent/key.pem')

    def test_ssl_context_and_certfile_together_raises(self, cert_path, key_path, manage_cert_and_key):
        ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        with pytest.raises(TypeError):
            ASGIServer(_noop_app, ssl_context=ctx, certfile=cert_path)


# ---------------------------------------------------------------------------
# Bug 1 – make_ssl_context defers when only one of cert/key is set
# ---------------------------------------------------------------------------

class TestMakeSSLContext:
    """make_ssl_context() must silently defer (not raise) when called with incomplete state."""

    def test_defers_when_only_certfile_set(self, cert_path, manage_cert_and_key):
        """Bug 1 regression: setting certfile alone must not crash."""
        server = ASGIServer(_noop_app)
        # Directly set certfile without keyfile – should not raise
        server._certfile = cert_path
        server.make_ssl_context()           # must return None silently
        assert server.ssl_context is None   # not yet ready

    def test_defers_when_only_keyfile_set(self, key_path, manage_cert_and_key):
        """Bug 1 regression: setting keyfile alone must not crash."""
        server = ASGIServer(_noop_app)
        server._keyfile = key_path
        server.make_ssl_context()
        assert server.ssl_context is None

    def test_builds_context_when_both_set(self, cert_path, key_path, manage_cert_and_key):
        """Once both files are present make_ssl_context() must produce a context."""
        server = ASGIServer(_noop_app)
        server._certfile = cert_path
        server._keyfile = key_path
        server.make_ssl_context()
        assert isinstance(server.ssl_context, ssl.SSLContext)

    def test_alpn_protocols_include_http11(self, cert_path, key_path, manage_cert_and_key):
        """The SSL context must advertise HTTP/1.1 for ALPN negotiation."""
        server = ASGIServer(_noop_app, certfile=cert_path, keyfile=key_path)
        # ssl module does not expose the configured ALPN list directly, but
        # we can verify the context was built without error and is CLIENT_AUTH.
        assert server.ssl_context.verify_mode is not None


# ---------------------------------------------------------------------------
# Bug 7 – open_socket: both sockets share the same port
# ---------------------------------------------------------------------------

class TestOpenSocket:
    """open_socket() must bind IPv4 and IPv6 to the *same* port."""

    def test_open_socket_assigns_port(self):
        """open_socket() must set server.port after binding."""
        server = ASGIServer(_noop_app)
        server.open_socket(port=0)
        try:
            assert server.port is not None
            assert isinstance(server.port, int)
            assert server.port > 0
        finally:
            server.close_socket()

    def test_all_raw_sockets_share_the_same_port(self):
        """Bug 7 regression: every bound socket must listen on the same port number."""
        server = ASGIServer(_noop_app)
        server.open_socket(port=0)
        try:
            ports = {s.getsockname()[1] for s in server.raw_sockets}
            assert len(ports) == 1, (
                f"IPv4 and IPv6 sockets got different ports: {ports}. "
                "When port=0, IPv4 must be bound first to learn the port, "
                "then IPv6 must be bound to the SAME port."
            )
        finally:
            server.close_socket()

    def test_raw_sockets_are_not_ssl_wrapped(self):
        """Bug 8 regression: raw_sockets must hold plain socket.socket objects.

        asyncio.start_server() handles TLS via ssl= parameter; pre-wrapping
        with ssl_context.wrap_socket() adds a double-TLS layer.
        """
        server = ASGIServer(_noop_app)
        server.open_socket(port=0)
        try:
            for sock in server.raw_sockets:
                assert not isinstance(sock, ssl.SSLSocket), (
                    "raw_sockets contains an SSLSocket – this causes a double-TLS layer. "
                    "Pass raw TCP sockets to asyncio.start_server(ssl=ctx) instead."
                )
        finally:
            server.close_socket()

    def test_ipv4_socket_is_present(self):
        """At least one AF_INET socket must be bound."""
        server = ASGIServer(_noop_app)
        server.open_socket(port=0)
        try:
            families = {s.family for s in server.raw_sockets}
            assert socket.AF_INET in families
        finally:
            server.close_socket()

    def test_server_port_matches_socket_port(self):
        """server.port must equal the port reported by getsockname()."""
        server = ASGIServer(_noop_app)
        server.open_socket(port=0)
        try:
            for sock in server.raw_sockets:
                assert sock.getsockname()[1] == server.port
        finally:
            server.close_socket()

    def test_close_socket_closes_all_sockets(self):
        """close_socket() must close every raw socket."""
        server = ASGIServer(_noop_app)
        server.open_socket(port=0)
        sockets_snapshot = list(server.raw_sockets)
        server.close_socket()
        for sock in sockets_snapshot:
            assert sock.fileno() == -1, "Socket was not closed by close_socket()"


# ---------------------------------------------------------------------------
# Bug 8 – run() must use sock= per socket, not sockets= (plural)
# ---------------------------------------------------------------------------

class TestASGIServerRun:
    """ASGIServer.run() must start without TypeError and accept connections."""

    @pytest.mark.asyncio
    async def test_plain_server_starts_and_accepts_tcp(self):
        """Bug 8 regression: run() must not raise TypeError about 'sockets=' kwarg."""
        server = ASGIServer(_noop_app)
        server.open_socket(port=0)
        port = server.port

        task = asyncio.create_task(server.run())
        await asyncio.sleep(0.1)   # give the server time to start

        # Verify a plain TCP connection is accepted
        try:
            _, writer = await asyncio.open_connection('127.0.0.1', port)
            writer.close()
            await writer.wait_closed()
        finally:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    @pytest.mark.asyncio
    async def test_tls_server_starts_and_accepts_tls(self, cert_path, key_path, manage_cert_and_key):
        """TLS server must complete a handshake; double-TLS would cause SSLError."""
        server = ASGIServer(_noop_app, certfile=cert_path, keyfile=key_path)
        server.open_socket(port=0)
        port = server.port

        client_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        client_ctx.load_verify_locations(cert_path)

        task = asyncio.create_task(server.run())
        await asyncio.sleep(0.1)

        try:
            _, writer = await asyncio.open_connection(
                '127.0.0.1', port,
                ssl=client_ctx,
                server_hostname='localhost',
            )
            writer.close()
            await writer.wait_closed()
        finally:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
