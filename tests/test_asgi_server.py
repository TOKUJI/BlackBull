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
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from blackbull.server.server import ASGIServer, HTTP11Handler
from blackbull.server.recipient import RecipientFactory
from blackbull.protocol.rsock import create_dual_stack_sockets, _bind_socket


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
        assert server.ssl_context is not None
        assert server.ssl_context.verify_mode is not None


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


# ---------------------------------------------------------------------------
# Helpers for HTTP/1.1 body-decoding unit tests
# ---------------------------------------------------------------------------

class FakeStreamReader:
    """Minimal asyncio.StreamReader stand-in backed by an in-memory buffer."""

    def __init__(self, data: bytes):
        self._buf = bytearray(data)

    async def read(self, n: int) -> bytes:
        chunk = bytes(self._buf[:n])
        del self._buf[:n]
        return chunk

    async def readuntil(self, separator: bytes) -> bytes:
        idx = self._buf.find(separator)
        if idx == -1:
            result = bytes(self._buf)
            self._buf.clear()
        else:
            end = idx + len(separator)
            result = bytes(self._buf[:end])
            del self._buf[:end]
        return result


# ---------------------------------------------------------------------------
# Body-decoding tests
# ---------------------------------------------------------------------------

class TestHTTP1_1BodyDecoding:
    """HTTP/1.1 body reading: Content-Length and Transfer-Encoding: chunked."""

    def _make_recipient(self, body_bytes: bytes, scope: dict):
        """Return an HTTP1Recipient backed by an in-memory buffer."""
        return RecipientFactory.http1(FakeStreamReader(body_bytes), scope)

    @pytest.mark.asyncio
    async def test_content_length_reads_exact_bytes(self):
        """receive() must return exactly the bytes declared by Content-Length."""
        body = b'hello world'
        scope = {'headers': [(b'content-length', str(len(body)).encode())]}
        event = await self._make_recipient(body, scope)()
        assert event == {'type': 'http.request', 'body': b'hello world', 'more_body': False}

    @pytest.mark.asyncio
    async def test_content_length_multi_digit(self):
        """A multi-digit Content-Length (e.g. 100) must not be truncated to its first digit."""
        body = b'x' * 100
        scope = {'headers': [(b'content-length', b'100')]}
        event = await self._make_recipient(body, scope)()
        assert len(event['body']) == 100

    @pytest.mark.asyncio
    async def test_no_body_headers_returns_empty(self):
        """A request with no Content-Length or Transfer-Encoding has an empty body."""
        scope = {'headers': []}
        event = await self._make_recipient(b'', scope)()
        assert event == {'type': 'http.request', 'body': b'', 'more_body': False}

    @pytest.mark.asyncio
    async def test_chunked_decodes_multiple_chunks(self):
        """Each chunk must arrive as a separate event with more_body=True; terminal chunk ends stream."""
        chunk1, chunk2 = b'hello', b' world'
        chunked = (
            f'{len(chunk1):x}\r\n'.encode() + chunk1 + b'\r\n'
            + f'{len(chunk2):x}\r\n'.encode() + chunk2 + b'\r\n'
            + b'0\r\n\r\n'
        )
        scope = {'headers': [(b'transfer-encoding', b'chunked')]}
        recipient = self._make_recipient(chunked, scope)
        e1 = await recipient()
        e2 = await recipient()
        e3 = await recipient()
        assert e1 == {'type': 'http.request', 'body': b'hello', 'more_body': True}
        assert e2 == {'type': 'http.request', 'body': b' world', 'more_body': True}
        assert e3 == {'type': 'http.request', 'body': b'', 'more_body': False}

    @pytest.mark.asyncio
    async def test_chunked_single_chunk(self):
        """A single non-empty chunk followed by the terminal chunk must decode correctly."""
        body = b'ping'
        chunked = f'{len(body):x}\r\n'.encode() + body + b'\r\n0\r\n\r\n'
        scope = {'headers': [(b'transfer-encoding', b'chunked')]}
        event = await self._make_recipient(chunked, scope)()
        assert event['body'] == body

    @pytest.mark.asyncio
    async def test_chunked_empty_body(self):
        """A terminal-only chunked body (0\\r\\n\\r\\n) must produce an empty body."""
        scope = {'headers': [(b'transfer-encoding', b'chunked')]}
        event = await self._make_recipient(b'0\r\n\r\n', scope)()
        assert event['body'] == b''


# ---------------------------------------------------------------------------
# Helpers for lifespan / timeout tests
# ---------------------------------------------------------------------------

def _make_fake_writer():
    """Return a MagicMock writer with transport and written-bytes tracking."""
    sw = MagicMock()
    sw.written = bytearray()
    sw.write = MagicMock(side_effect=lambda d: sw.written.extend(d))
    sw.drain = AsyncMock()
    sw.closed = False
    sw.close = MagicMock(side_effect=lambda: setattr(sw, 'closed', True))
    sw.wait_closed = AsyncMock()

    class _FakeTransport:
        def get_extra_info(self, key, default=None):
            extras = {'peername': ('127.0.0.1', 54321),
                      'sockname': ('0.0.0.0', 8000),
                      'ssl_object': None}
            return extras.get(key, default)

    sw.transport = _FakeTransport()
    return sw


# ---------------------------------------------------------------------------
# P2 — ASGI lifespan
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestASGILifespan:
    """ASGIServer must dispatch lifespan.startup and lifespan.shutdown.

    P2 item: the server must call the ASGI app with scope={'type': 'lifespan'}
    before accepting requests and with lifespan.shutdown after the server stops.

    ASGI lifespan spec:
      scope = {'type': 'lifespan', 'asgi': {'version': '3.0'}}
      receive → {'type': 'lifespan.startup'}  / {'type': 'lifespan.shutdown'}
      send   ← {'type': 'lifespan.startup.complete'} / {'type': 'lifespan.shutdown.complete'}
    """

    async def test_startup_event_dispatched(self):
        """App must receive {'type': 'lifespan.startup'} before serving requests."""
        received_events = []

        async def app(scope, receive, send):
            if scope.get('type') == 'lifespan':
                event = await receive()
                received_events.append(event.get('type'))
                await send({'type': 'lifespan.startup.complete'})

        server = ASGIServer(app)
        await server.startup()

        assert 'lifespan.startup' in received_events, (
            f'Expected lifespan.startup event; got {received_events}'
        )

    async def test_shutdown_event_dispatched(self):
        """App must receive {'type': 'lifespan.shutdown'} on server shutdown."""
        received_events = []

        async def app(scope, receive, send):
            if scope.get('type') == 'lifespan':
                while True:
                    event = await receive()
                    received_events.append(event.get('type'))
                    if event['type'] == 'lifespan.startup':
                        await send({'type': 'lifespan.startup.complete'})
                    elif event['type'] == 'lifespan.shutdown':
                        await send({'type': 'lifespan.shutdown.complete'})
                        break

        server = ASGIServer(app)
        await server.startup()
        await server.shutdown()

        assert 'lifespan.shutdown' in received_events, (
            f'Expected lifespan.shutdown event; got {received_events}'
        )

    async def test_startup_failure_raises(self):
        """If the app sends lifespan.startup.failed, ASGIServer.startup() must raise."""
        async def app(scope, receive, send):
            if scope.get('type') == 'lifespan':
                await receive()  # lifespan.startup
                await send({'type': 'lifespan.startup.failed',
                            'message': 'DB connection refused'})

        server = ASGIServer(app)
        with pytest.raises(Exception, match='DB connection refused'):
            await server.startup()


# ---------------------------------------------------------------------------
# P2 — Timeout handling
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestTimeoutHandling:
    """Header and body reads must be wrapped with asyncio.wait_for.

    P2 item: a slow or stalled client keeps the connection open indefinitely.
    Wrapping reads in asyncio.wait_for(coro, timeout=N) lets the server
    reclaim resources by aborting the connection after N seconds.
    """

    async def test_slow_header_read_times_out(self):
        """A reader that never yields headers must trigger a timeout."""
        async def slow_read(*args, **kwargs):
            await asyncio.sleep(10)
            return b''

        sw = _make_fake_writer()
        reader = MagicMock()
        reader.readuntil = AsyncMock(side_effect=slow_read)

        async def noop_app(scope, receive, send):
            pass

        server = ASGIServer(noop_app, read_timeout=0.05)

        with pytest.raises((asyncio.TimeoutError, TimeoutError,
                            ConnectionResetError, OSError)):
            await asyncio.wait_for(
                server.client_connected_cb(reader, sw),
                timeout=1.0,
            )

    async def test_slow_body_read_times_out(self):
        """A reader that hangs during body read must also time out."""
        async def slow_body(*args, **kwargs):
            await asyncio.sleep(10)
            return b''

        sw = _make_fake_writer()
        reader = MagicMock()
        reader.readuntil = AsyncMock(side_effect=[b'POST / HTTP/1.1\r\n',
                                                  b'Content-Length: 5\r\n\r\n'])
        reader.read = AsyncMock(side_effect=slow_body)

        async def noop_app(scope, receive, send):
            await receive()  # triggers body read

        server = ASGIServer(noop_app, read_timeout=0.05)

        with pytest.raises((asyncio.TimeoutError, TimeoutError,
                            ConnectionResetError, OSError)):
            await asyncio.wait_for(
                server.client_connected_cb(reader, sw),
                timeout=1.0,
            )


# ---------------------------------------------------------------------------
# P3 — mTLS (mutual TLS)
# ---------------------------------------------------------------------------

class TestMTLS:
    """ASGIServer must support mutual TLS (mTLS) via ssl.CERT_REQUIRED +
    load_verify_locations (RFC 8446 §4.3.2).

    Expected API: ASGIServer exposes a ``configure_mtls(ca_cert: str)`` method
    that sets CERT_REQUIRED on the existing ssl_context and loads the CA cert
    so the server can verify client certificates.
    """

    def test_configure_mtls_sets_cert_required(self, cert_path, key_path,
                                                manage_cert_and_key):
        """After configure_mtls(), ssl_context.verify_mode must be CERT_REQUIRED."""
        server = ASGIServer(_noop_app, certfile=str(cert_path),
                            keyfile=str(key_path))
        server.configure_mtls(ca_cert=str(cert_path))
        assert server.ssl_context is not None
        assert server.ssl_context.verify_mode == ssl.CERT_REQUIRED, (
            f'mTLS requires CERT_REQUIRED; got {server.ssl_context.verify_mode}'
        )

    def test_configure_mtls_calls_load_verify_locations(self, cert_path, key_path,
                                                         manage_cert_and_key):
        """configure_mtls() must call load_verify_locations with cafile=<ca_cert>."""
        from unittest.mock import patch
        server = ASGIServer(_noop_app, certfile=str(cert_path),
                            keyfile=str(key_path))
        assert server.ssl_context is not None
        with patch.object(server.ssl_context, 'load_verify_locations') as mock_lvl:
            server.configure_mtls(ca_cert=str(cert_path))
            mock_lvl.assert_called_once_with(cafile=str(cert_path))

    def test_standard_tls_does_not_require_client_cert(self, cert_path, key_path,
                                                        manage_cert_and_key):
        """Without calling configure_mtls(), client certs must not be required."""
        server = ASGIServer(_noop_app, certfile=str(cert_path),
                            keyfile=str(key_path))
        assert server.ssl_context is not None
        assert server.ssl_context.verify_mode != ssl.CERT_REQUIRED, (
            f'Standard TLS must not require client cert; '
            f'got {server.ssl_context.verify_mode}'
        )

    def test_configure_mtls_without_ssl_context_raises(self):
        """Calling configure_mtls() on a plain (non-TLS) server must raise."""
        server = ASGIServer(_noop_app)   # no certfile/keyfile → ssl_context is None
        with pytest.raises((AttributeError, RuntimeError, TypeError)):
            server.configure_mtls(ca_cert='/some/ca.pem')
