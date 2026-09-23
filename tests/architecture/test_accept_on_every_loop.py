"""Accepting behaves the same on the default loop and on uvloop.

TLS here is anonymous Diffie-Hellman, TLS 1.2: ALPN and SNI work and no key
material is involved.  A stalled handshake needs no certificate at all.
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import socket
import ssl

import pytest

from blackbull import BlackBull
from blackbull.server.listener import Listener, Tcp
from blackbull.server.server import Server

LOOPS = ['selector', pytest.param('uvloop', marks=pytest.mark.skipif(
    importlib.util.find_spec('uvloop') is None, reason='uvloop not installed'))]

REQUEST = b'GET / HTTP/1.1\r\nhost: localhost\r\nconnection: close\r\n\r\n'


def _run(loop: str, coro_fn):
    if loop == 'uvloop':
        import uvloop
        return uvloop.run(coro_fn())
    return asyncio.run(coro_fn())


def _anonymous_tls(*, server: bool, alpn: list[str]) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER if server
                             else ssl.PROTOCOL_TLS_CLIENT)
    if not server:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    context.maximum_version = ssl.TLSVersion.TLSv1_2
    context.set_ciphers('aNULL:@SECLEVEL=0')
    context.set_alpn_protocols(alpn)
    return context


def _app() -> BlackBull:
    app = BlackBull()

    @app.route(path='/')
    async def index():
        return 'ok'

    return app


async def _until(predicate, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        assert asyncio.get_running_loop().time() < deadline, 'timed out'
        await asyncio.sleep(0.01)


async def _served(port: int, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        try:
            reader, writer = await asyncio.open_connection('127.0.0.1', port)
            writer.write(REQUEST)
            line = await asyncio.wait_for(reader.readline(), 2)
            writer.close()
            if line.startswith(b'HTTP/1.1 200'):
                return
        except OSError:
            pass
        assert asyncio.get_running_loop().time() < deadline, 'never served'
        await asyncio.sleep(0.02)


# ---------------------------------------------------------------------------
# stop() and connections still in their TLS handshake
# ---------------------------------------------------------------------------

@pytest.mark.timeout(60)
@pytest.mark.parametrize('loop', LOOPS)
def test_stop_closes_connections_still_in_their_tls_handshake(loop):
    async def main():
        server = Server(_app(), max_connections=64,
                        ssl_context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER))
        server.open_socket(0)
        runner = asyncio.create_task(server.run())
        silent = []
        try:
            await _until(lambda: getattr(server, '_running_servers', None))
            for _ in range(5):
                silent.append(socket.create_connection(('127.0.0.1', server.port)))
            await asyncio.sleep(0.5)   # every connect accepted, none answered
            await asyncio.wait_for(server.stop(drain_timeout=1.0), 5)
            assert server._accept_gate._descriptors_held == 0
            await asyncio.wait_for(runner, 5)

            for sock in silent:
                sock.settimeout(2)
                assert sock.recv(1) == b'', 'the server kept a handshake open'
            assert server._accept_gate._descriptors_held == 0
        finally:
            for sock in silent:
                sock.close()
            if not runner.done():
                runner.cancel()
    _run(loop, main)


# ---------------------------------------------------------------------------
# TLS on both loops: ALPN picks the stack
# ---------------------------------------------------------------------------

H2_PREFACE = b'PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n' + bytes(
    [0, 0, 0, 4, 0, 0, 0, 0, 0])   # an empty SETTINGS frame


@pytest.mark.timeout(60)
@pytest.mark.parametrize('loop', LOOPS)
def test_alpn_selects_http2_or_http1_over_tls(loop):
    async def main():
        server = Server(_app(), max_connections=64,
                        ssl_context=_anonymous_tls(server=True,
                                                   alpn=['h2', 'http/1.1']))
        server.open_socket(0)
        runner = asyncio.create_task(server.run())
        try:
            await _until(lambda: getattr(server, '_running_servers', None))

            reader, writer = await asyncio.open_connection(
                '127.0.0.1', server.port,
                ssl=_anonymous_tls(server=False, alpn=['h2']))
            writer.write(H2_PREFACE)
            head = await asyncio.wait_for(reader.readexactly(9), 5)
            writer.close()
            assert head[3] == 0x4, f'no SETTINGS frame first: {head!r}'

            reader, writer = await asyncio.open_connection(
                '127.0.0.1', server.port,
                ssl=_anonymous_tls(server=False, alpn=['http/1.1']))
            writer.write(REQUEST)
            line = await asyncio.wait_for(reader.readline(), 5)
            writer.close()
            assert line.startswith(b'HTTP/1.1 200'), line
        finally:
            await asyncio.wait_for(server.stop(drain_timeout=1.0), 5)
            await asyncio.wait({runner}, timeout=5)
            server.close_socket()
    _run(loop, main)


# ---------------------------------------------------------------------------
# A listener's reader goes before its descriptor
# ---------------------------------------------------------------------------

def test_control_a_reader_left_on_a_closed_descriptor_fires_on_its_reuse():
    """What the next test guards against, shown to be observable."""
    pytest.importorskip('uvloop')
    import uvloop

    async def main():
        loop = asyncio.get_running_loop()
        fired = []
        first = socket.socket()
        first.bind(('127.0.0.1', 0))
        first.listen()
        fd = first.fileno()
        loop.add_reader(fd, lambda: fired.append(1))
        first.close()
        second = socket.socket()
        second.bind(('127.0.0.1', 0))
        second.listen()
        client = None
        try:
            assert second.fileno() == fd
            client = socket.create_connection(second.getsockname())
            await asyncio.sleep(0.1)
        finally:
            loop.remove_reader(fd)
            second.close()
            if client is not None:
                client.close()
        assert fired, 'the positive control did not register'
    uvloop.run(main())


@pytest.mark.timeout(60)
@pytest.mark.parametrize('loop', LOOPS)
@pytest.mark.parametrize('state', ['accepting', 'paused'])
def test_a_cancelled_run_leaves_no_reader_on_a_reused_descriptor(loop, state):
    async def main():
        errors = []
        asyncio.get_running_loop().set_exception_handler(
            lambda _loop, context: errors.append(context))
        server = Server(_app(), max_connections=1, listeners=[
            Listener(Tcp(0, host='127.0.0.1')) for _ in range(2)])
        server.open_socket()
        fds = [socks[0].fileno() for _l, socks in server.bound_listeners]
        port = server.bound_listeners[0][1][0].getsockname()[1]
        runner = asyncio.create_task(server.run())
        idle = []
        await _served(port)
        if state == 'paused':
            limit = server._accept_gate._limit
            while server._accept_gate._descriptors_held < limit:
                idle.append(socket.create_connection(('127.0.0.1', port)))
                await asyncio.sleep(0.01)
            assert server._accept_gate._paused
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)
        for sock in idle:
            sock.close()

        # A second server on the same descriptor numbers must accept: a
        # reader or selector key left on them would take its connections or
        # swallow its registration.
        again = Server(_app(), max_connections=1, listeners=[
            Listener(Tcp(0, host='127.0.0.1')) for _ in range(2)])
        again.open_socket()
        assert sorted(socks[0].fileno()
                      for _l, socks in again.bound_listeners) == sorted(fds)
        second = asyncio.create_task(again.run())
        try:
            for _l, socks in again.bound_listeners:
                await _served(socks[0].getsockname()[1])
        finally:
            await asyncio.wait_for(again.stop(drain_timeout=1.0), 5)
            await asyncio.wait({second}, timeout=5)
            again.close_socket()
        assert errors == []
    _run(loop, main)


# ---------------------------------------------------------------------------
# The socket file of an AF_UNIX listener
# ---------------------------------------------------------------------------

@pytest.mark.timeout(60)
@pytest.mark.parametrize('loop', LOOPS)
@pytest.mark.parametrize('rebound', [False, True], ids=['own', 'rebound'])
def test_stop_removes_the_socket_file_it_bound_and_no_other(loop, rebound, tmp_path):
    path = tmp_path / 'served.sock'

    async def main():
        server = Server(_app())
        server.open_socket(unix_path=str(path))
        runner = asyncio.create_task(server.run())
        other = None
        try:
            await _until(lambda: getattr(server, '_running_servers', None))
            if rebound:
                path.unlink()
                other = socket.socket(socket.AF_UNIX)
                other.bind(str(path))
            await asyncio.wait_for(server.stop(drain_timeout=1.0), 5)
            await asyncio.wait_for(runner, 5)
            assert path.exists() == rebound
        finally:
            if other is not None:
                other.close()
    _run(loop, main)


@pytest.mark.timeout(60)
@pytest.mark.parametrize('loop', LOOPS)
def test_stop_leaves_an_adopted_socket_file_to_its_owner(loop, tmp_path):
    """systemd keeps listening on the path after the service stops."""
    path = tmp_path / 'activated.sock'
    owner = socket.socket(socket.AF_UNIX)
    owner.bind(str(path))
    owner.listen()

    async def main():
        server = Server(_app())
        server.open_socket(inherited_fd=os.dup(owner.fileno()))
        runner = asyncio.create_task(server.run())
        await _until(lambda: getattr(server, '_running_servers', None))
        await asyncio.wait_for(server.stop(drain_timeout=1.0), 5)
        await asyncio.wait_for(runner, 5)
        server.close_socket()

    try:
        _run(loop, main)
        assert path.exists(), 'stop removed a socket file it did not bind'
    finally:
        owner.close()


@pytest.mark.timeout(60)
@pytest.mark.parametrize('loop', LOOPS)
def test_a_worker_leaves_the_masters_socket_file(loop, tmp_path, monkeypatch):
    """A worker that stops or respawns must not unlink the path the master and
    its siblings still listen on; the master's own shutdown does."""
    import multiprocessing
    import signal
    import time

    from blackbull.env import reset_settings_cache
    from blackbull.server.worker import run_worker

    monkeypatch.setenv('BB_UVLOOP', '1' if loop == 'uvloop' else '0')
    reset_settings_cache()
    path = tmp_path / 'shared.sock'
    master = Server(_app())
    master.open_socket(unix_path=str(path))
    worker = multiprocessing.get_context('fork').Process(
        target=run_worker, args=(_app(), master.bound_listeners, None, 0, 64))
    worker.start()
    try:
        deadline = time.monotonic() + 10
        while True:
            try:
                with socket.socket(socket.AF_UNIX) as client:
                    client.settimeout(2)
                    client.connect(str(path))
                    client.sendall(REQUEST)
                    if client.recv(12) == b'HTTP/1.1 200':
                        break
            except OSError:
                pass
            assert time.monotonic() < deadline, 'the worker never served'
            time.sleep(0.05)
        worker.terminate()          # SIGTERM: the worker's graceful stop
        worker.join(10)
        assert worker.exitcode is not None
        assert path.exists(), "a worker's stop removed the master's socket file"
    finally:
        if worker.is_alive():
            os.kill(worker.pid, signal.SIGKILL)
            worker.join()
        master.close_socket()
        reset_settings_cache()
    assert not path.exists(), "the master's shutdown left its socket file"


@pytest.mark.timeout(60)
@pytest.mark.parametrize('loop', LOOPS)
def test_an_adopted_fd_takes_the_configured_backlog_once_accepting_opens(
        loop, tmp_path, monkeypatch):
    from blackbull.env import get_settings, reset_settings_cache
    from blackbull.protocol import rsock

    monkeypatch.delenv('BB_SOCKET_BACKLOG', raising=False)
    reset_settings_cache()
    path = tmp_path / 'adopted.sock'
    owner = socket.socket(socket.AF_UNIX)
    owner.bind(str(path))
    owner.listen(4)
    if rsock.unix_accept_queue(owner) is None or rsock.somaxconn() is None:
        owner.close()
        pytest.skip('sock_diag or somaxconn is unreadable here')
    assert rsock.unix_accept_queue(owner) == (0, 4)
    expected = min(get_settings().socket_backlog, rsock.somaxconn())

    async def main():
        server = Server(_app())
        server.open_socket(inherited_fd=os.dup(owner.fileno()))
        runner = asyncio.create_task(server.run())
        try:
            await _until(lambda: getattr(server, '_running_servers', None))
            assert rsock.unix_accept_queue(owner) == (0, expected)
        finally:
            await asyncio.wait_for(server.stop(drain_timeout=1.0), 5)
            await asyncio.wait({runner}, timeout=5)
            server.close_socket()

    try:
        _run(loop, main)
    finally:
        owner.close()
        reset_settings_cache()
