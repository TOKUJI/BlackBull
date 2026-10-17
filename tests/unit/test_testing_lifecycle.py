import asyncio
import threading

import pytest

from blackbull.testing import WebSocketDisconnect, WebSocketTestSession, _LoopThread


@pytest.mark.parametrize('failure', ['raise', 'return', 'timeout', 'cancel', 'unexpected'])
def test_websocket_failed_entry_reclaims_app_and_loop(failure):
    finished = threading.Event()

    async def app(conn, receive, send):
        try:
            assert (await receive())['type'] == 'websocket.connect'
            if failure == 'raise':
                raise ValueError('before accept')
            if failure == 'return':
                return
            if failure == 'cancel':
                raise asyncio.CancelledError
            if failure == 'unexpected':
                await send({'type': 'websocket.send', 'text': 'too early'})
            await asyncio.Event().wait()
        finally:
            finished.set()

    session = WebSocketTestSession(app, '/', timeout=.1)
    expected = {'raise': ValueError, 'return': WebSocketDisconnect,
                'timeout': TimeoutError, 'cancel': asyncio.CancelledError,
                'unexpected': RuntimeError}[failure]
    try:
        with pytest.raises(expected):
            session.__enter__()
        assert finished.is_set()
        assert not session._loop_thread._thread.is_alive()
        assert session._loop_thread.loop.is_closed()
        assert session._app_task.done()
        session.close()
        session.close()
    finally:
        if session._loop_thread._thread.is_alive():
            session._loop_thread.stop()


def test_loop_timeout_waits_for_coroutine_cancellation():
    loop = _LoopThread()
    finished = threading.Event()
    async def work():
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(.01)
            finished.set()
    loop.start()
    try:
        with pytest.raises(TimeoutError):
            loop.run_coro(work(), timeout=.05)
        assert finished.is_set()
        assert loop.run_coro(asyncio.sleep(0, result='usable')) == 'usable'
    finally:
        loop.stop()


def test_owned_loop_drains_tasks_and_async_generators():
    loop = _LoopThread()
    task_done, generator_done = threading.Event(), threading.Event()
    retained = []
    async def values():
        try:
            yield 1
        finally:
            await asyncio.sleep(0)
            generator_done.set()
    async def pending():
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            task_done.set()
    async def start():
        gen = values()
        retained.append(gen)
        assert await anext(gen) == 1
        retained.append(asyncio.create_task(pending()))
        await asyncio.sleep(0)
    loop.start()
    loop.run_coro(start())
    loop.stop()
    assert task_done.is_set()
    assert generator_done.is_set()
    assert not loop._thread.is_alive()
    assert loop.loop.is_closed()


def test_websocket_receive_failure_reclaims_loop():
    async def app(conn, receive, send):
        await receive()
        await send({'type': 'websocket.accept'})
        await receive()
        raise ValueError('after accept')
    session = WebSocketTestSession(app, '/', timeout=.1)
    with pytest.raises(ValueError, match='after accept'):
        with session:
            session.send_text('fail')
            session.receive_text()
    assert session._app_task.done()
    assert not session._loop_thread._thread.is_alive()


def test_websocket_handshake_rejection_and_close_are_idempotent():
    async def app(conn, receive, send):
        await receive()
        await send({'type': 'websocket.close', 'code': 1008})
    session = WebSocketTestSession(app, '/')
    with pytest.raises(WebSocketDisconnect) as error:
        session.__enter__()
    assert error.value.code == 1008
    session.close()
    session.close()
    assert not session._loop_thread._thread.is_alive()


@pytest.mark.parametrize('async_wrapper', [False, True])
def test_client_lifespan_http_and_websockets_share_resources(async_wrapper):
    from blackbull.testing import TestClient
    from blackbull.connection import Connection

    active = set()
    resources = {}

    async def app(conn, receive, send):
        kind = conn.type if isinstance(conn, Connection) else conn['type']
        if kind == 'lifespan':
            await receive()
            resources['loop'] = asyncio.get_running_loop()
            await send({'type': 'lifespan.startup.complete'})
            await receive()
            assert not active
            await send({'type': 'lifespan.shutdown.complete'})
            return
        future = resources['loop'].create_future()
        resources['loop'].call_soon(future.set_result, 'ready')
        assert await future == 'ready'
        if kind == 'http':
            await send({'type': 'http.response.start', 'status': 200, 'headers': []})
            await send({'type': 'http.response.body', 'body': b'ok'})
            return
        await receive()
        if conn.path == '/fail':
            raise ValueError('handshake failed')
        active.add(conn.path)
        try:
            await send({'type': 'websocket.accept'})
            while (event := await receive())['type'] != 'websocket.disconnect':
                await send({'type': 'websocket.send', 'text': conn.path + event['text']})
        finally:
            active.remove(conn.path)

    def exercise():
        client = TestClient(app)
        with pytest.raises(RuntimeError):
            client.websocket_connect('/')
        with client:
            with pytest.raises(RuntimeError):
                client.__enter__()
            first = client.websocket_connect('/one').__enter__()
            second = client.websocket_connect('/two').__enter__()
            with pytest.raises(ValueError, match='handshake failed'):
                with client.websocket_connect('/fail'):
                    pass
            first.send_text('1')
            second.send_text('2')
            assert first.receive_text() == '/one1'
            assert second.receive_text() == '/two2'
            first.close()
            assert client.get('/').text == 'ok'
            second.send_text('3')
            assert second.receive_text() == '/two3'
            stale = client.websocket_connect('/stale')
        assert not active
        second.close()
        with pytest.raises(RuntimeError):
            client.websocket_connect('/')
        with pytest.raises(RuntimeError):
            client.get('/')
        with client:
            with pytest.raises(RuntimeError):
                stale.__enter__()
            assert client.get('/').text == 'ok'
            with client.websocket_connect('/new') as fresh:
                fresh.send_text('4')
                assert fresh.receive_text() == '/new4'
    if async_wrapper:
        asyncio.run(asyncio.to_thread(exercise))
    else:
        exercise()


@pytest.mark.parametrize('target', ['websocket', 'client'])
@pytest.mark.parametrize('phase', ['entry', 'body'])
def test_cleanup_failure_preserves_original_exception(monkeypatch, caplog, target, phase):
    from blackbull.testing import TestClient

    async def app(conn, receive, send):
        await receive()
        if phase == 'entry':
            raise ValueError('original failure')
        if target == 'client':
            await send({'type': 'lifespan.startup.complete'})
            await receive()
            await send({'type': 'lifespan.shutdown.complete'})
        else:
            await send({'type': 'websocket.accept'})
            await receive()

    stop = _LoopThread.stop
    def failing_stop(self, *args, **kwargs):
        stop(self, *args, **kwargs)
        raise TimeoutError('cleanup failure')
    monkeypatch.setattr(_LoopThread, 'stop', failing_stop)
    context = TestClient(app) if target == 'client' else WebSocketTestSession(app, '/')
    with pytest.raises((ValueError, RuntimeError), match='original failure'):
        with context:
            raise ValueError('original failure')
    assert 'cleanup failure' in caplog.text


def test_start_timeout_cannot_leave_a_late_loop_running(monkeypatch):
    loop = _LoopThread()
    release = threading.Event()
    get_loop = asyncio.Runner.get_loop
    stop = loop.stop

    def delayed_get_loop(runner):
        assert release.wait(2)
        return get_loop(runner)

    def release_and_stop(*args, **kwargs):
        release.set()
        return stop(*args, **kwargs)

    monkeypatch.setattr(asyncio.Runner, 'get_loop', delayed_get_loop)
    monkeypatch.setattr(loop._ready, 'wait', lambda timeout: False)
    monkeypatch.setattr(loop, 'stop', release_and_stop)
    try:
        with pytest.raises(TimeoutError, match='starting'):
            loop.start()
        assert not loop._thread.is_alive()
    finally:
        release.set()
        stop()


@pytest.mark.parametrize('target', ['websocket', 'client'])
def test_normal_context_exit_does_not_hide_cleanup_error_in_except(monkeypatch, target):
    from blackbull.testing import TestClient
    async def app(conn, receive, send):
        await receive()
        if target == 'client':
            await send({'type': 'lifespan.startup.complete'})
            await receive()
            await send({'type': 'lifespan.shutdown.complete'})
        else:
            await send({'type': 'websocket.accept'})
            await receive()

    stop = _LoopThread.stop
    def failing_stop(self):
        stop(self)
        raise RuntimeError('cleanup failure')
    monkeypatch.setattr(_LoopThread, 'stop', failing_stop)
    try:
        raise ValueError('already handled')
    except ValueError:
        with pytest.raises(RuntimeError, match='cleanup failure'):
            context = TestClient(app) if target == 'client' else WebSocketTestSession(app, '/')
            with context:
                pass


def test_drain_submission_failure_preserves_app_error(monkeypatch):
    submit = asyncio.run_coroutine_threadsafe
    calls = 0
    def failing_submit(coro, loop):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError('submission failed')
        return submit(coro, loop)
    async def fail():
        raise ValueError('app failed')
    loop = _LoopThread()
    loop.start()
    try:
        monkeypatch.setattr(asyncio, 'run_coroutine_threadsafe', failing_submit)
        with pytest.raises(ValueError, match='app failed'):
            loop.run_coro(fail())
    finally:
        loop.stop()


def test_failed_submission_closes_both_coroutines(monkeypatch):
    import inspect

    submitted = []
    def fail_submit(coro, loop):
        submitted.append(coro)
        raise RuntimeError('submission failed')
    loop = _LoopThread()
    loop.start()
    work = asyncio.sleep(0)
    try:
        monkeypatch.setattr(asyncio, 'run_coroutine_threadsafe', fail_submit)
        with pytest.raises(RuntimeError, match='submission failed'):
            loop.run_coro(work)
        assert inspect.getcoroutinestate(work) == inspect.CORO_CLOSED
        assert inspect.getcoroutinestate(submitted[0]) == inspect.CORO_CLOSED
    finally:
        work.close()
        for coro in submitted:
            coro.close()
        loop.stop()


@pytest.mark.parametrize('sync', [False, True])
@pytest.mark.parametrize('asgi', ['0', '1'])
def test_native_server_lifespan_resources_work_on_both_http_versions(sync, asgi, monkeypatch):
    from blackbull import BlackBull
    from blackbull.client.http1 import HTTP1Client
    from blackbull.client.http2 import HTTP2Client
    from blackbull.env import reset_settings_cache
    from blackbull.testing import NativeTestServer

    monkeypatch.setenv('BB_FORCE_ASGI_SCOPE', asgi)
    reset_settings_cache()
    app = BlackBull()
    loops = []

    @app.on_startup
    async def startup():
        loops.append(asyncio.get_running_loop())

    @app.route(path='/')
    async def resource(conn):
        future = loops[0].create_future()
        loops[0].call_soon(future.set_result, 'ok')
        return await future

    async def requests(server):
        for client_type in (HTTP1Client, HTTP2Client):
            async with client_type('127.0.0.1', server.port) as client:
                for _ in range(2):
                    async with asyncio.timeout(3):
                        response = await client.request('GET', '/')
                    assert response.status == 200
                    assert response.body == b'ok'

    async def asynchronous():
        async with NativeTestServer(app) as server:
            await requests(server)

    try:
        if sync:
            with NativeTestServer(app) as server:
                asyncio.run(requests(server))
        else:
            asyncio.run(asynchronous())
    finally:
        reset_settings_cache()
