"""What ``LifespanManager`` concludes from the answer the application gave."""
from __future__ import annotations

import asyncio
import contextlib

import pytest

from blackbull import BlackBull
from blackbull.asgi import ASGIEvent
from blackbull.server.server import LifespanManager, Server

PATIENCE = 5.0


def _scripted_app(*, startup: str | None = ASGIEvent.LIFESPAN_STARTUP_COMPLETE,
                  shutdown: str | None = ASGIEvent.LIFESPAN_SHUTDOWN_COMPLETE,
                  message: str = '', raise_after_ack: BaseException | None = None):
    """Answers each phase with the given event; ``None`` returns without one."""
    async def app(scope, receive, send):
        assert scope['type'] == 'lifespan'
        while True:
            event = await receive()
            if event['type'] == ASGIEvent.LIFESPAN_STARTUP:
                if startup is None:
                    return
                await send({'type': startup, 'message': message})
                if raise_after_ack is not None:
                    raise raise_after_ack
            elif event['type'] == ASGIEvent.LIFESPAN_SHUTDOWN:
                if shutdown is None:
                    return
                await send({'type': shutdown, 'message': message})
                return
    return app


async def _cancel_leftover_task(manager: LifespanManager) -> None:
    task = manager._task
    if task is not None and not task.done():
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def _bounded_task(coro, patience: float = PATIENCE) -> asyncio.Task:
    # Not ``wait_for``: ``__aexit__`` raises its own ``TimeoutError``, which
    # would be indistinguishable from a hang.  Callers read ``task.done()``.
    task = asyncio.ensure_future(coro)
    done, _pending = await asyncio.wait({task}, timeout=patience)
    if not done:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    return task


@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_shutdown_failed_is_not_reported_as_completion():
    manager = LifespanManager(
        _scripted_app(shutdown=ASGIEvent.LIFESPAN_SHUTDOWN_FAILED,
                      message='flush to disk failed'),
        cleanup_timeout=PATIENCE)
    await manager.__aenter__()
    try:
        exit_task = await _bounded_task(manager.__aexit__(None, None, None))
        assert exit_task.done(), '__aexit__ never returned'
        error = exit_task.exception()
        assert error is not None, '__aexit__ reported a failed shutdown as success'
        assert 'flush to disk failed' in str(error), str(error)
    finally:
        await _cancel_leftover_task(manager)


@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_shutdown_complete_still_returns_normally():
    manager = LifespanManager(_scripted_app(), cleanup_timeout=PATIENCE)
    await manager.__aenter__()
    try:
        exit_task = await _bounded_task(manager.__aexit__(None, None, None))
        assert exit_task.done() and exit_task.exception() is None, (
            f'clean shutdown reported {exit_task.exception()!r}')
        assert exit_task.result() is False
        assert manager._task.done()
    finally:
        await _cancel_leftover_task(manager)


@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_app_returning_before_its_first_lifespan_message_is_served_without_lifespan():
    async def app(scope, receive, send):
        return

    manager = LifespanManager(app, cleanup_timeout=PATIENCE)
    try:
        entered = await _bounded_task(manager.__aenter__())
        assert entered.done() and entered.exception() is None, (
            f'{entered.exception()!r}')
        assert entered.result() is manager
        exit_task = await _bounded_task(manager.__aexit__(None, None, None))
        assert exit_task.done() and exit_task.exception() is None, (
            f'{exit_task.exception()!r}')
    finally:
        await _cancel_leftover_task(manager)


@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_app_raising_after_acking_startup_fails_the_exit():
    manager = LifespanManager(
        _scripted_app(raise_after_ack=RuntimeError('boom after ack')),
        cleanup_timeout=PATIENCE)
    entered = await _bounded_task(manager.__aenter__())
    try:
        assert entered.done() and entered.exception() is None, (
            f'the ack was not honoured: {entered.exception()!r}')
        exit_task = await _bounded_task(manager.__aexit__(None, None, None))
        assert exit_task.done(), '__aexit__ never returned'
        error = exit_task.exception()
        assert error is not None, 'a dead lifespan task was reported as success'
        assert 'boom after ack' in str(error), str(error)
    finally:
        await _cancel_leftover_task(manager)


@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_caller_cancelled_during_the_drain_is_still_cancelled():
    released = asyncio.Event()

    async def app(scope, receive, send):
        try:
            while True:
                event = await receive()
                if event['type'] == ASGIEvent.LIFESPAN_STARTUP:
                    await send({'type': ASGIEvent.LIFESPAN_STARTUP_COMPLETE})
                elif event['type'] == ASGIEvent.LIFESPAN_SHUTDOWN:
                    await send({'type': ASGIEvent.LIFESPAN_SHUTDOWN_COMPLETE})
                    return
        finally:
            # Survive the manager's reclaim, so the drain is still open when
            # the caller is cancelled.
            while not released.is_set():
                with contextlib.suppress(asyncio.CancelledError):
                    await released.wait()

    manager = LifespanManager(app, cleanup_timeout=30.0)
    await manager.__aenter__()
    caller = asyncio.ensure_future(manager.__aexit__(None, None, None))
    try:
        await asyncio.sleep(0.2)
        assert not caller.done(), 'the drain was over before the cancel landed'
        caller.cancel()
        await asyncio.wait({caller}, timeout=PATIENCE)
        assert caller.done(), '__aexit__ never returned after the cancel'
        assert caller.cancelled(), (
            'the caller resumed uncancelled: the cancellation was swallowed')
    finally:
        released.set()
        await _cancel_leftover_task(manager)


@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_caller_cancelled_during_the_shutdown_race_is_still_cancelled():
    async def app(scope, receive, send):
        while True:
            event = await receive()
            if event['type'] == ASGIEvent.LIFESPAN_STARTUP:
                await send({'type': ASGIEvent.LIFESPAN_STARTUP_COMPLETE})
            elif event['type'] == ASGIEvent.LIFESPAN_SHUTDOWN:
                await asyncio.sleep(30)

    manager = LifespanManager(app, cleanup_timeout=30.0)
    await manager.__aenter__()
    caller = asyncio.ensure_future(manager.__aexit__(None, None, None))
    try:
        await asyncio.sleep(0)
        caller.cancel()
        await asyncio.wait({caller}, timeout=PATIENCE)
        assert caller.done(), '__aexit__ never returned after the cancel'
        assert caller.cancelled(), 'the cancellation was swallowed'
    finally:
        await _cancel_leftover_task(manager)


@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_shutdown_without_startup_raises_naming_both_calls():
    app = BlackBull()

    @app.route(path='/')
    async def index():
        return 'ok'

    server = Server(app)
    shutdown = await _bounded_task(server.shutdown())
    assert shutdown.done(), 'shutdown() never returned'
    error = shutdown.exception()
    assert isinstance(error, RuntimeError), repr(error)
    assert 'startup()' in str(error) and 'shutdown()' in str(error), str(error)


@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_shutdown_after_a_failed_startup_is_quiet():
    app = BlackBull()

    @app.route(path='/')
    async def index():
        return 'ok'

    @app.on_startup
    async def _boom():
        raise RuntimeError('startup hook failed')

    server = Server(app)
    with pytest.raises(RuntimeError, match='startup hook failed'):
        await server.startup()
    shutdown = await _bounded_task(server.shutdown())
    assert shutdown.done() and shutdown.exception() is None, (
        f'shutdown() after a failed startup: {shutdown.exception()!r}')


@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_second_aexit_is_a_no_op():
    manager = LifespanManager(_scripted_app(), cleanup_timeout=PATIENCE)
    await manager.__aenter__()
    try:
        await manager.__aexit__(None, None, None)
        again = await _bounded_task(manager.__aexit__(None, None, None))
        assert again.done() and again.exception() is None, (
            f'the second exit raised {again.exception()!r}')
    finally:
        await _cancel_leftover_task(manager)

