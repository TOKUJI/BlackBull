import asyncio

import pytest

from blackbull.server.connection_protocol import ConnectionProtocol
from blackbull.server.connection_actor import ConnectionActor
from tests.caps._drive import make_app
from tests.harness import served


pytestmark = [pytest.mark.integration, pytest.mark.asyncio, pytest.mark.timeout(30)]


async def test_normal_service_lingers_once(monkeypatch):
    linger = ConnectionProtocol.linger_close
    calls = 0

    async def counted_close(conn, *args, **kwargs):
        nonlocal calls
        calls += 1
        await linger(conn, *args, **kwargs)

    monkeypatch.setattr(ConnectionProtocol, 'linger_close', counted_close)
    async with served(make_app()) as s:
        wire = await s.wire()
        await wire.send(b'GET / HTTP/1.1\r\nhost: localhost\r\nconnection: close\r\n\r\n')
        assert (await wire.response()).status == 200
        assert await wire.closed()
    assert calls == 1


@pytest.mark.parametrize('error', [RuntimeError, asyncio.CancelledError])
async def test_early_actor_exit_still_closes_the_connection(monkeypatch, error):
    async def fail_before_close(actor):
        raise error()

    monkeypatch.setattr(ConnectionActor, 'run', fail_before_close)
    async with served(make_app()) as s:
        wire = await s.wire()
        assert await wire.closed()
        async with asyncio.timeout(5):
            while s.server.active_connections or s.server.held_connections:
                await asyncio.sleep(0.001)


async def test_service_recovers_while_a_refusal_is_still_closing(monkeypatch):
    waiting, release = asyncio.Event(), asyncio.Event()
    linger = ConnectionProtocol.linger_close
    first_close = True

    async def held_close(conn, *args, **kwargs):
        nonlocal first_close
        if first_close:
            first_close = False
            waiting.set()
            await release.wait()
        await linger(conn, *args, **kwargs)

    monkeypatch.setattr(ConnectionProtocol, 'linger_close', held_close)
    async with served(make_app(), max_connections=1) as s:
        try:
            first = await s.wire()
            await first.send(b'GET / HTTP/1.1\r\nhost: localhost\r\n\r\n')
            assert (await first.response()).status == 200
            refused = await s.wire()
            assert (await refused.response()).status == 503
            await asyncio.wait_for(waiting.wait(), 5)
            first.writer.close()
            await first.writer.wait_closed()
            async with asyncio.timeout(5):
                while s.server.active_connections:
                    await asyncio.sleep(0.001)
            assert s.server.held_connections >= 1
            next_ = await s.wire()
            await next_.send(b'GET / HTTP/1.1\r\nhost: localhost\r\nconnection: close\r\n\r\n')
            assert (await next_.response()).status == 200
            assert not release.is_set()
        finally:
            release.set()
