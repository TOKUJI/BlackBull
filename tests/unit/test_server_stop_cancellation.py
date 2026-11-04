import asyncio
import contextlib
import logging
import socket

import pytest
import pytest_asyncio

from blackbull import BlackBull
from blackbull.client.http2 import HTTP2Client
from blackbull.grpc import GrpcServiceRegistry, decode_messages, encode_message
from blackbull.server.listener import Listener, Tcp
from blackbull.server.server import Server


@pytest_asyncio.fixture(params=["http1", "http2", "grpc"])
async def serving(monkeypatch, request):
    app = BlackBull()
    entered = asyncio.Event()
    release = asyncio.Event()

    @app.route(path="/")
    async def blocked(conn):
        entered.set()
        await release.wait()
        return "ok"

    registry = GrpcServiceRegistry()

    @registry.method("/shutdown.Echo/Call")
    async def echo(payload, context):
        entered.set()
        await release.wait()
        return payload

    app.enable_grpc(registry)

    server = Server(app, listeners=[Listener(Tcp(0)), Listener(Tcp(0))])
    server.open_socket()
    sockets = list(server.raw_sockets)
    addresses = [sock.getsockname() for sock in sockets]
    fds = [sock.fileno() for sock in sockets]
    runner = asyncio.create_task(server.run())
    writer = None
    client = None
    reply = None
    failures = {}
    try:
        if request.param == "http1":
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(*addresses[0]), timeout=2)
            writer.write(b"GET / HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
            await writer.drain()
            reply = asyncio.create_task(reader.read())
        else:
            client = HTTP2Client(*addresses[0])
            await asyncio.wait_for(client.__aenter__(), timeout=2)

            async def receive():
                if request.param == "grpc":
                    response = await client.request(
                        "POST", "/shutdown.Echo/Call",
                        headers=[("content-type", "application/grpc")],
                        body=encode_message(b"ok"))
                    assert response.trailers.get(b"grpc-status") == b"0"
                    assert decode_messages(response.body) == [(False, b"ok")]
                else:
                    response = await client.request("GET", "/")
                    assert response.body == b"ok"
                assert response.status == 200
                return b"ok"

            reply = asyncio.create_task(receive())
        await asyncio.wait_for(entered.wait(), timeout=2)

        stopped = asyncio.Event()
        original_close = socket.socket.close

        def close(sock):
            fd = sock.fileno()
            result = original_close(sock)
            if any(sock is listener for listener in sockets):
                stopped.set()
                error = failures.pop(fd, None)
                if error is not None:
                    raise error
            return result

        monkeypatch.setattr(socket.socket, "close", close)
        yield server, reply, runner, release, stopped, failures, fds
    finally:
        release.set()
        failures.clear()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(server.stop(drain_timeout=0.1), timeout=2)
        if reply is not None:
            await asyncio.wait_for(asyncio.gather(reply, return_exceptions=True), timeout=2)
        if client is not None:
            await asyncio.wait_for(client.__aexit__(None, None, None), timeout=2)
        if writer is not None:
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()
        await asyncio.wait_for(asyncio.gather(runner, return_exceptions=True), timeout=2)


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_with", ["cancel", "timeout", "wait_for"])
@pytest.mark.parametrize("close_fails", [False, True])
async def test_stop_preserves_cancellation_during_drain(
    cancel_with, close_fails, serving, caplog,
):
    server, reply, runner, release, stopped, failures, fds = serving
    close_error = OSError("listener close failed")
    if close_fails:
        failures[fds[0]] = close_error

    async def stop():
        if cancel_with == "timeout":
            async with asyncio.timeout(0.05):
                await server.stop(drain_timeout=30.0)
        elif cancel_with == "wait_for":
            await asyncio.wait_for(server.stop(drain_timeout=30.0), timeout=0.05)
        else:
            await server.stop(drain_timeout=30.0)

    with caplog.at_level(logging.ERROR, logger="blackbull.server.server"):
        caller = asyncio.create_task(stop())
        try:
            await asyncio.wait_for(stopped.wait(), timeout=1)
            if cancel_with == "cancel":
                caller.cancel("shutdown caller cancelled")
            expected = (asyncio.CancelledError if cancel_with == "cancel"
                        else TimeoutError)
            with pytest.raises(expected) as raised:
                await asyncio.wait_for(caller, timeout=2)
            if cancel_with == "cancel":
                assert raised.value.args == ("shutdown caller cancelled",)
                assert caller.cancelled()
            else:
                assert isinstance(raised.value.__cause__, asyncio.CancelledError)
        finally:
            caller.cancel()
            await asyncio.gather(caller, return_exceptions=True)

    errors = [record for record in caplog.records
              if record.levelno >= logging.ERROR]
    assert not any(
        record.exc_info and isinstance(record.exc_info[1], asyncio.CancelledError)
        for record in errors
    )
    if close_fails:
        assert sum(bool(record.exc_info and record.exc_info[1] is close_error)
                   for record in errors) == 1
        with pytest.raises(OSError) as repeated:
            await asyncio.wait_for(server.stop(), timeout=1)
        assert repeated.value is close_error
    else:
        assert not errors
        await asyncio.wait_for(server.stop(), timeout=1)

    release.set()
    assert (await asyncio.wait_for(reply, timeout=2)).endswith(b"ok")
    if close_fails:
        with pytest.raises(OSError) as ran:
            await asyncio.wait_for(runner, timeout=2)
        assert ran.value is close_error
    else:
        await asyncio.wait_for(runner, timeout=2)


@pytest.mark.asyncio
async def test_stop_still_groups_cleanup_failures_without_cancellation(serving):
    server, reply, runner, release, _stopped, failures, fds = serving
    close_errors = [OSError("first close failed"), OSError("second close failed")]
    failures.update(zip(fds, close_errors))
    release.set()
    await asyncio.wait_for(reply, timeout=2)

    with pytest.raises(ExceptionGroup) as raised:
        await server.stop()
    assert raised.value.exceptions == tuple(close_errors)

    with pytest.raises(ExceptionGroup) as repeated:
        await asyncio.wait_for(server.stop(), timeout=1)
    assert repeated.value is raised.value
    with pytest.raises(ExceptionGroup) as ran:
        await asyncio.wait_for(runner, timeout=2)
    assert ran.value is raised.value
