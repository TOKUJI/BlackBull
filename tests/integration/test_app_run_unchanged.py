"""``app.run(port=...)`` behaves exactly as it did before listeners existed.

The rework replaced four binding paths with one and made the socket the unit a
deployment states.  The overwhelmingly common case says none of that — it says
a port, and sometimes a worker count — and it is what
``docs/getting-started/`` teaches.  These tests pin what that call produces, so
the vocabulary underneath can keep moving without moving it.

Gate 3 of `.claude/planning/designs/listener-vocabulary.md`.
"""
from __future__ import annotations

import asyncio
import http.client
import pathlib
import socket
import ssl

import pytest

from blackbull import BlackBull
from blackbull.server.listener import HTTP, Tcp, Unix
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


class TestWhatOnePortProduces:
    def test_a_port_is_one_cleartext_http_listener_on_every_worker(self, app):
        server = Server(app)
        server.open_socket(0)
        try:
            assert len(server.bound_listeners) == 1
            listener, socks = server.bound_listeners[0]
            assert listener.speaks == HTTP
            assert listener.tls is None
            assert listener.workers == 'all'
            assert isinstance(listener.where, Tcp)
            assert server.port == socks[0].getsockname()[1]
            assert server.unix_path is None
        finally:
            server.close_socket()

    def test_a_certificate_makes_that_one_listener_tls(self, app):
        server = Server(app, certfile=str(CERT), keyfile=str(KEY))
        server.open_socket(0)
        try:
            listener, _socks = server.bound_listeners[0]
            assert listener.tls is server.ssl_context
            assert listener.workers == 'all'
        finally:
            server.close_socket()

    def test_a_unix_path_is_one_listener_too(self, app, tmp_path):
        path = str(tmp_path / 'bb.sock')
        server = Server(app)
        server.open_socket(unix_path=path)
        try:
            listener, _socks = server.bound_listeners[0]
            assert isinstance(listener.where, Unix)
            assert server.unix_path == path
            assert server.port is None
        finally:
            server.close_socket()

    def test_raw_sockets_is_still_dual_stack(self, app):
        """What the multi-worker master hands to each worker, unchanged."""
        server = Server(app)
        server.open_socket(0)
        try:
            families = {sock.family for sock in server.raw_sockets}
            assert socket.AF_INET in families
            assert all(sock.getsockname()[1] == server.port
                       for sock in server.raw_sockets)
        finally:
            server.close_socket()


class TestWhatOnePortServes:
    @staticmethod
    async def _serve_and_get(server, url, context=None):
        task = asyncio.create_task(server.run())
        await asyncio.sleep(0.5)
        try:
            def _get():
                return __import__('urllib.request', fromlist=['request']).urlopen(
                    url, timeout=10, context=context).read()
            return await asyncio.to_thread(_get)
        finally:
            await server.stop(drain_timeout=1.0)
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
            server.close_socket()

    @pytest.mark.asyncio
    async def test_cleartext(self, app):
        server = Server(app)
        server.open_socket(0)
        body = await self._serve_and_get(
            server, f'http://127.0.0.1:{server.port}/')
        assert body == b'ok'

    @pytest.mark.asyncio
    async def test_https(self, app):
        server = Server(app, certfile=str(CERT), keyfile=str(KEY))
        server.open_socket(0)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        body = await self._serve_and_get(
            server, f'https://127.0.0.1:{server.port}/', context=ctx)
        assert body == b'ok'


def test_workers_still_share_the_one_port(app):
    """``app.run(port=..., workers=N)``: one port, answered by several."""
    from blackbull.server.multiworker import MultiWorkerServer

    @app.route(path='/pid')
    async def pid(conn):
        import os
        return str(os.getpid())

    master = Server(app)
    master.open_socket(0)
    port = master.port
    mws = MultiWorkerServer(app, master.bound_listeners, None, workers=3)
    mws._spawn_all()

    import time
    pids = set()
    try:
        deadline = time.time() + 15
        while time.time() < deadline and len(pids) < 3:
            try:
                conn = http.client.HTTPConnection('127.0.0.1', port, timeout=2)
                conn.request('GET', '/pid')
                pids.add(conn.getresponse().read().decode())
                conn.close()
            except OSError:
                time.sleep(0.02)
    finally:
        mws._shutdown_all()
        master.close_socket()

    assert len(pids) == 3, f'every worker must serve the one port, saw {pids}'


class TestListenersReachTheEntryPoint:
    """A capability the documented entry point cannot say is not delivered."""

    def test_run_and_serve_both_accept_listeners(self):
        import inspect
        from blackbull import serve
        assert 'listeners' in inspect.signature(BlackBull.run).parameters
        assert 'listeners' in inspect.signature(serve).parameters

    def test_the_vocabulary_is_importable_from_the_package(self):
        from blackbull import InheritedFd, Listener, Tcp, Unix  # noqa: F401

    def test_saying_it_twice_is_refused(self, app):
        from blackbull import Listener as L, Tcp as T, serve
        with pytest.raises(TypeError, match='not both'):
            serve(app, port=8000, listeners=[L(T(0))])

    @pytest.mark.asyncio
    async def test_four_ports_from_one_call(self, app):
        """The deployment gate 2 is about: four listeners, one process."""
        from blackbull import Listener, Tcp
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(certfile=str(CERT), keyfile=str(KEY))
        ctx.set_alpn_protocols(['h2', 'http/1.1'])

        server = Server(app, listeners=[
            Listener(Tcp(0)), Listener(Tcp(0)),
            Listener(Tcp(0), tls=ctx), Listener(Tcp(0), tls=ctx),
        ])
        server.open_socket()
        ports = [(socks[0].getsockname()[1], listener.tls is not None)
                 for listener, socks in server.bound_listeners]
        assert len({port for port, _tls in ports}) == 4

        client_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        client_ctx.check_hostname = False
        client_ctx.verify_mode = ssl.CERT_NONE
        task = asyncio.create_task(server.run())
        await asyncio.sleep(0.5)
        try:
            import urllib.request
            for port, tls in ports:
                scheme = 'https' if tls else 'http'
                body = await asyncio.to_thread(
                    lambda p=port, s=scheme, t=tls: urllib.request.urlopen(
                        f'{s}://127.0.0.1:{p}/', timeout=10,
                        context=client_ctx if t else None).read())
                assert body == b'ok', f'{scheme} port {port}'
        finally:
            await server.stop(drain_timeout=1.0)
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
            server.close_socket()
