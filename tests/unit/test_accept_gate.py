"""The accept gate's count: one hold per accepted descriptor, released once.

The count starts at ``accept()`` and ends when the descriptor is closed,
whichever of a failed connect or ``connection_lost`` says so first.
"""
from __future__ import annotations

import asyncio
import errno
import logging
import socket
import ssl

import pytest

from blackbull import BlackBull
from blackbull.server import server as server_mod
from blackbull.server.server import Server, _AcceptGate


class _Quiet(asyncio.Protocol):
    admission = None
    made = 0
    lost = 0

    def connection_made(self, transport):
        type(self).made += 1

    def connection_lost(self, exc):
        type(self).lost += 1
        if self.admission is not None:
            self.admission.release()


@pytest.fixture
def listener():
    sock = socket.socket()
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(('127.0.0.1', 0))
    sock.listen()
    yield sock
    sock.close()


async def _settle(gate: _AcceptGate) -> None:
    for _ in range(50):
        if not gate._connecting:
            return
        await asyncio.sleep(0.01)


def test_an_admission_releases_once():
    gate = _AcceptGate()
    admission = gate.admit()
    assert gate._descriptors_held == 1
    admission.release()
    admission.release()
    assert gate._descriptors_held == 0


def test_a_connection_lost_without_an_admission_releases_nothing():
    """A protocol the gate never admitted — a test server's, a fallback
    accept's — must not drive the count below zero."""
    server = Server(BlackBull())
    protocol = server.connection_protocol_factory()()
    protocol.connection_lost(None)
    assert server._accept_gate._descriptors_held == 0


@pytest.mark.asyncio
async def test_the_admission_pause_logs_one_max_connections_cap_hit(caplog):
    gate = _AcceptGate()
    gate._limit = 2

    with caplog.at_level(logging.WARNING, logger='blackbull.caps'):
        first = gate.admit()
        gate.admit()
        first.release()
        gate.admit()

    records = [record for record in caplog.records
               if record.name == 'blackbull.caps'
               and getattr(record, 'cap', None) == 'max_connections']
    assert len(records) == 1, [record.getMessage() for record in records]
    assert records[0].limit == 2, records[0].limit
    # The arrival that exceeded, as at every other cap site.
    assert records[0].requested == 3, records[0].requested


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_accepting_pauses_at_the_limit_and_resumes_below_it(listener):
    gate = _AcceptGate()
    accepting = gate.arm([((_Quiet, None), listener)], 1, 16)
    limit = 1 + server_mod._REFUSAL_RESERVE
    port = listener.getsockname()[1]
    clients = []
    try:
        held = [gate.admit() for _ in range(limit - 1)]
        clients.append(socket.create_connection(('127.0.0.1', port)))
        await asyncio.sleep(0.2)
        assert gate._descriptors_held == limit, 'the arrival up to the limit'
        assert gate._paused

        clients.append(socket.create_connection(('127.0.0.1', port)))
        await asyncio.sleep(0.2)
        assert gate._descriptors_held == limit, 'accepted while paused'

        held.pop().release()
        await asyncio.sleep(0.2)
        assert gate._descriptors_held == limit, 'did not resume below the limit'
    finally:
        for client in clients:
            client.close()
        gate.close()
        for entry in accepting:
            entry.close()
        await _settle(gate)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_a_cleartext_connect_cancelled_after_connection_made_releases_once(
        listener):
    gate = _AcceptGate()
    _Quiet.made = _Quiet.lost = 0
    client = socket.create_connection(listener.getsockname())
    conn, _ = listener.accept()
    try:
        gate.connect(conn, _Quiet, None)
        [task] = gate._connecting
        await asyncio.sleep(0)
        task.cancel()
        await _settle(gate)
        await asyncio.sleep(0.05)
        assert gate._descriptors_held == 0
        assert conn.fileno() == -1
        # Cancellation propagates: stop() and callers can tell it from success.
        assert task.cancelled()
        assert not gate._connecting
    finally:
        client.close()


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_a_failed_tls_handshake_releases_without_connection_lost(
        listener, monkeypatch):
    monkeypatch.setattr(server_mod, '_SSL_HANDSHAKE_TIMEOUT', 0.2)
    gate = _AcceptGate()
    _Quiet.made = _Quiet.lost = 0
    client = socket.create_connection(listener.getsockname())
    conn, _ = listener.accept()
    try:
        gate.connect(conn, _Quiet, ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER))
        assert gate._descriptors_held == 1
        await asyncio.sleep(0.5)
        await _settle(gate)
        assert gate._descriptors_held == 0
        assert (_Quiet.made, _Quiet.lost) == (0, 0)
        assert conn.fileno() == -1
        assert not gate._connecting, 'a finished connect is still tracked'
    finally:
        client.close()


class _FlakySocket:
    """A listening socket whose ``accept()`` fails as told, then works."""

    def __init__(self, real: socket.socket, failures: list):
        self._real = real
        self._failures = failures
        self.family = real.family

    def fileno(self):
        return self._real.fileno()

    def accept(self):
        if self._failures:
            raise self._failures.pop(0)
        return self._real.accept()

    def listen(self, backlog):
        self._real.listen(backlog)

    def setblocking(self, flag):
        self._real.setblocking(flag)

    def getsockname(self):
        return self._real.getsockname()

    def close(self):
        self._real.close()


async def _accept_through(listener, failures, monkeypatch):
    """Arm on a socket that fails *failures* first; return what the loop's
    exception handler saw and whether the client was accepted."""
    monkeypatch.setattr(server_mod, '_ACCEPT_RETRY_DELAY', 0.2)
    loop = asyncio.get_running_loop()
    seen = []
    loop.set_exception_handler(lambda _loop, context: seen.append(context))
    gate = _AcceptGate()
    accepting = gate.arm([((_Quiet, None), _FlakySocket(listener, failures))],
                         64, 16)
    client = socket.create_connection(listener.getsockname())
    try:
        for _ in range(100):
            if gate._descriptors_held:
                break
            await asyncio.sleep(0.01)
        return seen, gate._descriptors_held
    finally:
        client.close()
        gate.close()
        for entry in accepting:
            entry.close()
        await _settle(gate)
        loop.set_exception_handler(None)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_accept_out_of_descriptors_is_reported_and_retried(
        listener, monkeypatch):
    seen, held = await _accept_through(
        listener, [OSError(errno.EMFILE, 'Too many open files')], monkeypatch)

    assert [c['message'] for c in seen] == [
        'socket.accept() out of system resource']
    assert seen[0]['exception'].errno == errno.EMFILE
    assert held == 1, 'accepting never resumed after the retry delay'


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_an_aborted_accept_moves_on_to_the_next(listener, monkeypatch):
    seen, held = await _accept_through(
        listener, [ConnectionAbortedError()], monkeypatch)

    assert seen == []
    assert held == 1


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_an_unexpected_accept_error_reaches_the_loop(listener, monkeypatch):
    seen, _held = await _accept_through(
        listener, [OSError(errno.EPERM, 'not permitted')], monkeypatch)

    assert [c.get('exception').errno for c in seen] == [errno.EPERM], seen


# ---------------------------------------------------------------------------
# Readers: none left behind, on either loop
# ---------------------------------------------------------------------------

def _listeners(n: int) -> list[socket.socket]:
    socks = []
    for _ in range(n):
        sock = socket.socket()
        sock.bind(('127.0.0.1', 0))
        sock.listen()
        socks.append(sock)
    return socks


def _registered(loop, fds) -> list[bool]:
    """Whether each descriptor still has a reader, as ``remove_reader``
    reports it — on the default loop; uvloop's returns ``None``."""
    return [loop.remove_reader(fd) for fd in fds]


@pytest.mark.timeout(30)
@pytest.mark.parametrize('how', ['gate_close', 'listener_close'])
def test_closing_removes_every_reader(how):
    async def main():
        running = asyncio.get_running_loop()
        socks = _listeners(3)
        fds = [sock.fileno() for sock in socks]
        gate = _AcceptGate()
        accepting = gate.arm([((_Quiet, None), sock) for sock in socks], 64, 16)
        try:
            if how == 'gate_close':
                gate.close()
            else:
                for entry in accepting:
                    entry.close()
            assert _registered(running, fds) == [False] * 3
        finally:
            gate.close()
            for sock in socks:
                sock.close()
    asyncio.run(main())


@pytest.mark.timeout(30)
def test_a_failed_arm_leaves_no_reader():
    async def main():
        running = asyncio.get_running_loop()
        socks = _listeners(3)
        fds = [sock.fileno() for sock in socks]
        real_add_reader = running.add_reader
        calls = []

        def _refuse_second(fd, *args):
            calls.append(fd)
            if len(calls) == 2:
                raise OSError('no reader for you')
            return real_add_reader(fd, *args)

        running.add_reader = _refuse_second
        gate = _AcceptGate()
        try:
            assert gate.arm([((_Quiet, None), s) for s in socks], 64, 16) is None
            assert len(calls) == 2
            assert _registered(running, fds) == [False] * 3
        finally:
            del running.add_reader
            for sock in socks:
                sock.close()
    asyncio.run(main())


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_one_accept_tick_stops_at_the_limit(listener):
    """Connections already queued when a tick crosses the limit stay queued."""
    port = listener.getsockname()[1]
    clients = [socket.create_connection(('127.0.0.1', port)) for _ in range(5)]
    gate = _AcceptGate()
    limit = 1 + server_mod._REFUSAL_RESERVE
    held = []
    accepting = []
    try:
        gate._limit = limit            # so the admissions below count against it
        held = [gate.admit() for _ in range(limit - 2)]
        accepting = gate.arm([((_Quiet, None), listener)], 1, 16)
        await asyncio.sleep(0.2)
        assert gate._descriptors_held == limit
    finally:
        for client in clients:
            client.close()
        gate.close()
        for entry in accepting:
            entry.close()
        await _settle(gate)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_a_connect_that_fails_before_a_transport_closes_the_socket(
        listener, monkeypatch):
    async def refuse(*_args, **_kwargs):
        raise ValueError('no transport')

    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, 'connect_accepted_socket', refuse)
    gate = _AcceptGate()
    client = socket.create_connection(listener.getsockname())
    conn, _ = listener.accept()
    try:
        gate.connect(conn, _Quiet, None)
        await _settle(gate)
        assert conn.fileno() == -1
        assert gate._descriptors_held == 0
    finally:
        client.close()


# ---------------------------------------------------------------------------
# A back-off after EMFILE and a pause, on two listeners
# ---------------------------------------------------------------------------

class _Idle(asyncio.Protocol):
    """Holds its admission for the connection's life, like a served client."""

    admission = None


@pytest.fixture
def two_listeners():
    socks = []
    for _ in range(2):
        sock = socket.socket()
        sock.bind(('127.0.0.1', 0))
        sock.listen()
        socks.append(sock)
    yield socks
    for sock in socks:
        sock.close()


async def _backed_off_then_paused(two_listeners, monkeypatch):
    """Listener A backs off after EMFILE with its client still queued; then
    listener B's accept brings the count to the limit and pauses the gate."""
    monkeypatch.setattr(server_mod, '_ACCEPT_RETRY_DELAY', 0.5)
    real_a, real_b = two_listeners
    gate = _AcceptGate()
    limit = 1 + server_mod._REFUSAL_RESERVE
    gate._limit = limit
    held = [gate.admit() for _ in range(limit - 1)]
    flaky = _FlakySocket(real_a, [OSError(errno.EMFILE, 'Too many open files')])
    asyncio.get_running_loop().set_exception_handler(lambda *_: None)
    accepting = gate.arm([((_Idle, None), flaky), ((_Idle, None), real_b)], 1, 16)
    clients = [socket.create_connection(real_a.getsockname())]
    await asyncio.sleep(0.05)
    assert gate._descriptors_held == limit - 1, 'A accepted through EMFILE'
    clients.append(socket.create_connection(real_b.getsockname()))
    await asyncio.sleep(0.05)
    assert gate._descriptors_held == limit and gate._paused
    return gate, accepting, held, clients


async def _teardown(gate, accepting, clients):
    for client in clients:
        client.close()
    gate.close()
    for entry in accepting:
        entry.close()
    await _settle(gate)
    asyncio.get_running_loop().set_exception_handler(None)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_a_retry_after_emfile_does_not_read_while_paused(
        two_listeners, monkeypatch):
    gate, accepting, _held, clients = await _backed_off_then_paused(
        two_listeners, monkeypatch)
    try:
        await asyncio.sleep(0.8)          # past A's retry, still paused
        assert gate._descriptors_held == gate._limit, (
            "A's retry accepted past the limit")
    finally:
        await _teardown(gate, accepting, clients)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_a_resume_during_the_back_off_waits_for_the_retry(
        two_listeners, monkeypatch):
    gate, accepting, held, clients = await _backed_off_then_paused(
        two_listeners, monkeypatch)
    try:
        held.pop().release()              # resume, inside A's back-off
        await asyncio.sleep(0.1)
        assert gate._descriptors_held == gate._limit - 1, (
            "A read again before its back-off ended")
        await asyncio.sleep(0.8)
        assert gate._descriptors_held == gate._limit, 'A never retried'
    finally:
        await _teardown(gate, accepting, clients)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_a_protocol_factory_that_raises_releases_its_admission(listener):
    def broken():
        raise RuntimeError('factory failed')

    gate = _AcceptGate()
    client = socket.create_connection(listener.getsockname())
    conn, _ = listener.accept()
    try:
        with pytest.raises(RuntimeError, match='factory failed'):
            gate.connect(conn, broken, None)
        assert gate._descriptors_held == 0
        assert conn.fileno() == -1
    finally:
        client.close()


class _Unlistenable(_FlakySocket):
    def listen(self, backlog):
        raise OSError(errno.EOPNOTSUPP, 'not a listening socket')


@pytest.mark.asyncio
@pytest.mark.timeout(30)
@pytest.mark.parametrize('failing', ['listen', 'add_reader'])
async def test_the_fallback_warning_names_what_failed(
        listener, monkeypatch, caplog, failing):
    sock = listener
    if failing == 'listen':
        sock = _Unlistenable(listener, [])
    else:
        def _refuse(*_args):
            raise NotImplementedError('no readers on this loop')
        monkeypatch.setattr(asyncio.get_running_loop(), 'add_reader', _refuse)
    with caplog.at_level(logging.WARNING, logger='blackbull.server.server'):
        assert _AcceptGate().arm([((_Quiet, None), sock)], 64, 16) is None
    [record] = [r for r in caplog.records if r.levelno == logging.WARNING]
    message = record.getMessage()
    assert f'{failing}()' in message, message
    assert 'BB_MAX_CONNECTIONS' in message


# ---------------------------------------------------------------------------
# A failed connect is reported the way the loop reports its own
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.timeout(30)
@pytest.mark.parametrize('debug', [False, True])
async def test_a_failed_connect_is_reported_only_in_debug_and_never_as_unretrieved(
        listener, monkeypatch, debug):
    import gc

    async def refuse(*_args, **_kwargs):
        await asyncio.sleep(0)
        raise ConnectionResetError('peer went away')

    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, 'connect_accepted_socket', refuse)
    seen = []
    loop.set_exception_handler(lambda _loop, context: seen.append(context['message']))
    loop.set_debug(debug)
    gate = _AcceptGate()
    client = socket.create_connection(listener.getsockname())
    conn, _ = listener.accept()
    try:
        gate.connect(conn, _Quiet, None)
        await _settle(gate)
        await asyncio.sleep(0)
        gc.collect()
        await asyncio.sleep(0)
        assert gate._descriptors_held == 0
        expected = (['Error on transport creation for incoming connection']
                    if debug else [])
        assert seen == expected
    finally:
        loop.set_debug(False)
        loop.set_exception_handler(None)
        client.close()
