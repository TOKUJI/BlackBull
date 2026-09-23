"""An ``AF_UNIX`` listener whose accept queue is full when accepting opens.

Past the backlog, a non-blocking ``AF_UNIX`` client is refused rather than
delayed, so a queue that filled during lifespan startup has already cost
clients.  The server reads the queue from the kernel as accepting opens and
warns once per full listener, whoever set its backlog.
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import shutil
import socket
import struct
import subprocess
import sys
from pathlib import Path

import pytest

from blackbull import BlackBull
from blackbull.env import reset_settings_cache
from blackbull.protocol import rsock
from blackbull.server import server as server_mod
from blackbull.server.listener import Listener, Unix
from blackbull.server.server import Server

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith('linux'), reason='sock_diag is Linux-only')

BACKLOG = 4


def _app(started: asyncio.Event, release: asyncio.Event) -> BlackBull:
    app = BlackBull()

    @app.on_startup
    async def _startup():
        started.set()
        await release.wait()

    @app.route(path='/')
    async def _index():
        return 'ok'

    return app


def _connect_nonblocking(path: str, n: int) -> tuple[list, int]:
    """*n* connect attempts the way nginx makes them; returns (clients, refused)."""
    clients, refused = [], 0
    for _ in range(n):
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.setblocking(False)
        try:
            client.connect(path)
        except BlockingIOError:
            refused += 1
            client.close()
            continue
        clients.append(client)
    return clients, refused


async def _answered(client: socket.socket) -> None:
    """A queued client gets its answer, so accepting has opened.

    A fresh connection cannot tell: it would join the queue being read.
    """
    loop = asyncio.get_running_loop()
    await loop.sock_sendall(
        client, b'GET / HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n')
    data = await asyncio.wait_for(loop.sock_recv(client, 64), timeout=5)
    assert data.startswith(b'HTTP/1.1 200'), data


async def _served(path: str, timeout: float = 5.0) -> None:
    """Returns once the server answers, so accepting has opened."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        try:
            reader, writer = await asyncio.open_unix_connection(path)
            writer.write(b'GET / HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n')
            data = await asyncio.wait_for(reader.read(), timeout=1.0)
            writer.close()
            if data.startswith(b'HTTP/1.1 200'):
                return
        except (OSError, asyncio.TimeoutError):
            pass
        if loop.time() >= deadline:
            raise AssertionError('server never answered after startup')
        await asyncio.sleep(0.02)


async def _open_during_startup(served, attempts: int) -> int:
    """Run the server, make *attempts* connects during startup; returns refusals."""
    server, path, started, release = served
    runner = asyncio.create_task(server.run())
    clients: list = []
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        clients, refused = _connect_nonblocking(path, attempts)
        release.set()
        await _answered(clients[0])
        return refused
    finally:
        for client in clients:
            client.close()
        await asyncio.wait_for(server.stop(drain_timeout=1.0), timeout=5)
        await asyncio.wait({runner}, timeout=5)
        server.close_socket()


def _server(tmp_path, how: str, max_connections: int, monkeypatch):
    """A server on an AF_UNIX listener of backlog ``BACKLOG``.

    ``bound``: BlackBull binds it, from ``BB_SOCKET_BACKLOG``.  ``adopted``:
    the creator listens and hands BlackBull a duplicate fd, as systemd does,
    while ``BB_SOCKET_BACKLOG`` stays at its default.
    """
    started, release = asyncio.Event(), asyncio.Event()
    path = str(tmp_path / 'q.sock')
    server = Server(_app(started, release), max_connections=max_connections)
    if how == 'bound':
        monkeypatch.setenv('BB_SOCKET_BACKLOG', str(BACKLOG))
        reset_settings_cache()
        server.open_socket(unix_path=path)
    else:
        monkeypatch.delenv('BB_SOCKET_BACKLOG', raising=False)
        reset_settings_cache()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as creator:
            creator.bind(path)
            creator.listen(BACKLOG)
            server.open_socket(inherited_fd=os.dup(creator.fileno()))
    return server, path, started, release


def _backlog_warnings(caplog) -> list:
    return [r for r in caplog.records
            if r.levelno >= logging.WARNING and r.name.startswith('blackbull')]


def _cap_records(caplog) -> list:
    return [r for r in caplog.records if r.name == 'blackbull.caps']


@pytest.fixture(autouse=True)
def _fresh_settings():
    yield
    reset_settings_cache()


@pytest.mark.asyncio
@pytest.mark.timeout(30)
@pytest.mark.parametrize('how', ['bound', 'adopted'])
@pytest.mark.parametrize('max_connections', [0, 64], ids=['start_serving', 'gate'])
async def test_a_full_queue_at_open_warns_once_with_waiting_and_backlog(
        tmp_path, caplog, monkeypatch, how, max_connections):
    served = _server(tmp_path, how, max_connections, monkeypatch)
    path = served[1]
    with caplog.at_level(logging.WARNING, logger='blackbull'):
        refused = await _open_during_startup(served, BACKLOG + 3)
    assert refused == 2, 'the kernel admits backlog + 1 on AF_UNIX'

    warnings = _backlog_warnings(caplog)
    assert len(warnings) == 1, [r.getMessage() for r in warnings]
    record = warnings[0]
    assert record.name == 'blackbull.caps'
    assert (record.cap, record.requested, record.limit) == (
        'socket_backlog', BACKLOG + 1, BACKLOG)
    assert record.scope_path == path
    message = record.getMessage()
    assert path in message
    assert 'may have been refused' in message
    assert 'BB_SOCKET_BACKLOG' in message and 'Backlog=' in message
    assert 'somaxconn' not in message


@pytest.mark.asyncio
@pytest.mark.timeout(30)
@pytest.mark.parametrize('how', ['bound', 'adopted'])
async def test_a_queue_that_is_not_full_gives_no_warning(
        tmp_path, caplog, monkeypatch, how):
    """``BACKLOG`` waiting is not full: one more would still be admitted."""
    served = _server(tmp_path, how, 0, monkeypatch)
    with caplog.at_level(logging.WARNING, logger='blackbull'):
        refused = await _open_during_startup(served, BACKLOG)
    assert refused == 0
    assert _backlog_warnings(caplog) == []


@pytest.mark.asyncio
@pytest.mark.timeout(30)
@pytest.mark.parametrize('failure', ['raises', 'returns_none'])
async def test_a_failing_query_gives_no_warning_and_still_serves(
        tmp_path, caplog, monkeypatch, failure):
    def broken(sock):
        if failure == 'raises':
            raise OSError('sock_diag unavailable')
        return None

    monkeypatch.setattr(server_mod, 'unix_accept_queue', broken)
    served = _server(tmp_path, 'bound', 0, monkeypatch)
    with caplog.at_level(logging.WARNING, logger='blackbull'):
        await _open_during_startup(served, BACKLOG + 3)
    assert _backlog_warnings(caplog) == []


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_tcp_listeners_are_never_queried(monkeypatch):
    queried = []
    monkeypatch.setattr(server_mod, 'unix_accept_queue',
                        lambda sock: queried.append(sock) or None)
    started, release = asyncio.Event(), asyncio.Event()
    server = Server(_app(started, release))
    server.open_socket(0)
    runner = asyncio.create_task(server.run())
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        release.set()
        reader, writer = await asyncio.open_connection('127.0.0.1', server.port)
        writer.write(b'GET / HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n')
        assert (await asyncio.wait_for(reader.read(), 5)).startswith(b'HTTP/1.1 200')
        writer.close()
    finally:
        await asyncio.wait_for(server.stop(drain_timeout=1.0), timeout=5)
        await asyncio.wait({runner}, timeout=5)
        server.close_socket()
    assert queried == []


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_each_unix_listener_is_queried_once(tmp_path, monkeypatch):
    queried = []
    monkeypatch.setattr(server_mod, 'unix_accept_queue',
                        lambda sock: queried.append(sock.getsockname()) or None)
    started, release = asyncio.Event(), asyncio.Event()
    paths = [str(tmp_path / 'a.sock'), str(tmp_path / 'b.sock')]
    server = Server(_app(started, release),
                    listeners=[Listener(Unix(p)) for p in paths])
    server.open_socket()
    runner = asyncio.create_task(server.run())
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        release.set()
        await _served(paths[1])
    finally:
        await asyncio.wait_for(server.stop(drain_timeout=1.0), timeout=5)
        await asyncio.wait({runner}, timeout=5)
        server.close_socket()
    assert sorted(queried) == sorted(paths)


def _fill(sock_path: str, backlog: int) -> tuple[socket.socket, list]:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(sock_path)
    sock.listen(backlog)
    return sock, _connect_nonblocking(sock_path, 8)[0]


def test_a_somaxconn_capped_backlog_names_somaxconn(tmp_path, caplog, monkeypatch):
    """Raising ``BB_SOCKET_BACKLOG`` cannot help once somaxconn caps it.

    The listener reads as ``listen(1024)`` does under ``somaxconn=4``.
    """
    monkeypatch.setattr(server_mod, 'somaxconn', lambda: BACKLOG)
    sock, clients = _fill(str(tmp_path / 's.sock'), BACKLOG)
    try:
        with caplog.at_level(logging.WARNING, logger='blackbull'):
            server_mod._warn_if_unix_queue_full(sock)
    finally:
        for client in clients:
            client.close()
        sock.close()
    [record] = _backlog_warnings(caplog)
    assert record.limit == BACKLOG
    message = record.getMessage()
    assert 'net.core.somaxconn' in message
    assert 'BB_SOCKET_BACKLOG' not in message


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_an_abstract_listener_is_named_with_an_at(caplog, monkeypatch):
    """``ListenStream=@name``: the kernel names it in bytes, with a NUL."""
    name = f'bb-abstract-{os.getpid()}'
    started, release = asyncio.Event(), asyncio.Event()
    server = Server(_app(started, release))
    reset_settings_cache()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as creator:
        creator.bind('\0' + name)
        creator.listen(BACKLOG)
        server.open_socket(inherited_fd=os.dup(creator.fileno()))
    runner = asyncio.create_task(server.run())
    clients: list = []
    try:
        with caplog.at_level(logging.WARNING, logger='blackbull'):
            await asyncio.wait_for(started.wait(), timeout=5)
            clients, _ = _connect_nonblocking('\0' + name, BACKLOG + 3)
            release.set()
            # Not a served request: serving an abstract listener is not
            # what this pins.
            for _ in range(250):
                if _cap_records(caplog) or runner.done():
                    break
                await asyncio.sleep(0.02)
            assert not runner.done(), runner.exception()
    finally:
        for client in clients:
            client.close()
        await asyncio.wait_for(server.stop(drain_timeout=1.0), timeout=5)
        await asyncio.wait({runner}, timeout=5)
        server.close_socket()
    [record] = _cap_records(caplog)
    assert record.scope_path == '@' + name
    assert repr('@' + name) in record.getMessage()


@pytest.mark.parametrize('proc', [
    OSError('no /proc'), PermissionError('denied'), 'garbage', '', '\xff'])
def test_an_unreadable_somaxconn_keeps_the_backlog_advice(
        tmp_path, caplog, monkeypatch, proc):
    def fake_open(*args, **kwargs):
        if isinstance(proc, Exception):
            raise proc
        return io.StringIO(proc)

    monkeypatch.setattr(rsock, 'open', fake_open, raising=False)
    assert rsock.somaxconn() is None
    sock, clients = _fill(str(tmp_path / 's.sock'), BACKLOG)
    try:
        with caplog.at_level(logging.WARNING, logger='blackbull'):
            server_mod._warn_if_unix_queue_full(sock)
    finally:
        for client in clients:
            client.close()
        sock.close()
    [record] = _backlog_warnings(caplog)
    assert record.limit == BACKLOG
    assert 'BB_SOCKET_BACKLOG' in record.getMessage()
    assert 'net.core.somaxconn' not in record.getMessage()


def test_a_failing_log_call_never_reaches_run(tmp_path, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError('handler blew up')

    monkeypatch.setattr(server_mod, 'log_cap_hit', broken)
    sock, clients = _fill(str(tmp_path / 's.sock'), BACKLOG)
    try:
        server_mod._warn_if_unix_queue_full(sock)
    finally:
        for client in clients:
            client.close()
        sock.close()


_IN_NETNS = """
import json, logging, socket, sys
with open('/proc/sys/net/core/somaxconn', 'w') as f:
    f.write('3')
from blackbull.server.server import _warn_if_unix_queue_full
records = []
class Keep(logging.Handler):
    def emit(self, record):
        records.append({'limit': record.limit, 'message': record.getMessage()})
caps = logging.getLogger('blackbull.caps')
caps.addHandler(Keep())
caps.setLevel(logging.WARNING)
sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
sock.bind(sys.argv[1])
sock.listen(1024)
clients = []
for _ in range(8):
    c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    c.setblocking(False)
    try:
        c.connect(sys.argv[1])
        clients.append(c)
    except BlockingIOError:
        c.close()
_warn_if_unix_queue_full(sock)
print(json.dumps(records))
"""


@pytest.mark.timeout(30)
def test_a_real_somaxconn_cap_names_somaxconn(tmp_path):
    """The same, with the kernel doing the capping in a private namespace."""
    unshare = shutil.which('unshare')
    if unshare is None or subprocess.run(
            [unshare, '-rn', 'true'], capture_output=True).returncode != 0:
        pytest.skip('unprivileged network namespaces are unavailable')
    root = Path(__file__).resolve().parents[2]
    done = subprocess.run(
        [unshare, '-rn', sys.executable, '-c', _IN_NETNS, str(tmp_path / 's.sock')],
        capture_output=True, text=True, timeout=20,
        env={**os.environ, 'PYTHONPATH': str(root)})
    assert done.returncode == 0, done.stderr
    [record] = json.loads(done.stdout)
    assert record['limit'] == 3
    assert 'net.core.somaxconn' in record['message']
    assert 'BB_SOCKET_BACKLOG' not in record['message']


# ---------------------------------------------------------------------------
# The kernel reader on its own
# ---------------------------------------------------------------------------

@pytest.fixture
def listener(tmp_path):
    path = str(tmp_path / 'r.sock')
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(path)
    sock.listen(BACKLOG)
    yield sock, path
    sock.close()


def test_reader_returns_waiting_and_backlog(listener):
    sock, path = listener
    assert rsock.unix_accept_queue(sock) == (0, BACKLOG)
    clients, refused = _connect_nonblocking(path, BACKLOG + 3)
    try:
        assert refused == 2
        assert rsock.unix_accept_queue(sock) == (BACKLOG + 1, BACKLOG)
    finally:
        for client in clients:
            client.close()


def test_reader_sees_the_same_queue_through_a_dup(listener):
    sock, path = listener
    clients, _ = _connect_nonblocking(path, 2)
    dup = socket.socket(fileno=os.dup(sock.fileno()))
    try:
        assert rsock.unix_accept_queue(dup) == rsock.unix_accept_queue(sock) == (2, BACKLOG)
    finally:
        dup.close()
        for client in clients:
            client.close()


@pytest.mark.parametrize('reply', [b'', b'\x00' * 16, b'\xff' * 64])
def test_reader_returns_none_on_an_unusable_reply(listener, monkeypatch, reply):
    monkeypatch.setattr(rsock, '_sock_diag_exchange', lambda request: reply)
    assert rsock.unix_accept_queue(listener[0]) is None


def test_reader_returns_none_when_netlink_is_unavailable(listener, monkeypatch):
    def refuse(request):
        raise OSError('no netlink here')
    monkeypatch.setattr(rsock, '_sock_diag_exchange', refuse)
    assert rsock.unix_accept_queue(listener[0]) is None


def test_reader_returns_none_off_linux(listener, monkeypatch):
    monkeypatch.setattr(rsock.sys, 'platform', 'darwin')
    assert rsock.unix_accept_queue(listener[0]) is None


def test_reader_returns_none_for_a_socket_that_is_not_listening(tmp_path):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.bind(str(tmp_path / 'idle.sock'))
        assert rsock.unix_accept_queue(sock) is None


def _reply(inode: int, *, attrs: bytes, state: int = 10, length_extra: int = 0) -> bytes:
    """A sock_diag reply: nlmsghdr, unix_diag_msg, then *attrs*."""
    body = struct.pack('=BBBBIII', socket.AF_UNIX, socket.SOCK_STREAM, state, 0,
                       inode, 0, 0) + attrs
    return struct.pack('=IHHII', 16 + len(body) + length_extra, 20, 0, 1, 0) + body


def _rqlen(waiting: int, backlog: int) -> bytes:
    return struct.pack('=HHII', 12, 4, waiting, backlog)


def _inode(sock) -> int:
    return os.fstat(sock.fileno()).st_ino


def test_reader_parses_a_crafted_reply(listener, monkeypatch):
    """The positive control for the crafted replies below."""
    reply = _reply(_inode(listener[0]), attrs=_rqlen(7, 3))
    monkeypatch.setattr(rsock, '_sock_diag_exchange', lambda request: reply)
    assert rsock.unix_accept_queue(listener[0]) == (7, 3)


def test_reader_rejects_a_reply_for_another_inode(listener, monkeypatch):
    reply = _reply(_inode(listener[0]) + 1, attrs=_rqlen(7, 3))
    monkeypatch.setattr(rsock, '_sock_diag_exchange', lambda request: reply)
    assert rsock.unix_accept_queue(listener[0]) is None


def test_reader_rejects_a_reply_shorter_than_its_header_says(listener, monkeypatch):
    reply = _reply(_inode(listener[0]), attrs=_rqlen(7, 3), length_extra=64)
    monkeypatch.setattr(rsock, '_sock_diag_exchange', lambda request: reply)
    assert rsock.unix_accept_queue(listener[0]) is None


@pytest.mark.timeout(5)
def test_reader_stops_at_a_zero_length_attribute(listener, monkeypatch):
    """A zero-length attribute never advances the walk; startup would hang."""
    reply = _reply(_inode(listener[0]),
                   attrs=struct.pack('=HH', 0, 1) + _rqlen(7, 3))
    monkeypatch.setattr(rsock, '_sock_diag_exchange', lambda request: reply)
    assert rsock.unix_accept_queue(listener[0]) is None
