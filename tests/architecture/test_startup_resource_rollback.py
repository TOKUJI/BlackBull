"""Startup either commits every resource or rolls all of them back."""
from __future__ import annotations

import asyncio
import errno
import multiprocessing
import os
import socket
import ssl
import threading
from types import SimpleNamespace

import pytest

from blackbull import BlackBull
from blackbull.app import serve
from blackbull.protocol.rsock import adopt_inherited_sockets, close_sockets
from blackbull.server.listener import InheritedFd, Listener, Tcp
from blackbull.server.multiworker import MultiWorkerServer
from blackbull.server.reload import exec_self_with_sockets
from blackbull.server.server import LifespanManager, Server, SocketManager


class _CloseProbe:
    def __init__(self, *, fail: bool = False):
        self.close_calls = 0
        self._fail = fail

    def fileno(self):
        return id(self)

    def getsockname(self):
        return ("127.0.0.1", 12345)

    def close(self):
        self.close_calls += 1
        if self._fail:
            raise OSError("close failed")


class _AsyncServerProbe(_CloseProbe):
    def __init__(self, *, fail_close: bool = False):
        super().__init__(fail=fail_close)
        self.wait_closed_calls = 0

    async def wait_closed(self):
        self.wait_closed_calls += 1


class _SocketProbe(_CloseProbe):
    family = socket.AF_INET


class _GetSockNameFailure(_CloseProbe):
    def getsockname(self):
        raise OSError("getsockname failed")


class _ProcessProbe:
    def __init__(self, fail_operation=None, *, alive=False, stubborn=False):
        self.fail_operation = fail_operation
        self.error = RuntimeError(f"{fail_operation} failed")
        self.alive = alive
        self.stubborn = stubborn
        self.calls = []
        self._pid = 1234

    @property
    def pid(self):
        self.calls.append("pid")
        if self.fail_operation == "pid":
            raise self.error
        return self._pid

    @property
    def exitcode(self):
        return None

    def start(self):
        self.calls.append("start")
        self.alive = True

    def is_alive(self):
        self.calls.append("is_alive")
        if self.fail_operation == "is_alive":
            raise self.error
        return self.alive

    def terminate(self):
        self.calls.append("terminate")
        if self.fail_operation == "terminate":
            raise self.error
        if not self.stubborn:
            self.alive = False

    def join(self, timeout=None):
        self.calls.append(("join", timeout))
        if self.fail_operation == "join":
            raise self.error

    def kill(self):
        self.calls.append("kill")
        if self.fail_operation == "kill":
            raise self.error
        self.alive = False

    def close(self):
        self.calls.append("close")
        if self.fail_operation == "close":
            raise self.error


@pytest.mark.asyncio
async def test_socket_manager_rolls_back_servers_created_before_later_failure(
    monkeypatch,
):
    first = _AsyncServerProbe(fail_close=True)
    second = _AsyncServerProbe()
    calls = 0

    async def create_server(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise RuntimeError("third create failed")
        return (first, second)[calls - 1]

    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "create_server", create_server)
    monkeypatch.setattr(
        "blackbull.env.get_settings",
        lambda: SimpleNamespace(socket_backlog=16),
    )
    socks = [SimpleNamespace(family=socket.AF_INET) for _ in range(3)]

    with pytest.raises(RuntimeError, match="third create failed"):
        async with SocketManager(
            [(sock, asyncio.Protocol) for sock in socks], None
        ):
            pass

    assert first.close_calls == 1
    assert second.close_calls == 1
    assert first.wait_closed_calls == 1
    assert second.wait_closed_calls == 1


@pytest.mark.asyncio
async def test_later_tls_group_failure_closes_earlier_group(monkeypatch):
    app = BlackBull()
    server = Server(app)
    plain_socket = _SocketProbe()
    tls_socket = _SocketProbe()
    tls_marker = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server.bound_listeners = [
        (Listener(Tcp(1)), [plain_socket]),
        (Listener(Tcp(2), tls=tls_marker), [tls_socket]),
    ]
    first_group = _AsyncServerProbe()
    primary = RuntimeError("TLS group failed")
    calls = 0

    async def create_server(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise primary
        return first_group

    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "create_server", create_server)
    monkeypatch.setattr(
        server, "connection_protocol_factory", lambda _binding: asyncio.Protocol
    )
    monkeypatch.setattr(
        "blackbull.env.get_settings",
        lambda: SimpleNamespace(socket_backlog=16),
    )

    with pytest.raises(RuntimeError) as raised:
        await server.run()

    assert raised.value is primary
    assert first_group.close_calls == 1
    assert first_group.wait_closed_calls == 1
    assert plain_socket.close_calls == 1
    assert tls_socket.close_calls == 1


@pytest.mark.asyncio
async def test_start_serving_partial_failure_reclaims_real_connection(monkeypatch):
    async def app(scope, receive, send):
        assert scope["type"] == "lifespan"
        await receive()
        await send({"type": "lifespan.startup.complete"})
        await receive()
        await send({"type": "lifespan.shutdown.complete"})

    server = Server(app)
    first_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    first_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    first_socket.bind(("127.0.0.1", 0))
    first_socket.listen()
    address = first_socket.getsockname()
    second_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    second_socket.bind(("127.0.0.1", 0))
    second_socket.listen()
    server.bound_listeners = [
        (Listener(Tcp(address[1])), [first_socket, second_socket])
    ]
    connection_started = asyncio.Event()
    connection_tasks = []
    server_transports = []
    release = asyncio.Event()

    async def parked_connection(transport):
        connection_started.set()
        try:
            await release.wait()
        finally:
            transport.close()

    class TrackingProtocol(asyncio.Protocol):
        def connection_made(self, transport):
            server_transports.append(transport)
            task = asyncio.create_task(parked_connection(transport))
            connection_tasks.append(task)
            server._connection_tasks.add(task)
            task.add_done_callback(server._connection_tasks.discard)

    primary = RuntimeError("second start_serving failed")

    class StartFailure(_AsyncServerProbe):
        reader = None
        writer = None

        async def start_serving(self):
            self.reader, self.writer = await asyncio.open_connection(*address)
            await asyncio.wait_for(connection_started.wait(), timeout=2)
            raise primary

    failing_server = StartFailure()
    loop = asyncio.get_running_loop()
    create_server = loop.create_server
    calls = 0

    async def create_one_real_server(factory, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return await create_server(factory, **kwargs)
        return failing_server

    monkeypatch.setattr(loop, "create_server", create_one_real_server)
    monkeypatch.setattr(server, "connection_protocol_factory", lambda _binding: TrackingProtocol)

    try:
        with pytest.raises(RuntimeError) as raised:
            await server.run()
        assert raised.value is primary
        assert connection_tasks
        assert all(task.done() for task in connection_tasks)
        assert server_transports
        assert all(transport.is_closing() for transport in server_transports)
        assert await asyncio.wait_for(failing_server.reader.read(), timeout=2) == b""
        assert failing_server.close_calls == 1
        assert failing_server.wait_closed_calls == 1
    finally:
        release.set()
        if failing_server.writer is not None:
            failing_server.writer.close()
            await failing_server.writer.wait_closed()
        for sock in (first_socket, second_socket):
            sock.close()


def test_open_socket_failure_closes_new_sockets_and_allows_retry(monkeypatch):
    app = BlackBull()
    server = Server(
        app,
        listeners=[Listener(Tcp(1)), Listener(Tcp(2))],
    )
    first = _CloseProbe()
    second = _CloseProbe()
    third = _CloseProbe()
    calls = 0

    def fail_second(_listener, _cfg):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("second bind failed")
        return [first]

    monkeypatch.setattr(server, "_bind_listener", fail_second)
    with pytest.raises(RuntimeError, match="second bind failed"):
        server.open_socket()

    assert first.close_calls == 1
    assert server.bound_listeners == []
    assert server.raw_sockets == []
    assert server.port is None
    assert server.unix_path is None
    assert server.protocol_ports == {}

    retry_sockets = iter((second, third))
    monkeypatch.setattr(server, "_bind_listener", lambda *_: [next(retry_sockets)])
    server.open_socket()
    assert len(server.bound_listeners) == 2
    server.close_socket()
    server.close_socket()
    assert second.close_calls == 1
    assert third.close_calls == 1


def test_listener_rollback_releases_real_address_for_immediate_rebind(monkeypatch):
    app = BlackBull()
    server = Server(app, listeners=[Listener(Tcp(1)), Listener(Tcp(2))])
    acquired = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    acquired.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    acquired.bind(("127.0.0.1", 0))
    acquired.listen()
    address = acquired.getsockname()
    calls = 0

    def fail_second(_listener, _cfg):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("second bind failed")
        return [acquired]

    monkeypatch.setattr(server, "_bind_listener", fail_second)
    with pytest.raises(RuntimeError, match="second bind failed"):
        server.open_socket()

    rebound = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        rebound.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        rebound.bind(address)
    finally:
        rebound.close()


def test_raw_protocol_bind_failure_refuses_partial_start(monkeypatch):
    app = BlackBull()

    @app.raw_handler("probe", port=9)
    async def probe(_reader, _writer, _ctx):
        pass

    server = Server(app, listeners=[Listener(Tcp(1))])
    http = _CloseProbe()
    monkeypatch.setattr(server, "_bind_listener", lambda *_: [http])
    monkeypatch.setattr(
        "blackbull.server.server.create_configured_sockets", lambda *_a, **_k: []
    )

    with pytest.raises(RuntimeError, match="probe"):
        server.open_socket()

    assert http.close_calls == 1
    assert server.bound_listeners == []
    assert server.protocol_ports == {}


def test_raw_protocol_socket_is_tracked_before_address_read(monkeypatch):
    app = BlackBull()

    @app.raw_handler("probe", port=0)
    async def probe(_reader, _writer, _ctx):
        pass

    server = Server(app, listeners=[Listener(Tcp(1))])
    http = _CloseProbe()
    raw = _GetSockNameFailure()
    monkeypatch.setattr(server, "_bind_listener", lambda *_: [http])
    monkeypatch.setattr(
        "blackbull.server.server.create_configured_sockets", lambda *_a, **_k: [raw]
    )

    with pytest.raises(OSError, match="getsockname failed"):
        server.open_socket()

    assert http.close_calls == 1
    assert raw.close_calls == 1


def test_distinct_socket_wrappers_for_one_fd_are_disarmed_before_close():
    owner = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    alias = socket.socket(fileno=owner.fileno())

    assert close_sockets([owner, alias]) is None

    assert owner.fileno() == -1
    assert alias.fileno() == -1


def test_distinct_socket_close_failures_are_grouped():
    first = _CloseProbe(fail=True)
    second = _CloseProbe(fail=True)

    cleanup_error = close_sockets([first, second])

    assert isinstance(cleanup_error, ExceptionGroup)
    assert len(cleanup_error.exceptions) == 2
    assert first.close_calls == 1
    assert second.close_calls == 1


@pytest.mark.asyncio
async def test_lifespan_startup_failure_reclaims_app_task():
    release = asyncio.Event()

    async def app(_scope, receive, send):
        await receive()
        await send({"type": "lifespan.startup.failed", "message": "broken"})
        await release.wait()

    manager = LifespanManager(app, cleanup_timeout=0.05)
    with pytest.raises(RuntimeError, match="broken"):
        await manager.__aenter__()

    assert manager._task.done()


@pytest.mark.asyncio
async def test_lifespan_shutdown_stall_is_bounded_and_reclaimed():
    release = asyncio.Event()

    async def app(_scope, receive, send):
        await receive()
        await send({"type": "lifespan.startup.complete"})
        await receive()
        await release.wait()

    manager = LifespanManager(app, cleanup_timeout=0.05)
    await manager.__aenter__()

    with pytest.raises(TimeoutError):
        await manager.__aexit__(None, None, None)

    assert manager._task.done()


@pytest.mark.asyncio
async def test_lifespan_shutdown_task_exception_is_reported():
    failure = RuntimeError("shutdown failed")

    async def app(_scope, receive, send):
        await receive()
        await send({"type": "lifespan.startup.complete"})
        await receive()
        raise failure

    manager = LifespanManager(app, cleanup_timeout=0.05)
    await manager.__aenter__()

    with pytest.raises(RuntimeError) as raised:
        await manager.__aexit__(None, None, None)

    assert raised.value is failure


@pytest.mark.asyncio
async def test_cancellation_before_startup_commit_propagates_and_closes_socket():
    startup_entered = asyncio.Event()
    release = asyncio.Event()

    async def app(_scope, receive, _send):
        await receive()
        startup_entered.set()
        await release.wait()

    server = Server(app)
    server.open_socket(0)
    sockets = list(server.raw_sockets)
    runner = asyncio.create_task(server.run())
    await asyncio.wait_for(startup_entered.wait(), timeout=2)
    runner.cancel()

    with pytest.raises(asyncio.CancelledError):
        await runner

    assert all(sock.fileno() == -1 for sock in sockets)


@pytest.mark.asyncio
async def test_cancellation_after_startup_commit_keeps_public_suppression():
    app = BlackBull()
    server = Server(app)
    server.open_socket(0)
    sockets = list(server.raw_sockets)
    runner = asyncio.create_task(server.run())
    for _ in range(100):
        running = getattr(server, "_running_servers", [])
        if running and all(item.is_serving() for item in running):
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("server did not commit startup")

    runner.cancel()
    await runner

    assert all(sock.fileno() == -1 for sock in sockets)


@pytest.mark.asyncio
async def test_stop_close_failure_finishes_cleanup_and_is_reported_by_run():
    shutdown_seen = asyncio.Event()

    async def app(_scope, receive, send):
        assert (await receive())["type"] == "lifespan.startup"
        await send({"type": "lifespan.startup.complete"})
        assert (await receive())["type"] == "lifespan.shutdown"
        shutdown_seen.set()
        await send({"type": "lifespan.shutdown.complete"})

    server = Server(app)
    server.open_socket(0)
    listening_sockets = list(server.raw_sockets)
    runner = asyncio.create_task(server.run())
    for _ in range(100):
        running = getattr(server, "_running_servers", [])
        if running and all(item.is_serving() for item in running):
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("server did not commit startup")

    actual_servers = list(server._running_servers)
    first_error = OSError("first server close failed")

    class CloseFailure(_AsyncServerProbe):
        def close(self):
            self.close_calls += 1
            raise first_error

    failure = CloseFailure()
    server._running_servers.insert(0, failure)
    release = asyncio.Event()
    connection = asyncio.create_task(release.wait())
    server._connection_tasks.add(connection)
    connection.add_done_callback(server._connection_tasks.discard)

    stopper = asyncio.create_task(server.stop(drain_timeout=0.0))
    try:
        with pytest.raises(OSError) as stopped:
            await asyncio.wait_for(stopper, timeout=2)
        with pytest.raises(OSError) as ran:
            await asyncio.wait_for(runner, timeout=2)

        assert stopped.value is first_error
        assert ran.value is first_error
        assert failure.wait_closed_calls >= 1
        assert all(not item.is_serving() for item in actual_servers)
        assert connection.done()
        assert shutdown_seen.is_set()
        assert all(sock.fileno() == -1 for sock in listening_sockets)
    finally:
        release.set()
        server._stopped_event.set()
        if not runner.done():
            runner.cancel()
        await asyncio.gather(runner, stopper, connection, return_exceptions=True)


@pytest.mark.asyncio
async def test_run_waits_for_in_progress_stop_despite_repeated_cancellation(
    monkeypatch,
):
    shutdown_seen = asyncio.Event()

    async def app(_scope, receive, send):
        assert (await receive())["type"] == "lifespan.startup"
        await send({"type": "lifespan.startup.complete"})
        assert (await receive())["type"] == "lifespan.shutdown"
        shutdown_seen.set()
        await send({"type": "lifespan.shutdown.complete"})

    server = Server(app)
    server.open_socket(0)
    listening_sockets = list(server.raw_sockets)
    address = ("127.0.0.1", server.port)
    connection_started = asyncio.Event()
    release = asyncio.Event()
    connection_tasks = []
    transports = []

    async def parked_connection(transport):
        connection_started.set()
        try:
            await release.wait()
        finally:
            transport.close()

    class TrackingProtocol(asyncio.Protocol):
        def connection_made(self, transport):
            transports.append(transport)
            task = asyncio.create_task(parked_connection(transport))
            connection_tasks.append(task)
            server._connection_tasks.add(task)
            task.add_done_callback(server._connection_tasks.discard)

    monkeypatch.setattr(
        server, "connection_protocol_factory", lambda _binding: TrackingProtocol
    )
    runner = asyncio.create_task(server.run())
    client_reader = None
    client_writer = None
    stopper = None
    try:
        for _ in range(100):
            running = getattr(server, "_running_servers", [])
            if running and all(item.is_serving() for item in running):
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("server did not commit startup")

        actual_servers = list(server._running_servers)
        client_reader, client_writer = await asyncio.open_connection(*address)
        await asyncio.wait_for(connection_started.wait(), timeout=2)
        stopper = asyncio.create_task(server.stop(drain_timeout=1.0))
        await asyncio.wait_for(server._stopped_event.wait(), timeout=2)
        assert not server._stop_done_event.is_set()

        runner.cancel()
        await asyncio.sleep(0)
        runner.cancel()
        await asyncio.sleep(0.05)

        assert not runner.done()
        assert not stopper.done()
        assert not connection_tasks[0].done()

        release.set()
        await asyncio.wait_for(stopper, timeout=2)
        await asyncio.wait_for(runner, timeout=2)

        assert all(task.done() for task in connection_tasks)
        assert all(transport.is_closing() for transport in transports)
        assert await asyncio.wait_for(client_reader.read(), timeout=2) == b""
        assert all(not item.is_serving() for item in actual_servers)
        assert shutdown_seen.is_set()
        assert all(sock.fileno() == -1 for sock in listening_sockets)
    finally:
        release.set()
        server._stopped_event.set()
        if client_writer is not None:
            client_writer.close()
            await client_writer.wait_closed()
        pending = [task for task in (runner, stopper) if task is not None]
        for task in pending:
            if not task.done():
                task.cancel()
        await asyncio.gather(*pending, *connection_tasks, return_exceptions=True)


def test_adopted_reload_fds_are_all_or_nothing(monkeypatch):
    first = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    first.bind(("127.0.0.1", 0))
    first.listen()
    fd = first.detach()
    monkeypatch.setenv("BB_INHERIT_FDS", f"{fd},999999")

    with pytest.raises(RuntimeError, match="999999"):
        adopt_inherited_sockets()

    with pytest.raises(OSError):
        os.fstat(fd)
    assert "BB_INHERIT_FDS" not in os.environ


@pytest.mark.parametrize(
    "spec",
    [",", "999999,", ",999999", "999998,,999999", "-1"],
)
def test_malformed_reload_fd_spec_is_rejected_before_adoption(monkeypatch, spec):
    socket_calls = []

    class UnexpectedSocket(socket.socket):
        def __init__(self, *args, **kwargs):
            socket_calls.append((args, kwargs))
            raise AssertionError("malformed fd spec reached socket adoption")

    monkeypatch.setenv("BB_INHERIT_FDS", spec)
    monkeypatch.setattr("blackbull.protocol.rsock.socket.socket", UnexpectedSocket)

    with pytest.raises(RuntimeError, match="Malformed BB_INHERIT_FDS"):
        adopt_inherited_sockets()

    assert socket_calls == []
    assert "BB_INHERIT_FDS" not in os.environ


def test_oversized_reload_fd_is_reported_as_adoption_failure(monkeypatch):
    oversized_fd = 2**100
    monkeypatch.setenv("BB_INHERIT_FDS", str(oversized_fd))

    with pytest.raises(RuntimeError, match="Failed to adopt inherited fd"):
        adopt_inherited_sockets()

    assert "BB_INHERIT_FDS" not in os.environ


def test_duplicate_reload_fd_is_rejected_and_closed(monkeypatch):
    listening = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listening.bind(("127.0.0.1", 0))
    listening.listen()
    fd = listening.detach()
    monkeypatch.setenv("BB_INHERIT_FDS", f"{fd},{fd}")

    with pytest.raises(RuntimeError, match="Duplicate inherited fd"):
        adopt_inherited_sockets()

    with pytest.raises(OSError):
        os.fstat(fd)
    assert "BB_INHERIT_FDS" not in os.environ


def test_duplicate_explicit_inherited_fd_is_rejected_before_commit():
    listening = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listening.bind(("127.0.0.1", 0))
    listening.listen()
    fd = listening.detach()
    server = Server(
        BlackBull(),
        listeners=[
            Listener(InheritedFd(fd)),
            Listener(InheritedFd(fd)),
        ],
    )

    with pytest.raises(RuntimeError, match="Duplicate listening fd"):
        server.open_socket()

    with pytest.raises(OSError):
        os.fstat(fd)
    assert server.bound_listeners == []
    assert server.raw_sockets == []


def test_multiworker_entrypoint_closes_master_sockets_if_logging_fails(
    monkeypatch,
):
    from blackbull.server import ASGIServer as ProductionServer

    created_servers = []
    acquired_sockets = []
    ipv4_addresses = []
    primary = RuntimeError("startup logging failed")

    def capture_server(*args, **kwargs):
        server = ProductionServer(*args, **kwargs)
        created_servers.append(server)
        return server

    def fail_starting_log(message, *_args, **_kwargs):
        if message == "Starting %d worker(s) on %s%s":
            acquired_sockets.extend(created_servers[0].raw_sockets)
            ipv4_addresses.extend(
                sock.getsockname()
                for sock in acquired_sockets
                if sock.family == socket.AF_INET
            )
            raise primary

    monkeypatch.setattr("blackbull.server.ASGIServer", capture_server)
    monkeypatch.setattr("blackbull.app.logger.info", fail_starting_log)

    with pytest.raises(RuntimeError) as raised:
        serve(BlackBull(), port=0, workers=2)

    assert raised.value is primary
    assert acquired_sockets
    assert all(sock.fileno() == -1 for sock in acquired_sockets)
    assert created_servers[0].bound_listeners == []
    assert created_servers[0].raw_sockets == []

    assert ipv4_addresses
    rebound = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    rebound.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        rebound.bind(ipv4_addresses[0])
    finally:
        rebound.close()


@pytest.mark.parametrize("failure_stage", ["constructor", "run"])
def test_multiworker_entrypoint_closes_detached_sockets_before_handoff(
    monkeypatch,
    failure_stage,
):
    from blackbull.server import ASGIServer as ProductionServer

    acquired_sockets = []
    ipv4_addresses = []
    primary = RuntimeError(f"{failure_stage} failed after detach")

    class CapturingServer(ProductionServer):
        detached = False

        @property
        def ssl_context(self):
            if self.detached:
                raise AssertionError("ssl_context was evaluated after detach")
            return super().ssl_context

        @ssl_context.setter
        def ssl_context(self, value):
            ProductionServer.ssl_context.fset(self, value)

        def _take_bound_listeners(self):
            acquired_sockets.extend(self.raw_sockets)
            ipv4_addresses.extend(
                sock.getsockname()
                for sock in acquired_sockets
                if sock.family == socket.AF_INET
            )
            listeners = super()._take_bound_listeners()
            self.detached = True
            return listeners

    def fail_constructor(*_args, **_kwargs):
        raise primary

    class FailOnRun:
        def __init__(self, *_args, **_kwargs):
            pass

        def run(self):
            raise primary

    monkeypatch.setattr("blackbull.server.ASGIServer", CapturingServer)
    if failure_stage == "constructor":
        monkeypatch.setattr(
            "blackbull.server.multiworker.MultiWorkerServer", fail_constructor
        )
    else:
        monkeypatch.setattr(
            "blackbull.server.multiworker.MultiWorkerServer", FailOnRun
        )

    try:
        with pytest.raises(RuntimeError) as raised:
            serve(BlackBull(), port=0, workers=2)

        assert raised.value is primary
        assert acquired_sockets
        assert all(sock.fileno() == -1 for sock in acquired_sockets)
        assert ipv4_addresses

        rebound = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        rebound.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            rebound.bind(ipv4_addresses[0])
        finally:
            rebound.close()
    finally:
        for sock in acquired_sockets:
            sock.close()


def test_exec_failure_restores_handoff_state(monkeypatch):
    listening = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listening.bind(("127.0.0.1", 0))
    listening.listen()
    fd = listening.fileno()
    original_flag = os.get_inheritable(fd)
    monkeypatch.setenv("BB_INHERIT_FDS", "previous")

    def fail_exec(*_args):
        raise OSError("exec failed")

    monkeypatch.setattr(os, "execvp", fail_exec)
    try:
        with pytest.raises(OSError, match="exec failed"):
            exec_self_with_sockets([listening], argv=["probe"])
        assert os.environ["BB_INHERIT_FDS"] == "previous"
        assert os.get_inheritable(fd) is original_flag
    finally:
        listening.close()


def test_spawn_failure_reclaims_real_child(monkeypatch):
    monkeypatch.setattr("blackbull.server.multiworker.REUSEPORT_SUPPORTED", False)
    server = MultiWorkerServer(BlackBull(), [], None, workers=2)
    context = multiprocessing.get_context("fork")
    stop = context.Event()
    child = context.Process(target=stop.wait, args=(60,))
    calls = 0
    child_pid = None

    def spawn(_worker_id):
        nonlocal calls, child_pid
        calls += 1
        if calls == 2:
            raise RuntimeError("second spawn failed")
        child.start()
        child_pid = child.pid
        return child

    monkeypatch.setattr(server, "_spawn_worker", spawn)
    try:
        with pytest.raises(RuntimeError, match="second spawn failed"):
            server._spawn_all()
        with pytest.raises(OSError) as missing:
            os.kill(child_pid, 0)
        assert missing.value.errno == errno.ESRCH
    finally:
        try:
            alive = child.is_alive()
        except ValueError:
            alive = False
        if alive:
            child.terminate()
            child.join(timeout=5)
        try:
            child.close()
        except ValueError:
            pass


@pytest.mark.parametrize(
    "operation,alive,stubborn",
    [
        ("is_alive", True, False),
        ("pid", False, False),
        ("terminate", True, False),
        ("join", True, False),
        ("kill", True, True),
        ("close", False, False),
    ],
)
def test_process_cleanup_error_does_not_skip_later_processes(
    monkeypatch, operation, alive, stubborn
):
    monkeypatch.setattr("blackbull.server.multiworker.REUSEPORT_SUPPORTED", False)
    server = MultiWorkerServer(BlackBull(), [], None, workers=1)
    broken = _ProcessProbe(operation, alive=alive, stubborn=stubborn)
    follower = _ProcessProbe(alive=False)
    server._processes = [broken, follower]

    cleanup_error = server._shutdown_all()

    errors = (cleanup_error.exceptions
              if isinstance(cleanup_error, ExceptionGroup)
              else (cleanup_error,))
    assert broken.error in errors
    assert "close" in follower.calls


def test_distinct_process_cleanup_failures_are_grouped(monkeypatch):
    monkeypatch.setattr("blackbull.server.multiworker.REUSEPORT_SUPPORTED", False)
    server = MultiWorkerServer(BlackBull(), [], None, workers=1)
    first = _ProcessProbe("close", alive=False)
    second = _ProcessProbe("close", alive=False)
    server._processes = [first, second]

    cleanup_error = server._shutdown_all()

    assert isinstance(cleanup_error, ExceptionGroup)
    assert cleanup_error.exceptions == (first.error, second.error)


def test_reload_groups_distinct_cleanup_failures(monkeypatch):
    monkeypatch.setattr("blackbull.server.multiworker.REUSEPORT_SUPPORTED", False)
    server = MultiWorkerServer(BlackBull(), [], None, workers=1, reload=True)
    termination_error = RuntimeError("terminate failed")
    watcher_error = RuntimeError("watcher failed")
    reclaim_error = RuntimeError("reclaim failed")
    monkeypatch.setattr(server, "_terminate_all", lambda: termination_error)
    monkeypatch.setattr(server, "_stop_watcher", lambda: watcher_error)
    monkeypatch.setattr(
        server,
        "_reclaim_processes",
        lambda *_args, **_kwargs: ([], reclaim_error),
    )

    with pytest.raises(ExceptionGroup) as raised:
        server._reload_now()

    assert raised.value.exceptions == (
        termination_error,
        watcher_error,
        reclaim_error,
    )


def test_process_cleanup_preserves_spawn_failure_identity(monkeypatch):
    monkeypatch.setattr("blackbull.server.multiworker.REUSEPORT_SUPPORTED", False)
    server = MultiWorkerServer(BlackBull(), [], None, workers=1)
    primary = RuntimeError("spawn failed")
    broken = _ProcessProbe("is_alive", alive=True)
    follower = _ProcessProbe(alive=False)
    monkeypatch.setattr(server, "_install_signal_handlers", lambda: None)

    def fail_spawn():
        server._processes = [broken, follower]
        raise primary

    monkeypatch.setattr(server, "_spawn_all", fail_spawn)

    with pytest.raises(RuntimeError) as raised:
        server.run()

    assert raised.value is primary
    assert "close" in follower.calls


def test_partial_spawn_uses_one_absolute_cleanup_deadline(monkeypatch):
    monkeypatch.setattr("blackbull.server.multiworker.REUSEPORT_SUPPORTED", False)
    server = MultiWorkerServer(
        BlackBull(), [], None, workers=2, shutdown_timeout=1.0
    )
    primary = RuntimeError("second spawn failed")
    clock = [0.0]
    timeouts = []

    class StubbornProcess(_ProcessProbe):
        def __init__(self):
            super().__init__(alive=True, stubborn=True)

        def join(self, timeout=None):
            timeouts.append(timeout)
            clock[0] += timeout or 0.0

        def kill(self):
            self.calls.append("kill")

    child = StubbornProcess()
    calls = 0

    def spawn(_worker_id):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise primary
        return child

    monkeypatch.setattr(
        "blackbull.server.multiworker.time.monotonic", lambda: clock[0]
    )
    monkeypatch.setattr(server, "_install_signal_handlers", lambda: None)
    monkeypatch.setattr(server, "_spawn_worker", spawn)

    with pytest.raises(RuntimeError) as raised:
        server.run()

    assert raised.value is primary
    assert sum(timeouts) <= server._shutdown_timeout


def test_started_process_is_tracked_if_post_start_logging_fails(monkeypatch):
    monkeypatch.setattr("blackbull.server.multiworker.REUSEPORT_SUPPORTED", False)
    server = MultiWorkerServer(BlackBull(), [], None, workers=1)
    child = _ProcessProbe()
    server._mp_ctx = SimpleNamespace(Process=lambda **_kwargs: child)
    primary = RuntimeError("log handler failed")

    def fail_log(*_args, **_kwargs):
        raise primary

    monkeypatch.setattr("blackbull.server.multiworker.logger.info", fail_log)

    with pytest.raises(RuntimeError) as raised:
        server._spawn_worker(0)

    assert raised.value is primary
    assert "terminate" in child.calls
    assert "close" in child.calls


def test_respawn_failure_preserves_primary_and_closes_dead_worker(monkeypatch):
    monkeypatch.setattr("blackbull.server.multiworker.REUSEPORT_SUPPORTED", False)
    server = MultiWorkerServer(BlackBull(), [], None, workers=1)
    dead = _ProcessProbe(alive=False)
    server._processes = [dead]
    primary = RuntimeError("replacement failed")
    monkeypatch.setattr(
        server, "_spawn_worker", lambda _worker_id: (_ for _ in ()).throw(primary)
    )

    with pytest.raises(RuntimeError) as raised:
        server._reap_and_respawn()

    assert raised.value is primary
    assert "close" in dead.calls


def test_invalid_worker_count_rolls_back_transferred_listener(monkeypatch):
    monkeypatch.setattr("blackbull.server.multiworker.REUSEPORT_SUPPORTED", False)
    listener = _CloseProbe()

    with pytest.raises(ValueError, match="workers must be >= 1"):
        MultiWorkerServer(
            BlackBull(), [(Listener(Tcp(1)), [listener])], None, workers=0
        )

    assert listener.close_calls == 1


def test_reuseport_partial_rebind_closes_master_and_worker_sockets(monkeypatch):
    from blackbull import env as env_module

    monkeypatch.setenv("BB_SOCKET_REUSEPORT", "1")
    env_module.reset_settings_cache()
    monkeypatch.setattr("blackbull.server.multiworker.REUSEPORT_SUPPORTED", True)
    master = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    master.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    master.bind(("127.0.0.1", 0))
    master.listen()
    worker = _CloseProbe()
    worker.family = socket.AF_INET
    calls = 0

    def partial_rebind(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return [worker] if calls == 1 else []

    monkeypatch.setattr(
        "blackbull.server.multiworker.create_configured_sockets", partial_rebind
    )

    with pytest.raises(RuntimeError, match="Failed to re-bind"):
        MultiWorkerServer(
            BlackBull(), [(Listener(Tcp(master.getsockname()[1])), [master])],
            None, workers=2
        )

    assert master.fileno() == -1
    assert worker.close_calls == 1


def test_watcher_start_failure_stops_started_thread(monkeypatch):
    monkeypatch.setattr("blackbull.server.multiworker.REUSEPORT_SUPPORTED", False)
    server = MultiWorkerServer(BlackBull(), [], None, workers=1, reload=True)

    created = []

    class Watcher:
        def __init__(self, *_args, **_kwargs):
            self.stop_event = threading.Event()
            self.thread = threading.Thread(target=self.stop_event.wait, daemon=True)
            created.append(self)

        def start(self):
            self.thread.start()
            raise RuntimeError("watcher start failed")

        def stop(self, timeout=2.0):
            self.stop_event.set()
            self.thread.join(timeout=timeout)

    monkeypatch.setattr("blackbull.server.reload.FileChangeWatcher", Watcher)
    monkeypatch.setattr(server, "_install_signal_handlers", lambda: None)
    monkeypatch.setattr(server, "_spawn_all", lambda: None)

    try:
        with pytest.raises(RuntimeError, match="watcher start failed"):
            server.run()
        assert not created[0].thread.is_alive()
    finally:
        if created and created[0].thread.is_alive():
            created[0].stop()
