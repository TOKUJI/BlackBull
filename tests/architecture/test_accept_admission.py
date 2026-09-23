"""A burst refused by the connection cap must not exhaust the descriptor reserve.

Two processes: the server's ``RLIMIT_NOFILE`` must be small, and lowering it
here would lower it for every test on this ``pytest -n`` worker.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import socket
import sys
from collections import Counter
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CHILD = Path(__file__).resolve().parent / '_accept_admission_server.py'

REQUEST = b'GET / HTTP/1.1\r\nhost: localhost\r\nconnection: close\r\n\r\n'

BURST = 200

ACCEPT_ERRORS_ALLOWED = 0

TIGHT_FD_BUDGET = 128
WIDE_FD_BUDGET = 1024


async def _one_client_outcome(port: int, *, patience: float = 15.0) -> str:
    # A short patience reads a queued connection as a refused one.
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection('127.0.0.1', port), patience)
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


async def _burst(*, soft: int, saturate: bool, burst: int = BURST,
                 backlog: str = '1024', park: float = 0.0) -> tuple[Counter, dict]:
    child = await asyncio.create_subprocess_exec(
        sys.executable, str(CHILD), str(soft), backlog, str(park),
        cwd=str(REPO_ROOT), env=dict(os.environ, PYTHONPATH=str(REPO_ROOT)),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE)
    idle: list = []
    try:
        announced = await asyncio.wait_for(child.stdout.readline(), 30)
        info = json.loads(announced)
        port = info['port']

        if saturate:
            for _ in range(info['cap']):
                idle.append(await asyncio.wait_for(
                    asyncio.open_connection('127.0.0.1', port), 10))
            # Let every idle connection take its slot before the burst.
            await asyncio.sleep(1.0)

        observed = Counter(await asyncio.gather(
            *(_one_client_outcome(port) for _ in range(burst))))
        await asyncio.sleep(2.0)
        child.stdin.write(b'\n')
        await child.stdin.drain()
        server_view = json.loads(
            await asyncio.wait_for(child.stdout.readline(), 30))
    finally:
        for _reader, writer in idle:
            writer.close()
        try:
            await asyncio.wait_for(child.wait(), 20)
        except (Exception, asyncio.CancelledError):
            child.kill()
            await child.wait()
    return observed, server_view


@pytest.mark.slow
@pytest.mark.asyncio
@pytest.mark.timeout(180)
async def test_a_refused_burst_does_not_exhaust_the_fd_reserve():
    observed, server_view = await _burst(soft=TIGHT_FD_BUDGET, saturate=True)

    assert sum(observed.values()) == BURST
    assert server_view['accept_errors'] <= ACCEPT_ERRORS_ALLOWED, (
        f"{server_view['accept_errors']} accept-resource records for {BURST} "
        f'refused connections ({server_view}): the server took connections it '
        f'had no descriptor to hold')


@pytest.mark.slow
@pytest.mark.asyncio
@pytest.mark.timeout(180)
async def test_a_refused_burst_gets_a_503_per_client():
    observed, _server_view = await _burst(soft=TIGHT_FD_BUDGET, saturate=True)

    assert observed['503'] == BURST, (
        f'not every refused client read its 503: {dict(observed)}')


@pytest.mark.slow
@pytest.mark.asyncio
@pytest.mark.timeout(180)
async def test_control_a_burst_with_headroom_is_served_without_accept_errors():
    observed, server_view = await _burst(soft=WIDE_FD_BUDGET, saturate=False)

    assert observed['200'] == BURST, (
        f'a burst inside the cap was not served: {dict(observed)}')
    assert server_view['accept_errors'] == 0, (
        f'accepting a legal burst cost descriptors: {server_view}')


STARTUP_PARK_SECONDS = 1.5   # every burst client must be queued before accepting opens


@pytest.mark.slow
@pytest.mark.asyncio
@pytest.mark.timeout(180)
async def test_a_burst_parked_through_the_startup_window_is_served_not_reset():
    observed, server_view = await _burst(
        soft=TIGHT_FD_BUDGET, saturate=False, park=STARTUP_PARK_SECONDS)

    assert server_view['accept_errors'] == 0, (
        f'the first tick after the window spent the reserve: {server_view}')
    assert observed['200'] == BURST, (
        f'a connection parked through the window was not served: '
        f'{dict(observed)}')


class _Accepted(asyncio.Protocol):
    seen: list = []

    def connection_made(self, transport):
        _Accepted.seen.append(transport.get_extra_info('sockname'))
        transport.close()


@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_a_gate_failing_on_the_second_listener_leaves_all_three_accepting(
        monkeypatch):
    from blackbull.server.server import _AcceptGate

    loop = asyncio.get_running_loop()
    _Accepted.seen = []
    # The bound sockets, not ``server.sockets``: those wrappers have no
    # ``listen``, so the gate would fail on the first listener instead.
    sockets = []
    for _ in range(3):
        sock = socket.socket()
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(('127.0.0.1', 0))
        sockets.append(sock)
    ports = [sock.getsockname()[1] for sock in sockets]
    servers = [await loop.create_server(_Accepted, sock=sock, start_serving=False)
               for sock in sockets]
    real_start_serving = loop._start_serving
    calls = []

    def _explode_on_the_second(*args, **kwargs):
        calls.append(args)
        if len(calls) == 2:
            raise OSError('no reader for you')
        return real_start_serving(*args, **kwargs)

    gate = _AcceptGate()
    try:
        monkeypatch.setattr(loop, '_start_serving', _explode_on_the_second,
                            raising=False)
        assert gate.arm(list(zip(servers, sockets)), 64) is False, (
            'arm() reported success after failing to register a reader')
        assert len(calls) == 2, (
            f'the gate gave up before the listener that fails: {len(calls)} '
            f'registration(s) attempted')
        monkeypatch.undo()
        assert [server._serving for server in servers] == [False] * 3, (
            'a listener was left flagged as serving without a reader')

        for server in servers:
            await server.start_serving()
        for port in ports:
            _reader, writer = await asyncio.wait_for(
                asyncio.open_connection('127.0.0.1', port), 5)
            writer.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(writer.wait_closed(), 5)
        await asyncio.sleep(0.1)
        assert len(_Accepted.seen) == 3, (
            f'{3 - len(_Accepted.seen)} listener(s) never accepted after the '
            f'fallback: {_Accepted.seen}')
    finally:
        for server in servers:
            server.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(server.wait_closed(), 5)


@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_the_admission_pause_logs_one_max_connections_cap_hit(caplog):
    from blackbull.server.server import _AcceptGate

    gate = _AcceptGate()
    gate._loop = asyncio.get_running_loop()
    gate._limit = 2

    with caplog.at_level(logging.WARNING, logger='blackbull.caps'):
        gate.hold()
        gate.hold()
        gate.release()
        gate.hold()

    records = [record for record in caplog.records
               if record.name == 'blackbull.caps'
               and getattr(record, 'cap', None) == 'max_connections']
    assert len(records) == 1, [record.getMessage() for record in records]
    assert records[0].limit == 2, records[0].limit
    # The arrival that exceeded, as at every other cap site.
    assert records[0].requested == 3, records[0].requested
