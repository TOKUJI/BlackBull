"""A burst refused by the connection cap must not exhaust the descriptor reserve.

Two processes: the server's ``RLIMIT_NOFILE`` must be small, and lowering it
here would lower it for every test on this ``pytest -n`` worker.
"""
from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import json
import logging
import os
import socket
import sys
from collections import Counter
from pathlib import Path

import pytest

from blackbull.server.server import _REFUSAL_RESERVE

REPO_ROOT = Path(__file__).resolve().parents[2]
CHILD = Path(__file__).resolve().parent / '_accept_admission_server.py'

REQUEST = b'GET / HTTP/1.1\r\nhost: localhost\r\nconnection: close\r\n\r\n'

BURST = 200

ACCEPT_ERRORS_ALLOWED = 0

TIGHT_FD_BUDGET = 128
WIDE_FD_BUDGET = 1024


LOOPS = ['selector', pytest.param('uvloop', marks=pytest.mark.skipif(
    importlib.util.find_spec('uvloop') is None, reason='uvloop not installed'))]


def _connector(info: dict):
    if info.get('unix'):
        return lambda: asyncio.open_unix_connection(info['unix'])
    return lambda: asyncio.open_connection('127.0.0.1', info['port'])


async def _one_client_outcome(connect, *, patience: float = 15.0) -> str:
    # A short patience reads a queued connection as a refused one.
    try:
        reader, writer = await asyncio.wait_for(connect(), patience)
    except (OSError, asyncio.TimeoutError) as exc:
        return f'connect:{type(exc).__name__}'
    try:
        writer.write(REQUEST)
        await asyncio.wait_for(writer.drain(), patience)
        line = await asyncio.wait_for(reader.readline(), patience)
    except (OSError, asyncio.TimeoutError) as exc:
        return f'io:{type(exc).__name__}'
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(writer.wait_closed(), timeout=5)
    if line.startswith(b'HTTP/1.1 503'):
        return '503'
    if line.startswith(b'HTTP/1.1 200'):
        return '200'
    return f'other:{line[:24]!r}'


class _Child:
    """The server child, and a way to ask it for its view."""

    def __init__(self, process, info: dict):
        self.process = process
        self.info = info

    async def report(self, *, last: bool = False) -> dict:
        self.process.stdin.write(b'\n' if last else b'r\n')
        await self.process.stdin.drain()
        return json.loads(
            await asyncio.wait_for(self.process.stdout.readline(), 30))


@contextlib.asynccontextmanager
async def _server_child(*, soft: int, backlog: str = '1024', park: float = 0.0,
                        loop: str = 'selector', tls: bool = False,
                        unix: str | None = None):
    env = dict(os.environ, PYTHONPATH=str(REPO_ROOT), ADMISSION_LOOP=loop,
               ADMISSION_TLS='1' if tls else '0')
    if unix is not None:
        env['ADMISSION_UNIX'] = unix
    process = await asyncio.create_subprocess_exec(
        sys.executable, str(CHILD), str(soft), backlog, str(park),
        cwd=str(REPO_ROOT), env=env,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE)
    try:
        info = json.loads(await asyncio.wait_for(process.stdout.readline(), 30))
        yield _Child(process, info)
    finally:
        with contextlib.suppress(Exception):
            process.stdin.write(b'\n')
        try:
            await asyncio.wait_for(process.wait(), 20)
        except (Exception, asyncio.CancelledError):
            process.kill()
            await process.wait()


async def _burst(*, soft: int, saturate: bool, burst: int = BURST,
                 backlog: str = '1024', park: float = 0.0,
                 loop: str = 'selector', unix: str | None = None,
                 ) -> tuple[Counter, dict, dict]:
    """The clients' outcomes, the server's view after them, and its view once
    every client has gone."""
    async with _server_child(soft=soft, backlog=backlog, park=park, loop=loop,
                             unix=unix) as child:
        connect = _connector(child.info)
        idle: list = []
        try:
            if saturate:
                for _ in range(child.info['cap']):
                    idle.append(await asyncio.wait_for(connect(), 10))
                # Let every idle connection take its slot before the burst.
                await asyncio.sleep(1.0)

            observed = Counter(await asyncio.gather(
                *(_one_client_outcome(connect) for _ in range(burst))))
            await asyncio.sleep(2.0)
            server_view = await child.report()
        finally:
            for _reader, writer in idle:
                writer.close()
        await asyncio.sleep(1.0)
        settled = await child.report(last=True)
    return observed, server_view, settled


@pytest.mark.slow
@pytest.mark.asyncio
@pytest.mark.timeout(180)
@pytest.mark.parametrize('loop', LOOPS)
async def test_a_refused_burst_does_not_exhaust_the_fd_reserve(loop):
    observed, server_view, settled = await _burst(
        soft=TIGHT_FD_BUDGET, saturate=True, loop=loop)

    assert sum(observed.values()) == BURST
    assert server_view['accept_errors'] <= ACCEPT_ERRORS_ALLOWED, (
        f"{server_view['accept_errors']} accept-resource records for {BURST} "
        f'refused connections ({server_view}): the server took connections it '
        f'had no descriptor to hold')
    assert isinstance(server_view['fds'], int), f'the server hit EMFILE: {server_view}'
    # Every accepted descriptor released once: none left, none released twice.
    assert settled['held'] == 0, settled


@pytest.mark.slow
@pytest.mark.asyncio
@pytest.mark.timeout(180)
@pytest.mark.parametrize('loop', LOOPS)
async def test_a_refused_burst_gets_a_503_per_client(loop):
    observed, _server_view, _settled = await _burst(
        soft=TIGHT_FD_BUDGET, saturate=True, loop=loop)

    assert observed['503'] == BURST, (
        f'not every refused client read its 503: {dict(observed)}')


@pytest.mark.slow
@pytest.mark.asyncio
@pytest.mark.timeout(180)
@pytest.mark.parametrize('loop', LOOPS)
async def test_control_a_burst_with_headroom_is_served_without_accept_errors(loop):
    observed, server_view, _settled = await _burst(
        soft=WIDE_FD_BUDGET, saturate=False, loop=loop)

    assert observed['200'] == BURST, (
        f'a burst inside the cap was not served: {dict(observed)}')
    assert server_view['accept_errors'] == 0, (
        f'accepting a legal burst cost descriptors: {server_view}')


STARTUP_PARK_SECONDS = 1.5   # every burst client must be queued before accepting opens


@pytest.mark.slow
@pytest.mark.asyncio
@pytest.mark.timeout(180)
@pytest.mark.parametrize('loop', LOOPS)
async def test_a_burst_parked_through_the_startup_window_is_served_not_reset(loop):
    observed, server_view, _settled = await _burst(
        soft=TIGHT_FD_BUDGET, saturate=False, park=STARTUP_PARK_SECONDS,
        loop=loop)

    assert server_view['accept_errors'] == 0, (
        f'the first tick after the window spent the reserve: {server_view}')
    assert observed['200'] == BURST, (
        f'a connection parked through the window was not served: '
        f'{dict(observed)}')


@pytest.mark.slow
@pytest.mark.asyncio
@pytest.mark.timeout(180)
@pytest.mark.parametrize('loop', LOOPS)
async def test_a_refused_unix_burst_gets_a_503_per_client(loop, tmp_path):
    observed, server_view, settled = await _burst(
        soft=TIGHT_FD_BUDGET, saturate=True, loop=loop,
        unix=str(tmp_path / 'admission.sock'))

    assert observed['503'] == BURST, (
        f'not every refused client read its 503: {dict(observed)}')
    assert isinstance(server_view['fds'], int), server_view
    assert settled['held'] == 0, settled


STALLED = 200


@pytest.mark.slow
@pytest.mark.asyncio
@pytest.mark.timeout(180)
@pytest.mark.parametrize('loop', LOOPS)
async def test_stalled_tls_handshakes_pause_accepting_at_the_limit(loop):
    """A client that opens a TLS connection and never says a word holds a
    descriptor for the whole handshake timeout: it is counted from accept."""
    async with _server_child(soft=TIGHT_FD_BUDGET, loop=loop, tls=True) as child:
        limit = child.info['cap'] + _REFUSAL_RESERVE
        silent = []
        try:
            for _ in range(STALLED):
                sock = socket.socket()
                sock.setblocking(False)
                with contextlib.suppress(BlockingIOError):
                    sock.connect(('127.0.0.1', child.info['port']))
                silent.append(sock)
            await asyncio.sleep(3.0)
            stalled = await child.report()
        finally:
            for sock in silent:
                sock.close()
        await asyncio.sleep(1.0)
        settled = await child.report(last=True)

    assert isinstance(stalled['fds'], int), f'the server hit EMFILE: {stalled}'
    assert stalled['accept_errors'] == 0, stalled
    assert stalled['held'] == limit, (
        f'accepting did not pause at the limit {limit}: {stalled}')
    assert settled['held'] == 0, settled


@pytest.mark.asyncio
@pytest.mark.timeout(60)
@pytest.mark.parametrize('fails_on', [1, 2], ids=['first', 'second'])
async def test_a_reader_failing_to_register_leaves_every_listener_accepting(
        monkeypatch, caplog, fails_on):
    """A loop that cannot register a reader: every listener still accepts,
    through the loop's own accept, and the operator is told once."""
    from blackbull import BlackBull
    from blackbull.server.listener import Listener, Tcp
    from blackbull.server.server import Server

    app = BlackBull()

    @app.route(path='/')
    async def index():
        return 'ok'

    server = Server(app, max_connections=64, listeners=[
        Listener(Tcp(0, host='127.0.0.1')) for _ in range(3)])
    server.open_socket()
    ports = [socks[0].getsockname()[1] for _l, socks in server.bound_listeners]
    loop = asyncio.get_running_loop()
    real_add_reader = loop.add_reader
    calls = []

    def _refuse(fd, *args):
        calls.append(fd)
        if len(calls) == fails_on:
            raise NotImplementedError('no readers on this loop')
        return real_add_reader(fd, *args)

    monkeypatch.setattr(loop, 'add_reader', _refuse)
    runner = asyncio.create_task(server.run())
    try:
        with caplog.at_level(logging.WARNING, logger='blackbull.server.server'):
            for port in ports:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection('127.0.0.1', port), 5)
                writer.write(REQUEST)
                line = await asyncio.wait_for(reader.readline(), 5)
                writer.close()
                assert line.startswith(b'HTTP/1.1 200'), (port, line)
    finally:
        await asyncio.wait_for(server.stop(drain_timeout=1.0), 5)
        await asyncio.wait({runner}, timeout=5)
        server.close_socket()
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING
                and 'accept' in r.getMessage().lower()]
    assert len(warnings) == 1, [r.getMessage() for r in warnings]
    assert 'BB_MAX_CONNECTIONS' in warnings[0].getMessage()
