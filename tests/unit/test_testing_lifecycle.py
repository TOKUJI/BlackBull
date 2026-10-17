import asyncio
from concurrent.futures import Future
import threading

import httpx
import pytest

from blackbull.testing import TestClient, WebSocketDisconnect, WebSocketTestSession


@pytest.fixture
def loop_resources(monkeypatch):
    threads, loops = [], []
    start, join = threading.Thread.start, threading.Thread.join
    get_loop = asyncio.Runner.get_loop

    def record_start(thread):
        threads.append(thread)
        return start(thread)

    def record_loop(runner):
        loop = get_loop(runner)
        loops.append(loop)
        return loop

    monkeypatch.setattr(threading.Thread, 'start', record_start)
    monkeypatch.setattr(asyncio.Runner, 'get_loop', record_loop)
    yield threads, loops
    for loop in loops:
        if loop.is_running():
            loop.call_soon_threadsafe(loop.stop)
    for thread in threads:
        join(thread, timeout=2)
        assert not thread.is_alive()


@pytest.mark.parametrize('error', [RuntimeError('startup'), asyncio.CancelledError(),
                                  KeyboardInterrupt(), SystemExit(), GeneratorExit(),
                                  BaseExceptionGroup('startup', [asyncio.CancelledError()]),
                                  type('StartupFailure', (BaseException,), {})()])
def test_loop_start_failure_reaches_caller_and_joins_thread(monkeypatch, error, loop_resources):

    def fail_startup(runner):
        raise error

    monkeypatch.setattr(asyncio.Runner, 'get_loop', fail_startup)
    with pytest.raises(type(error)) as caught:
        with TestClient(None):
            pytest.fail('startup should fail before entering the client')
    assert caught.value is error
    threads, _ = loop_resources
    assert len(threads) == 1
    assert not threads[0].is_alive()


@pytest.mark.parametrize('failure', ['raise', 'return', 'timeout', 'cancel', 'unexpected'])
def test_websocket_failed_entry_reclaims_app_and_loop(failure, loop_resources):
    finished = threading.Event()
    tasks = []

    async def app(conn, receive, send):
        tasks.append(asyncio.current_task())
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
    with pytest.raises(expected):
        with session:
            pytest.fail('handshake should fail')
    assert finished.is_set()
    threads, loops = loop_resources
    assert len(threads) == len(loops) == len(tasks) == 1
    assert not threads[0].is_alive()
    assert loops[0].is_closed()
    assert tasks[0].done()
    session.close()
    session.close()


def test_request_timeout_drains_cancellation_before_parent_close(monkeypatch, loop_resources):
    finished = threading.Event()
    tasks = []

    async def app(conn, receive, send):
        if conn['type'] != 'http':
            return
        if conn['path'] == '/wait':
            tasks.append(asyncio.current_task())
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(.01)
                finished.set()
        await send({'type': 'http.response.start', 'status': 200, 'headers': []})
        await send({'type': 'http.response.body', 'body': b'ok'})

    result = Future.result

    def short_timeout(future, timeout=None):
        return result(future, timeout=.05 if timeout == 60.0 else timeout)

    with TestClient(app) as client:
        with monkeypatch.context() as patch:
            patch.setattr(Future, 'result', short_timeout)
            with pytest.raises(TimeoutError):
                client.get('/wait')
        assert finished.is_set()
        assert len(tasks) == 1 and tasks[0].done()
        _, loops = loop_resources
        assert loops[0].is_running()
        assert client.get('/ok').text == 'ok'


def test_owned_loop_drains_tasks_and_async_generators(loop_resources):
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
    async def app(conn, receive, send):
        await receive()
        gen = values()
        retained.append(gen)
        assert await anext(gen) == 1
        retained.append(asyncio.create_task(pending()))
        await asyncio.sleep(0)
        await send({'type': 'websocket.accept'})
        await receive()
    with WebSocketTestSession(app, '/'):
        pass
    assert task_done.is_set()
    assert generator_done.is_set()
    threads, loops = loop_resources
    assert len(threads) == len(loops) == 1
    assert not threads[0].is_alive()
    assert loops[0].is_closed()


def test_websocket_receive_failure_reclaims_loop(loop_resources):
    tasks = []

    async def app(conn, receive, send):
        tasks.append(asyncio.current_task())
        await receive()
        await send({'type': 'websocket.accept'})
        await receive()
        raise ValueError('after accept')
    session = WebSocketTestSession(app, '/', timeout=.1)
    with pytest.raises(ValueError, match='after accept'):
        with session:
            session.send_text('fail')
            session.receive_text()
    threads, loops = loop_resources
    assert len(tasks) == len(threads) == len(loops) == 1
    assert tasks[0].done()
    assert not threads[0].is_alive()
    assert loops[0].is_closed()


def test_websocket_handshake_rejection_and_close_are_idempotent(loop_resources):
    async def app(conn, receive, send):
        await receive()
        await send({'type': 'websocket.close', 'code': 1008})
    session = WebSocketTestSession(app, '/')
    with pytest.raises(WebSocketDisconnect) as error:
        with session:
            pytest.fail('handshake should be rejected')
    assert error.value.code == 1008
    session.close()
    session.close()
    threads, loops = loop_resources
    assert len(threads) == len(loops) == 1
    assert not threads[0].is_alive()
    assert loops[0].is_closed()


@pytest.mark.parametrize('async_wrapper', [False, True])
def test_client_lifespan_http_and_websockets_share_resources(async_wrapper):
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
def test_cleanup_failure_preserves_original_exception(monkeypatch, caplog, target, phase,
                                                      loop_resources):
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

    join = threading.Thread.join
    threads, _ = loop_resources

    def failing_join(thread, timeout=None):
        join(thread, timeout)
        if thread in threads:
            raise TimeoutError('cleanup failure')

    monkeypatch.setattr(threading.Thread, 'join', failing_join)
    context = TestClient(app) if target == 'client' else WebSocketTestSession(app, '/')
    with pytest.raises((ValueError, RuntimeError), match='original failure'):
        with context:
            raise ValueError('original failure')
    assert 'cleanup failure' in caplog.text


def test_start_timeout_cannot_leave_a_late_loop_running(monkeypatch, loop_resources):
    from blackbull.testing import NativeClient

    release = threading.Event()
    get_loop = asyncio.Runner.get_loop
    join = threading.Thread.join
    wait = threading.Event.wait

    def delayed_get_loop(runner):
        assert release.wait(2)
        return get_loop(runner)

    def release_and_join(thread, timeout=None):
        release.set()
        return join(thread, timeout)

    def expire_startup(event, timeout=None):
        return False if timeout == 5.0 else wait(event, timeout)

    monkeypatch.setattr(asyncio.Runner, 'get_loop', delayed_get_loop)
    monkeypatch.setattr(threading.Event, 'wait', expire_startup)
    monkeypatch.setattr(threading.Thread, 'join', release_and_join)
    try:
        with pytest.raises(TimeoutError, match='starting'):
            with NativeClient(None):
                pytest.fail('startup should time out')
        threads, loops = loop_resources
        assert len(threads) == len(loops) == 1
        assert not threads[0].is_alive()
        assert loops[0].is_closed()
    finally:
        release.set()


@pytest.mark.parametrize('target', ['websocket', 'client'])
def test_normal_context_exit_does_not_hide_cleanup_error_in_except(monkeypatch, target,
                                                                  loop_resources):
    async def app(conn, receive, send):
        await receive()
        if target == 'client':
            await send({'type': 'lifespan.startup.complete'})
            await receive()
            await send({'type': 'lifespan.shutdown.complete'})
        else:
            await send({'type': 'websocket.accept'})
            await receive()

    join = threading.Thread.join
    threads, _ = loop_resources

    def failing_join(thread, timeout=None):
        join(thread, timeout)
        if thread in threads:
            raise RuntimeError('cleanup failure')

    monkeypatch.setattr(threading.Thread, 'join', failing_join)
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

    async def app(conn, receive, send):
        if conn['type'] != 'http':
            return
        if conn['path'] == '/fail':
            raise ValueError('app failed')
        await send({'type': 'http.response.start', 'status': 200, 'headers': []})
        await send({'type': 'http.response.body', 'body': b'ok'})

    with TestClient(app) as client:
        with monkeypatch.context() as patch:
            patch.setattr(asyncio, 'run_coroutine_threadsafe', failing_submit)
            with pytest.raises(ValueError, match='app failed'):
                client.get('/fail')
        assert client.get('/ok').text == 'ok'


def test_failed_submission_closes_both_coroutines(monkeypatch):
    import inspect

    submitted, requests = [], []
    def fail_submit(coro, loop):
        submitted.append(coro)
        raise RuntimeError('submission failed')

    async def app(conn, receive, send):
        return

    request = httpx.AsyncClient.request

    def record_request(client, *args, **kwargs):
        coro = request(client, *args, **kwargs)
        requests.append(coro)
        return coro

    with TestClient(app) as client:
        with monkeypatch.context() as patch:
            patch.setattr(httpx.AsyncClient, 'request', record_request)
            patch.setattr(asyncio, 'run_coroutine_threadsafe', fail_submit)
            try:
                with pytest.raises(RuntimeError, match='submission failed'):
                    client.get('/')
                assert len(requests) == len(submitted) == 1
                assert inspect.getcoroutinestate(requests[0]) == inspect.CORO_CLOSED
                assert inspect.getcoroutinestate(submitted[0]) == inspect.CORO_CLOSED
            finally:
                for coro in requests + submitted:
                    coro.close()


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
