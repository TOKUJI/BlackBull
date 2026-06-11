"""In-memory test client for ASGI 3.0 applications.

Lets you exercise a BlackBull (or any ASGI 3.0) app from a synchronous
pytest test without binding a TCP socket.  Built on
``httpx.ASGITransport`` for HTTP; uses a background-thread event loop
to bridge sync calls to the (async-only) ASGI transport, to drive the
``lifespan`` protocol, and to host WebSocket sessions.

Typical usage::

    from blackbull import BlackBull
    from blackbull.testing import TestClient

    app = BlackBull()

    @app.route('/')
    async def hello():
        return "hi"

    def test_hello():
        with TestClient(app) as client:
            response = client.get('/')
            assert response.status_code == 200
            assert response.text == "hi"

The client is a context manager so that ASGI ``lifespan.startup`` runs
before any request and ``lifespan.shutdown`` runs on exit.  Apps that
don't implement the lifespan protocol are tolerated silently.
"""

from __future__ import annotations

import asyncio
import queue
import threading
from typing import Any

import httpx


class WebSocketDisconnect(Exception):
    """Raised when the server side closes (or rejects) the WebSocket."""

    def __init__(self, code: int = 1000, reason: str = ''):
        self.code = code
        self.reason = reason
        super().__init__(f'WebSocket disconnected: code={code} reason={reason!r}')


class _LoopThread:
    """A dedicated background thread running an asyncio event loop.

    ``run_coro`` schedules a coroutine on the loop and blocks the caller
    until it returns; ``stop`` joins the thread cleanly.  ``server_to_client``
    is a threading queue carrying events the ASGI app emits via ``send``;
    ``client_to_server`` is an asyncio queue carrying events the test
    writes that the app reads via ``receive``.  The queues are only used
    for lifespan / WebSocket sessions; HTTP requests go through httpx.
    """

    def __init__(self) -> None:
        self.loop: asyncio.AbstractEventLoop | None = None
        self.client_to_server: asyncio.Queue | None = None
        self.server_to_client: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True, name='blackbull-testclient-loop')
        self._thread.start()
        self._ready.wait()

    def stop(self, timeout: float = 5.0) -> None:
        if self.loop is not None and self.loop.is_running():
            self.loop.call_soon_threadsafe(self.loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def run_coro(self, coro, timeout: float | None = 60.0):
        """Schedule ``coro`` on the loop thread and block until it returns."""
        assert self.loop is not None
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout=timeout)

    def _run(self) -> None:
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.client_to_server = asyncio.Queue()
        self._ready.set()
        try:
            self.loop.run_forever()
        finally:
            self.loop.close()


class _LifespanManager:
    """Drives the ASGI ``lifespan`` protocol for the duration of a TestClient session.

    Runs on the TestClient's shared loop thread so the lifespan task and
    the HTTP request tasks see the same event loop (apps may store
    asyncio primitives on ``scope['state']`` during startup that get
    used in request handlers).
    """

    def __init__(self, app: Any, loop_thread: _LoopThread,
                 startup_timeout: float = 10.0, shutdown_timeout: float = 10.0):
        self.app = app
        self.loop_thread = loop_thread
        self.startup_timeout = startup_timeout
        self.shutdown_timeout = shutdown_timeout
        self._app_task: asyncio.Task | None = None
        self._unsupported = False

    def startup(self) -> None:
        async def _spawn():
            scope = {
                'type': 'lifespan',
                'asgi': {'version': '3.0', 'spec_version': '2.0'},
            }

            async def receive():
                return await self.loop_thread.client_to_server.get()

            async def send(event):
                self.loop_thread.server_to_client.put(event)

            self._app_task = asyncio.create_task(self.app(scope, receive, send))

        self.loop_thread.run_coro(_spawn(), timeout=self.startup_timeout)
        self.loop_thread.run_coro(
            self.loop_thread.client_to_server.put({'type': 'lifespan.startup'}),
            timeout=self.startup_timeout,
        )

        event = self._wait_for_lifespan_event(self.startup_timeout)
        if event is None:
            # The app task ended before acking startup — treat as
            # "lifespan unsupported" and continue without it.  This is
            # how ASGI-2.0-style apps signal they don't implement the
            # lifespan protocol.
            self._unsupported = True
            return
        if event['type'] == 'lifespan.startup.failed':
            raise RuntimeError(
                f"Lifespan startup failed: {event.get('message', '')}",
            )

    def shutdown(self) -> None:
        if self._unsupported:
            return
        try:
            self.loop_thread.run_coro(
                self.loop_thread.client_to_server.put({'type': 'lifespan.shutdown'}),
                timeout=self.shutdown_timeout,
            )
            self._wait_for_lifespan_event(self.shutdown_timeout)
        finally:
            if self._app_task is not None:
                try:
                    self.loop_thread.run_coro(self._await_task(), timeout=self.shutdown_timeout)
                except Exception:
                    pass

    async def _await_task(self):
        assert self._app_task is not None
        try:
            await self._app_task
        except Exception:
            pass

    def _wait_for_lifespan_event(self, timeout: float):
        """Return the next lifespan event, or None if the app finished without one.

        If the app task ended with an exception, surface it as a
        RuntimeError so test authors see the underlying cause rather
        than a silent "lifespan unsupported" outcome.
        """
        deadline = timeout
        while True:
            try:
                return self.loop_thread.server_to_client.get(timeout=0.05)
            except queue.Empty:
                deadline -= 0.05
                if self._app_task is not None and self._app_task.done():
                    exc = self._app_task.exception()
                    if exc is not None:
                        raise RuntimeError(
                            f"Lifespan startup failed: {exc!r}"
                        ) from exc
                    return None
                if deadline <= 0:
                    raise TimeoutError('Timed out waiting for lifespan event')


class WebSocketTestSession:
    """Synchronous WebSocket session against an ASGI application.

    Open via :meth:`TestClient.websocket_connect` as a context manager::

        with client.websocket_connect('/ws') as ws:
            ws.send_text('ping')
            assert ws.receive_text() == 'pong'

    Raises :class:`WebSocketDisconnect` when the server closes (or
    rejects) the connection.
    """

    def __init__(
        self,
        app: Any,
        path: str,
        subprotocols: list[str] | None = None,
        headers: list[tuple[str, str]] | None = None,
        cookies: dict[str, str] | None = None,
        timeout: float = 5.0,
    ):
        self.app = app
        self.timeout = timeout
        if '?' in path:
            raw_path, query = path.split('?', 1)
        else:
            raw_path, query = path, ''
        encoded_headers: list[tuple[bytes, bytes]] = []
        for k, v in (headers or []):
            encoded_headers.append((k.lower().encode('latin-1'), v.encode('latin-1')))
        if cookies:
            cookie_str = '; '.join(f'{k}={v}' for k, v in cookies.items())
            encoded_headers.append((b'cookie', cookie_str.encode('latin-1')))
        self.scope = {
            'type': 'websocket',
            'asgi': {'version': '3.0', 'spec_version': '2.4'},
            'http_version': '1.1',
            'scheme': 'ws',
            'path': raw_path,
            'raw_path': raw_path.encode('latin-1'),
            'query_string': query.encode('latin-1'),
            'root_path': '',
            'headers': encoded_headers,
            'client': ('testclient', 50000),
            'server': ('testserver', 80),
            'subprotocols': list(subprotocols or []),
            'state': {},
        }
        self._loop_thread = _LoopThread()
        self._app_task: asyncio.Task | None = None
        self._accepted = False

    def __enter__(self) -> 'WebSocketTestSession':
        self._loop_thread.start()

        async def _spawn():
            async def receive():
                return await self._loop_thread.client_to_server.get()

            async def send(event):
                self._loop_thread.server_to_client.put(event)

            self._app_task = asyncio.create_task(self.app(self.scope, receive, send))

        self._loop_thread.run_coro(_spawn(), timeout=self.timeout)
        self._loop_thread.run_coro(
            self._loop_thread.client_to_server.put({'type': 'websocket.connect'}),
            timeout=self.timeout,
        )
        event = self._recv_event()
        if event['type'] == 'websocket.close':
            self._teardown()
            raise WebSocketDisconnect(code=event.get('code', 1000), reason=event.get('reason', ''))
        if event['type'] != 'websocket.accept':
            self._teardown()
            raise RuntimeError(f"Expected websocket.accept, got {event['type']!r}")
        self._accepted = True
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def send_text(self, text: str) -> None:
        self._send_client_event({'type': 'websocket.receive', 'text': text})

    def send_bytes(self, data: bytes) -> None:
        self._send_client_event({'type': 'websocket.receive', 'bytes': data})

    def send_json(self, data: Any) -> None:
        import json
        self.send_text(json.dumps(data))

    def receive_text(self) -> str:
        event = self._recv_event()
        if event['type'] == 'websocket.close':
            raise WebSocketDisconnect(code=event.get('code', 1000), reason=event.get('reason', ''))
        if event['type'] != 'websocket.send':
            raise RuntimeError(f"Expected websocket.send, got {event['type']!r}")
        if event.get('text') is None:
            raise RuntimeError('Expected text frame; got binary')
        return event['text']

    def receive_bytes(self) -> bytes:
        event = self._recv_event()
        if event['type'] == 'websocket.close':
            raise WebSocketDisconnect(code=event.get('code', 1000), reason=event.get('reason', ''))
        if event['type'] != 'websocket.send':
            raise RuntimeError(f"Expected websocket.send, got {event['type']!r}")
        if event.get('bytes') is None:
            raise RuntimeError('Expected binary frame; got text')
        return event['bytes']

    def receive_json(self) -> Any:
        import json
        return json.loads(self.receive_text())

    def iter_text(self):
        """Yield successive text messages from the server until the WebSocket closes.

        Stops cleanly when the server emits a ``websocket.close`` — the
        :class:`WebSocketDisconnect` raised by the underlying receive
        is caught and converted into normal iterator termination, so
        the test can write::

            with client.websocket_connect('/stream') as ws:
                for msg in ws.iter_text():
                    ...

        without an explicit try/except around the loop.
        """
        try:
            while True:
                yield self.receive_text()
        except WebSocketDisconnect:
            return

    def iter_bytes(self):
        """Yield successive binary messages from the server until the WebSocket closes.

        Mirror of :meth:`iter_text` for binary frames.
        """
        try:
            while True:
                yield self.receive_bytes()
        except WebSocketDisconnect:
            return

    def close(self, code: int = 1000) -> None:
        if self._loop_thread.loop is None:
            return
        if self._accepted:
            try:
                self._send_client_event({'type': 'websocket.disconnect', 'code': code})
            except Exception:
                pass
        self._teardown()

    def _send_client_event(self, event: dict) -> None:
        self._loop_thread.run_coro(
            self._loop_thread.client_to_server.put(event),
            timeout=self.timeout,
        )

    def _recv_event(self) -> dict:
        deadline = self.timeout
        while True:
            try:
                return self._loop_thread.server_to_client.get(timeout=0.05)
            except queue.Empty:
                deadline -= 0.05
                if self._app_task is not None and self._app_task.done():
                    exc = self._app_task.exception()
                    if exc is not None:
                        raise exc
                    raise WebSocketDisconnect(code=1006, reason='application ended without close frame')
                if deadline <= 0:
                    raise TimeoutError('Timed out waiting for WebSocket event from app')

    def _teardown(self) -> None:
        try:
            if self._app_task is not None:
                async def _await():
                    try:
                        await asyncio.wait_for(self._app_task, timeout=self.timeout)
                    except (asyncio.TimeoutError, Exception):
                        pass
                try:
                    self._loop_thread.run_coro(_await(), timeout=self.timeout + 1.0)
                except Exception:
                    pass
        finally:
            self._loop_thread.stop()


class TestClient:
    """In-memory HTTP+WebSocket test client for ASGI 3.0 applications.

    Provides a synchronous façade over ``httpx.AsyncClient`` +
    ``httpx.ASGITransport`` by hosting an event loop in a background
    thread.  HTTP request methods (``get``, ``post``, ``put``, …)
    forward to the underlying ``httpx.AsyncClient``; WebSocket sessions
    use a dedicated bridge to the ASGI receive/send channels.

    Use as a context manager so that the ASGI ``lifespan`` protocol
    runs around the test::

        with TestClient(app) as client:
            ...
    """

    # Tell pytest not to collect this class as a test container — the
    # ``Test*`` prefix triggers pytest's default collection heuristic
    # but this is a fixture, not a test class.
    __test__ = False

    def __init__(
        self,
        app: Any,
        base_url: str = 'http://testserver',
        raise_app_exceptions: bool = True,
        root_path: str = '',
        cookies: Any | None = None,
        headers: Any | None = None,
        follow_redirects: bool = False,
    ) -> None:
        self.app = app
        self.base_url = base_url
        self._raise_app_exceptions = raise_app_exceptions
        self._root_path = root_path
        self._cookies = cookies
        self._headers = headers
        self._follow_redirects = follow_redirects
        self._loop_thread = _LoopThread()
        self._lifespan: _LifespanManager | None = None
        self._async_client: httpx.AsyncClient | None = None

    def __enter__(self) -> 'TestClient':
        self._loop_thread.start()
        self._async_client = httpx.AsyncClient(
            transport=httpx.ASGITransport(
                app=self.app,
                raise_app_exceptions=self._raise_app_exceptions,
                root_path=self._root_path,
            ),
            base_url=self.base_url,
            cookies=self._cookies,
            headers=self._headers,
            follow_redirects=self._follow_redirects,
        )
        self._lifespan = _LifespanManager(self.app, self._loop_thread)
        try:
            self._lifespan.startup()
        except Exception:
            self._loop_thread.stop()
            raise
        return self

    def __exit__(self, *exc_info) -> None:
        try:
            if self._lifespan is not None:
                self._lifespan.shutdown()
        finally:
            try:
                if self._async_client is not None:
                    self._loop_thread.run_coro(self._async_client.aclose())
            finally:
                self._loop_thread.stop()

    # ----- HTTP methods -----------------------------------------------------

    def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        if self._async_client is None:
            raise RuntimeError(
                'TestClient must be used as a context manager: '
                '`with TestClient(app) as client: ...`',
            )
        return self._loop_thread.run_coro(
            self._async_client.request(method, url, **kwargs),
        )

    @property
    def cookies(self):
        """Persistent cookie jar, forwarded from the underlying ``httpx.AsyncClient``.

        Same semantics as ``httpx.Client.cookies``: cookies set by
        responses persist across requests on the same client.
        """
        if self._async_client is None:
            raise RuntimeError(
                'TestClient must be used as a context manager before '
                'accessing cookies.',
            )
        return self._async_client.cookies

    @property
    def headers(self):
        """Default headers applied to every request, forwarded from the
        underlying ``httpx.AsyncClient``."""
        if self._async_client is None:
            raise RuntimeError(
                'TestClient must be used as a context manager before '
                'accessing headers.',
            )
        return self._async_client.headers

    def get(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request('GET', url, **kwargs)

    def head(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request('HEAD', url, **kwargs)

    def options(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request('OPTIONS', url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request('POST', url, **kwargs)

    def put(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request('PUT', url, **kwargs)

    def patch(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request('PATCH', url, **kwargs)

    def delete(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request('DELETE', url, **kwargs)

    # ----- WebSocket --------------------------------------------------------

    def websocket_connect(
        self,
        url: str,
        subprotocols: list[str] | None = None,
        headers: list[tuple[str, str]] | None = None,
        cookies: dict[str, str] | None = None,
        timeout: float = 5.0,
    ) -> WebSocketTestSession:
        """Open a WebSocket session against the application.

        ``url`` is a path (relative to the app), e.g. ``/ws`` or
        ``/ws?token=abc``.  Returns a :class:`WebSocketTestSession`
        that should itself be used as a context manager.
        """
        return WebSocketTestSession(
            app=self.app,
            path=url,
            subprotocols=subprotocols,
            headers=headers,
            cookies=cookies,
            timeout=timeout,
        )


__all__ = ['TestClient', 'WebSocketTestSession', 'WebSocketDisconnect']
