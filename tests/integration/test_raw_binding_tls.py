"""Integration tests for TLS on raw protocol bindings (Sprint 75).

A raw binding registered with ``tls=True`` is served through the same TLS
machinery as the HTTPS listener (``mqtts://``-style deployments).  Bindings
default to cleartext, so existing registrations are unaffected — including on
an app that has certificates configured for HTTPS.
"""
import asyncio
import socket
import ssl
import time
from pathlib import Path
from multiprocessing import Process

import pytest

from blackbull import BlackBull
from blackbull.server import Server

CERT = str(Path(__file__).parent.parent / 'cert.pem')
KEY = str(Path(__file__).parent.parent / 'key.pem')


def _make_app():
    app = BlackBull()

    @app.route(path='/')
    async def index():
        return "http-ok"

    @app.raw_handler('echo-tls', port=0, tls=True)
    async def echo_tls(reader, writer, ctx):
        while True:
            data = await reader.read(1024)
            if not data:
                break
            await writer.write(data)

    @app.raw_handler('echo-clear', port=0)
    async def echo_clear(reader, writer, ctx):
        while True:
            data = await reader.read(1024)
            if not data:
                break
            await writer.write(data)

    return app


@pytest.fixture
def live_tls_bridge():
    """HTTPS + one TLS and one cleartext raw binding on ephemeral ports."""
    app = _make_app()
    server = Server(app, certfile=CERT, keyfile=KEY)
    server.open_socket(0)
    tls_port = server.protocol_ports['echo-tls']
    clear_port = server.protocol_ports['echo-clear']
    p = Process(target=lambda: asyncio.run(server.run()))
    p.start()
    try:
        server.wait_for_port(timeout=10.0)
        yield tls_port, clear_port
    finally:
        server.close()
        p.terminate()
        p.join(timeout=5)


def _connect(port, timeout=5.0):
    deadline = time.time() + timeout
    while True:
        try:
            return socket.create_connection(('127.0.0.1', port), timeout=2)
        except OSError:
            if time.time() >= deadline:
                raise
            time.sleep(0.05)


def _tls_connect(port):
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    sock = _connect(port)
    return ctx.wrap_socket(sock, server_hostname='localhost')


def test_tls_raw_echo_round_trips(live_tls_bridge):
    tls_port, _ = live_tls_bridge
    with _tls_connect(tls_port) as tsock:
        tsock.sendall(b'hello mqtts\n')
        assert tsock.recv(1024) == b'hello mqtts\n'


def test_cleartext_to_tls_port_fails(live_tls_bridge):
    """A plain-TCP client on a tls=True port never reaches the handler."""
    tls_port, _ = live_tls_bridge
    with _connect(tls_port) as sock:
        sock.sendall(b'not a tls hello\n')
        # The handshake fails server-side; the client sees a close (b'') or a
        # reset — never an echo of its bytes.
        try:
            data = sock.recv(1024)
        except OSError:
            data = b''
    assert data != b'not a tls hello\n'


def test_cleartext_binding_stays_cleartext_alongside_tls(live_tls_bridge):
    """tls=False (the default) keeps serving plain TCP even when the app has
    certificates configured — no behaviour change for existing registrations."""
    _, clear_port = live_tls_bridge
    with _connect(clear_port) as sock:
        sock.sendall(b'still cleartext\n')
        assert sock.recv(1024) == b'still cleartext\n'


def test_tls_binding_without_certs_fails_fast():
    """tls=True on a server with no TLS configured raises at serve time."""
    app = BlackBull()

    @app.raw_handler('needs-tls', port=0, tls=True)
    async def h(reader, writer, ctx):
        pass  # never reached — the server must refuse to start

    server = Server(app)
    server.open_socket(0)
    try:
        with pytest.raises(RuntimeError, match='needs-tls'):
            asyncio.run(asyncio.wait_for(server.run(), timeout=5))
    finally:
        server.close()
