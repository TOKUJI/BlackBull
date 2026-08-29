"""TLS belongs to the listener, not to the server.

The defect this closes: ``Server`` held one ``ssl_context`` that applied to
its one HTTP listener, so configuring a certificate silently turned that port
into HTTPS and "cleartext here, TLS there" was inexpressible — which is why a
deployment needing both ran one process per port.

Design: `BLA-A-17` [private].
"""
from __future__ import annotations

import asyncio
import pathlib
import ssl
import urllib.error
import urllib.request

import pytest

from blackbull import BlackBull
from blackbull.server.listener import Listener, Tcp
from blackbull.server.server import Server

CERT = pathlib.Path(__file__).parent.parent / 'cert.pem'
KEY = pathlib.Path(__file__).parent.parent / 'key.pem'


@pytest.fixture
def app():
    app = BlackBull()

    @app.route(path='/')
    async def index(conn):
        return 'ok'

    return app


def server_context() -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile=str(CERT), keyfile=str(KEY))
    ctx.set_alpn_protocols(['h2', 'http/1.1'])
    return ctx


def trusting_client_context() -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def ports_of(server, index):
    listener, socks = server.bound_listeners[index]
    return socks[0].getsockname()[1]


async def fetch(url: str, *, context: ssl.SSLContext | None = None) -> bytes:
    def _get():
        return urllib.request.urlopen(url, timeout=10, context=context).read()
    return await asyncio.to_thread(_get)


class _Running:
    """Start a Server on already-bound listeners and stop it cleanly."""

    def __init__(self, server):
        self.server = server

    async def __aenter__(self):
        self._task = asyncio.create_task(self.server.run())
        await asyncio.sleep(0.5)
        return self.server

    async def __aexit__(self, *exc):
        await self.server.stop(drain_timeout=1.0)
        self._task.cancel()
        try:
            await self._task
        except (asyncio.CancelledError, Exception):
            pass
        self.server.close_socket()
        return False


@pytest.mark.asyncio
async def test_cleartext_and_tls_in_one_process(app):
    """The posture that used to cost a second process."""
    server = Server(app, listeners=[
        Listener(Tcp(0)),
        Listener(Tcp(0), tls=server_context()),
    ])
    server.open_socket()
    clear, secure = ports_of(server, 0), ports_of(server, 1)
    assert clear != secure

    async with _Running(server):
        assert await fetch(f'http://127.0.0.1:{clear}/') == b'ok'
        assert await fetch(f'https://127.0.0.1:{secure}/',
                           context=trusting_client_context()) == b'ok'


@pytest.mark.asyncio
async def test_a_certificate_on_the_server_no_longer_converts_a_listener(app):
    """A listener is TLS because it says so, not because a certificate exists."""
    server = Server(app, certfile=str(CERT), keyfile=str(KEY),
                    listeners=[Listener(Tcp(0))])
    server.open_socket()
    port = ports_of(server, 0)

    async with _Running(server):
        assert await fetch(f'http://127.0.0.1:{port}/') == b'ok'


@pytest.mark.asyncio
async def test_two_listeners_can_present_different_certificates(app):
    """What a server-wide context could not say at all."""
    first, second = server_context(), server_context()
    server = Server(app, listeners=[
        Listener(Tcp(0), tls=first),
        Listener(Tcp(0), tls=second),
    ])
    server.open_socket()
    try:
        contexts = [listener.tls for listener, _socks in server.bound_listeners]
        assert contexts[0] is first
        assert contexts[1] is second
        assert contexts[0] is not contexts[1]
    finally:
        server.close_socket()


@pytest.mark.asyncio
async def test_the_certificate_arguments_still_serve_https(app):
    """``app.run(certfile=..., keyfile=...)`` keeps meaning HTTPS."""
    server = Server(app, certfile=str(CERT), keyfile=str(KEY))
    server.open_socket(0)
    port = server.port

    async with _Running(server):
        assert await fetch(f'https://127.0.0.1:{port}/',
                           context=trusting_client_context()) == b'ok'
        with pytest.raises(Exception):
            await fetch(f'http://127.0.0.1:{port}/')


@pytest.mark.asyncio
async def test_without_a_certificate_the_legacy_path_is_cleartext(app):
    server = Server(app)
    server.open_socket(0)
    port = server.port

    async with _Running(server):
        assert await fetch(f'http://127.0.0.1:{port}/') == b'ok'
