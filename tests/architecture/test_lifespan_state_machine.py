"""What the server concludes from the lifespan an application ran.

A BlackBull app runs it through its native hook; a foreign ASGI app runs it
through ``ASGIApp``, which speaks the ASGI lifespan protocol to it.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import signal

import pytest

from blackbull import ASGIApp, BlackBull
from blackbull.asgi import ASGIEvent
from blackbull.env import reset_settings_cache
from blackbull.server.server import LifespanManager, Server

PATIENCE = 5.0


@pytest.fixture
def native_lane(monkeypatch):
    # The dual-path lane would wrap a bare callable in ASGIApp, which has a hook.
    monkeypatch.setenv('BB_FORCE_ASGI_SCOPE', '0')
    reset_settings_cache()
    yield
    monkeypatch.undo()
    reset_settings_cache()


def _scripted_app(*, startup: str | None = ASGIEvent.LIFESPAN_STARTUP_COMPLETE,
                  shutdown: str | None = ASGIEvent.LIFESPAN_SHUTDOWN_COMPLETE,
                  message: str = '', raise_after_ack: BaseException | None = None,
                  died: asyncio.Event | None = None, ended: asyncio.Event | None = None):
    """Answers each phase with the given event; ``None`` returns without one."""
    async def app(scope, receive, send):
        assert scope['type'] == 'lifespan'
        try:
            while True:
                event = await receive()
                if event['type'] == ASGIEvent.LIFESPAN_STARTUP:
                    if startup is None:
                        return
                    await send({'type': startup, 'message': message})
                    if raise_after_ack is not None:
                        if died is not None:
                            died.set()
                        raise raise_after_ack
                elif event['type'] == ASGIEvent.LIFESPAN_SHUTDOWN:
                    if shutdown is None:
                        return
                    await send({'type': shutdown, 'message': message})
                    return
        finally:
            if ended is not None:
                ended.set()
    return ASGIApp(app)


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
async def test_a_task_death_after_startup_is_reported_at_once(caplog):
    died = asyncio.Event()
    manager = LifespanManager(
        _scripted_app(raise_after_ack=RuntimeError('boom'), died=died))
    with pytest.raises(RuntimeError):
        async with manager:
            await asyncio.wait_for(died.wait(), PATIENCE)
            for _ in range(3):
                await asyncio.sleep(0)
            assert any('failed after startup' in r.getMessage()
                       for r in caplog.records), (
                'the death surfaced only at shutdown')


@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_a_healthy_lifespan_is_not_reported(caplog):
    async with LifespanManager(_scripted_app()):
        await asyncio.sleep(0)
    assert not any('failed after startup' in r.getMessage()
                   for r in caplog.records)


@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_shutdown_failed_is_not_reported_as_completion():
    manager = LifespanManager(
        _scripted_app(shutdown=ASGIEvent.LIFESPAN_SHUTDOWN_FAILED,
                      message='flush to disk failed'),
        cleanup_timeout=PATIENCE)
    await manager.__aenter__()
    exit_task = await _bounded_task(manager.__aexit__(None, None, None))
    assert exit_task.done(), '__aexit__ never returned'
    error = exit_task.exception()
    assert error is not None, '__aexit__ reported a failed shutdown as success'
    assert 'flush to disk failed' in str(error), str(error)


@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_shutdown_complete_still_returns_normally():
    ended = asyncio.Event()
    manager = LifespanManager(_scripted_app(ended=ended), cleanup_timeout=PATIENCE)
    await manager.__aenter__()
    exit_task = await _bounded_task(manager.__aexit__(None, None, None))
    assert exit_task.done() and exit_task.exception() is None, (
        f'clean shutdown reported {exit_task.exception()!r}')
    assert exit_task.result() is False
    assert ended.is_set()


@pytest.mark.asyncio
@pytest.mark.timeout(60)
@pytest.mark.parametrize('kind', ['returns', 'raises-before-reading', 'reads-then-returns'])
async def test_an_app_without_lifespan_support_is_served_without_one(kind, caplog):
    calls = []

    async def app(scope, receive, send):
        calls.append(scope['type'])
        if kind == 'raises-before-reading':
            raise KeyError('path')  # an HTTP-only app reading a request key
        if kind == 'reads-then-returns':
            await receive()

    manager = LifespanManager(ASGIApp(app), cleanup_timeout=PATIENCE)
    with caplog.at_level(logging.INFO, logger='blackbull'):
        entered = await _bounded_task(manager.__aenter__())
        assert entered.done() and entered.exception() is None, (
            f'{entered.exception()!r}')
        exit_task = await _bounded_task(manager.__aexit__(None, None, None))
        assert exit_task.done() and exit_task.exception() is None, (
            f'{exit_task.exception()!r}')

    assert calls == ['lifespan']
    assert any('unsupported' in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
@pytest.mark.timeout(60)
@pytest.mark.parametrize('kind', ['sends-failed', 'raises-after-reading'])
async def test_a_failed_startup_refuses_and_reclaims_the_app(kind):
    ended = asyncio.Event()

    async def app(scope, receive, send):
        try:
            await receive()
            if kind == 'sends-failed':
                await send({'type': ASGIEvent.LIFESPAN_STARTUP_FAILED,
                            'message': 'no database'})
                await asyncio.sleep(30)
            raise RuntimeError('no database')
        finally:
            ended.set()

    manager = LifespanManager(ASGIApp(app), cleanup_timeout=PATIENCE)
    with pytest.raises(RuntimeError, match='no database'):
        await manager.__aenter__()
    assert ended.is_set()


@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_an_unexpected_startup_answer_fails_startup():
    manager = LifespanManager(
        _scripted_app(startup=ASGIEvent.LIFESPAN_SHUTDOWN_COMPLETE),
        cleanup_timeout=PATIENCE)
    with pytest.raises(RuntimeError, match='lifespan.shutdown.complete'):
        await manager.__aenter__()


@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_app_raising_after_acking_startup_fails_the_exit():
    manager = LifespanManager(
        _scripted_app(raise_after_ack=RuntimeError('boom after ack')),
        cleanup_timeout=PATIENCE)
    entered = await _bounded_task(manager.__aenter__())
    assert entered.done() and entered.exception() is None, (
        f'the ack was not honoured: {entered.exception()!r}')
    exit_task = await _bounded_task(manager.__aexit__(None, None, None))
    assert exit_task.done(), '__aexit__ never returned'
    error = exit_task.exception()
    assert error is not None, 'a dead lifespan task was reported as success'
    assert 'boom after ack' in str(error), str(error)


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
            # Survive the reclaim, so the drain is still open when the caller
            # is cancelled.
            while not released.is_set():
                with contextlib.suppress(asyncio.CancelledError):
                    await released.wait()

    manager = LifespanManager(ASGIApp(app), cleanup_timeout=30.0)
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

    manager = LifespanManager(ASGIApp(app), cleanup_timeout=30.0)
    await manager.__aenter__()
    caller = asyncio.ensure_future(manager.__aexit__(None, None, None))
    await asyncio.sleep(0)
    caller.cancel()
    await asyncio.wait({caller}, timeout=PATIENCE)
    assert caller.done(), '__aexit__ never returned after the cancel'
    assert caller.cancelled(), 'the cancellation was swallowed'


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
    await manager.__aexit__(None, None, None)
    again = await _bounded_task(manager.__aexit__(None, None, None))
    assert again.done() and again.exception() is None, (
        f'the second exit raised {again.exception()!r}')


@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_the_hook_is_found_through_wrapped():
    bb = BlackBull()
    ran = []

    @bb.on_startup
    async def _start():
        ran.append('startup')

    @bb.on_shutdown
    async def _stop():
        ran.append('shutdown')

    async def wrapper(conn, receive, send):
        await bb(conn, receive, send)
    wrapper.__wrapped__ = bb

    async with LifespanManager(wrapper):
        assert ran == ['startup']
    assert ran == ['startup', 'shutdown']


@pytest.mark.asyncio
@pytest.mark.timeout(60)
@pytest.mark.parametrize('adapted', [False, True], ids=['native', 'adapter'])
async def test_a_blackbull_startup_failure_keeps_its_cause(adapted):
    class Boom(Exception):
        pass

    app = BlackBull()

    @app.on_startup
    async def _boom():
        raise Boom('db unreachable')

    manager = LifespanManager(ASGIApp(app) if adapted else app)
    with pytest.raises(RuntimeError, match='db unreachable') as raised:
        await manager.__aenter__()
    assert isinstance(raised.value.__cause__, Boom)


@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_an_app_without_a_hook_is_warned_and_runs_no_lifespan(native_lane):
    calls = []

    async def bare(conn, receive, send):
        calls.append(conn)

    with pytest.warns(UserWarning, match='No lifespan hook') as warned:
        server = Server(bare)
    message = str(warned[0].message)
    assert 'ASGIApp' in message and '__wrapped__' in message, message
    await server.startup()
    await server.shutdown()
    assert calls == []


@pytest.mark.timeout(60)
def test_restore_accepts_a_handler_installed_from_c():
    from blackbull.server.server import _SigtermCapture

    before = signal.getsignal(signal.SIGTERM)
    capture = _SigtermCapture(server=None, drain_timeout=0.0)
    try:
        capture.installed = True
        capture._previous = None       # what ``signal.signal()`` returns for C

        assert capture.__exit__(None, None, None) is False

        capture._captured = [signal.SIGTERM]
        assert capture.__exit__(None, None, None) is False
    finally:
        signal.signal(signal.SIGTERM, before)


@pytest.mark.timeout(60)
def test_restore_puts_sig_dfl_back_when_the_previous_handler_is_unknown():
    from blackbull.server.server import _SigtermCapture

    before = signal.getsignal(signal.SIGTERM)
    capture = _SigtermCapture(server=None, drain_timeout=0.0)
    try:
        capture.installed = True
        capture._previous = None
        signal.signal(signal.SIGTERM, capture._handle)   # as __enter__ leaves it
        capture.__exit__(None, None, None)
        assert signal.getsignal(signal.SIGTERM) is signal.SIG_DFL
    finally:
        signal.signal(signal.SIGTERM, before)


@pytest.mark.asyncio
@pytest.mark.parametrize('fails', [True, False])
async def test_a_startup_hook_failure_fails_the_lifespan_without_hanging(fails):
    app = BlackBull()
    ran = []

    @app.intercept('app_startup')
    async def hook(event):
        ran.append(True)
        if fails:
            raise RuntimeError('startup blew up')

    mgr = LifespanManager(app)
    if fails:
        with pytest.raises(RuntimeError, match='startup blew up'):
            await asyncio.wait_for(mgr.__aenter__(), timeout=3.0)
    else:
        await asyncio.wait_for(mgr.__aenter__(), timeout=3.0)
    await mgr.__aexit__(None, None, None)
    assert ran == [True]
