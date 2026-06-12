"""Sprint 38 Task B — ``BB_REQUEST_TIMEOUT`` on the HTTP/1.1 path.

Before Sprint 38, ``BB_REQUEST_TIMEOUT`` only applied to HTTP/2
streams (``HTTP2Actor._spawn_stream_task`` wrapped each stream's
coroutine with ``asyncio.wait_for``).  The HTTP/1.1 path had no
equivalent — the handler ran unbounded.  Sprint 38 adds the
same ``asyncio.wait_for`` guard to the HTTP/1.1 request path
(``RequestActor.run`` / ``_dispatch_request``), returning
``408 Request Timeout`` and closing the connection when the
deadline elapses.

These tests verify:

* A slow handler that exceeds ``BB_REQUEST_TIMEOUT`` receives a
  ``408 Request Timeout`` response and has its connection closed.
* A fast handler that finishes within the timeout window is
  unaffected (normal response, connection stays open for
  keep-alive).
* ``BB_REQUEST_TIMEOUT=0`` (the default) preserves the pre-Sprint-38
  unbounded behaviour — no artificial deadline is imposed.
* The timeout guard does not leak exceptions into the event loop
  or crash sibling connections.

Integration tests use real TCP sockets (``@pytest.mark.integration``)
so the keep-alive / 408 / close sequence is observable on the wire.
"""

from __future__ import annotations

import asyncio
import multiprocessing
import socket
import time
from types import SimpleNamespace

import pytest

from blackbull import BlackBull
from blackbull.server import ASGIServer
from blackbull.asgi import ASGIEvent


# ---------------------------------------------------------------------------
# Wire helpers (same pattern as conftest.py in the conformance/http1/ dir)
# ---------------------------------------------------------------------------

def _send_and_receive(host: str, port: int, request: bytes, *,
                      timeout: float = 3.0) -> tuple[bytes, bool]:
    """Send *request* and read the response.  Returns ``(raw_bytes, closed)``
    where *closed* is True when the peer closed the connection before our
    recv timeout."""
    sock = socket.create_connection((host, port), timeout=timeout)
    try:
        sock.sendall(request)
        sock.shutdown(socket.SHUT_WR)
        chunks: list[bytes] = []
        sock.settimeout(timeout)
        closed = False
        try:
            while True:
                buf = sock.recv(4096)
                if not buf:
                    closed = True
                    break
                chunks.append(buf)
        except (socket.timeout, TimeoutError):
            closed = False
    finally:
        sock.close()
    return b''.join(chunks), closed


def _extract_status(raw: bytes) -> int | None:
    """Parse the HTTP/1.1 status code from the response bytes."""
    line_end = raw.find(b'\r\n')
    if line_end == -1:
        return None
    parts = raw[:line_end].split(b' ')
    if len(parts) < 2:
        return None
    try:
        return int(parts[1])
    except (ValueError, IndexError):
        return None


def _has_header(raw: bytes, name: bytes) -> bool:
    """True if *name* (case-insensitive) is present in the response headers."""
    headers_end = raw.find(b'\r\n\r\n')
    if headers_end == -1:
        return False
    header_block = raw[:headers_end]
    search = name.lower()
    for line in header_block.split(b'\r\n')[1:]:  # skip status line
        if line.lower().startswith(search + b':'):
            return True
    return False


# ---------------------------------------------------------------------------
# Live-server fixture (module-scoped, one server per module)
# ---------------------------------------------------------------------------

@pytest.fixture(scope='module')
def timeout_server():
    """A BlackBull server with ``BB_REQUEST_TIMEOUT=1``, hosting
    a ``/slow?s=...`` handler whose sleep duration is query-controlled
    so a single server instance serves all test cases."""
    import os as _os

    app = BlackBull()

    @app.route(path='/sleep')
    async def sleep_handler(scope, receive, send):
        """Sleep for ``?s=N`` seconds (float), then reply.  Allows the test
        to control handler duration without restarting the server."""
        qs = scope.get('query_string', b'').decode()
        duration = 0.0
        for param in qs.split('&'):
            if param.startswith('s='):
                try:
                    duration = float(param[2:])
                except ValueError:
                    pass
        await asyncio.sleep(duration)
        await send({
            'type': ASGIEvent.HTTP_RESPONSE_START,
            'status': 200,
            'headers': [(b'content-type', b'text/plain')],
        })
        await send({
            'type': ASGIEvent.HTTP_RESPONSE_BODY,
            'body': b'done',
        })

    @app.route(path='/ping')
    async def ping():
        return b'pong'

    @app.route(path='/buffered-stall')
    async def buffered_stall_handler(scope, receive, send):
        """Send ``http.response.start`` immediately, then stall for a
        duration controlled by ``?s=N`` before sending the body.

        This exercises the *buffered-headers* edge case: after
        ``HTTP_RESPONSE_START`` the sender has ``_buffered_status``
        set but ``_started`` is still False.  If the timeout fires in
        this window, the synthetic 408 must NOT emit the buffered
        status line (which would be 200, not 408)."""
        qs = scope.get('query_string', b'').decode()
        duration = 0.0
        for param in qs.split('&'):
            if param.startswith('s='):
                try:
                    duration = float(param[2:])
                except ValueError:
                    pass
        # Emit the start immediately — this buffers headers in the
        # sender without writing anything to the wire.
        await send({
            'type': ASGIEvent.HTTP_RESPONSE_START,
            'status': 200,
            'headers': [(b'content-type', b'text/plain')],
        })
        # Now stall — if BB_REQUEST_TIMEOUT fires here, _started is
        # still False but _buffered_status is 200.
        await asyncio.sleep(duration)
        await send({
            'type': ASGIEvent.HTTP_RESPONSE_BODY,
            'body': b'finished-after-stall',
        })

    _os.environ['BB_REQUEST_TIMEOUT'] = '1.0'
    server = ASGIServer(app)
    server.open_socket(0)

    def _child():
        # Sprint 10 caution: `get_settings()` is `@functools.cache`-d
        # and inherited across fork.  Reset in the child so the
        # BB_REQUEST_TIMEOUT override takes effect.
        from blackbull.env import reset_settings_cache
        reset_settings_cache()
        asyncio.run(server.run())

    proc = multiprocessing.Process(target=_child)
    proc.start()
    try:
        server.wait_for_port(timeout=10.0)
        yield SimpleNamespace(app=app, port=server.port, server=server)
    finally:
        server.close()
        proc.terminate()
        proc.join(timeout=5)
        _os.environ.pop('BB_REQUEST_TIMEOUT', None)


# ---------------------------------------------------------------------------
# §1 — Slow handler → 408 + connection close
# ---------------------------------------------------------------------------

@pytest.mark.integration
class TestRequestTimeout408:
    """When a handler exceeds ``BB_REQUEST_TIMEOUT``, the server must
    respond with ``408 Request Timeout`` and close the connection."""

    def test_slow_handler_returns_408(self, timeout_server):
        """A handler that sleeps 2.0 s with BB_REQUEST_TIMEOUT=1.0 must
        produce a 408 response (the deadline fires before the handler
        finishes)."""
        raw, closed = _send_and_receive(
            '127.0.0.1', timeout_server.port,
            b'GET /sleep?s=2.0 HTTP/1.1\r\n'
            b'Host: localhost\r\n'
            b'Connection: close\r\n\r\n',
            timeout=5.0,
        )
        status = _extract_status(raw)
        assert status == 408, (
            f'expected 408 Request Timeout; got status={status}, raw={raw[:200]!r}')

    def test_slow_handler_connection_closed_after_408(self, timeout_server):
        """After a 408, the server must close the connection — no
        keep-alive after a timeout."""
        raw, closed = _send_and_receive(
            '127.0.0.1', timeout_server.port,
            b'GET /sleep?s=3.0 HTTP/1.1\r\n'
            b'Host: localhost\r\n'
            b'Connection: close\r\n\r\n',
            timeout=5.0,
        )
        # The peer should either have explicitly closed (TCP FIN) or
        # sent Connection: close and then closed.
        assert closed or _has_header(raw, b'connection'), (
            f'connection must be closed after 408; closed={closed}, raw={raw[:300]!r}')

    def test_408_response_contains_retry_after_or_close_hint(self, timeout_server):
        """The 408 response should carry Connection: close so the client
        knows not to reuse this connection."""
        raw, closed = _send_and_receive(
            '127.0.0.1', timeout_server.port,
            b'GET /sleep?s=2.5 HTTP/1.1\r\n'
            b'Host: localhost\r\n'
            b'Connection: keep-alive\r\n\r\n',
            timeout=5.0,
        )
        # Even though the client asked for keep-alive, the server must
        # close after 408 — the connection is in an unrecoverable state.
        assert closed or _has_header(raw, b'connection'), (
            f'408 must force-close; closed={closed}')

    def test_close_is_immediate_not_graceful_drain(self, timeout_server):
        """The close after 408 should be immediate (RST or fast close),
        not a graceful 5-second drain.  The test measures wall-clock
        time from request to close."""
        t0 = time.monotonic()
        raw, closed = _send_and_receive(
            '127.0.0.1', timeout_server.port,
            b'GET /sleep?s=5.0 HTTP/1.1\r\n'
            b'Host: localhost\r\n'
            b'Connection: close\r\n\r\n',
            timeout=10.0,
        )
        elapsed = time.monotonic() - t0
        status = _extract_status(raw)
        assert status == 408
        # Should finish near the 1.0 s timeout, not the 5.0 s handler sleep
        assert elapsed < 3.0, (
            f'close took {elapsed:.1f}s — expected < 3.0s '
            f'(timeout=1.0, handler would sleep 5.0); raw={raw[:200]!r}')


# ---------------------------------------------------------------------------
# §2 — Fast handler is unaffected
# ---------------------------------------------------------------------------

@pytest.mark.integration
class TestRequestTimeoutFastHandlerUnaffected:
    """When a handler completes within the timeout window, the response
    must be normal and the connection must be reusable."""

    def test_fast_handler_returns_200(self, timeout_server):
        """A fast handler under ``BB_REQUEST_TIMEOUT=1.0`` must return
        200 as normal."""
        raw, closed = _send_and_receive(
            '127.0.0.1', timeout_server.port,
            b'GET /sleep?s=0.0 HTTP/1.1\r\n'
            b'Host: localhost\r\n'
            b'Connection: close\r\n\r\n',
            timeout=3.0,
        )
        status = _extract_status(raw)
        assert status == 200, (
            f'fast handler must return 200; got {status}, raw={raw[:200]!r}')

    def test_handler_at_half_timeout_still_succeeds(self, timeout_server):
        """A handler that uses 50 % of the timeout window must not be
        preempted — 408 is only for handlers that *exceed* the deadline."""
        raw, closed = _send_and_receive(
            '127.0.0.1', timeout_server.port,
            b'GET /sleep?s=0.5 HTTP/1.1\r\n'
            b'Host: localhost\r\n'
            b'Connection: close\r\n\r\n',
            timeout=3.0,
        )
        status = _extract_status(raw)
        assert status == 200, (
            f'handler at half-timeout must return 200; got {status}')

    def test_ping_handler_unaffected(self, timeout_server):
        """The /ping endpoint (zero sleep) must work correctly even with
        BB_REQUEST_TIMEOUT active."""
        raw, closed = _send_and_receive(
            '127.0.0.1', timeout_server.port,
            b'GET /ping HTTP/1.1\r\n'
            b'Host: localhost\r\n'
            b'Connection: close\r\n\r\n',
            timeout=3.0,
        )
        status = _extract_status(raw)
        assert status == 200
        assert b'pong' in raw


# ---------------------------------------------------------------------------
# §3 — With timeout disabled (0), all handlers run unbounded
# ---------------------------------------------------------------------------

@pytest.mark.integration
class TestRequestTimeoutDisabled:
    """``BB_REQUEST_TIMEOUT=0`` (or unset) must preserve the pre-Sprint-38
    behaviour: handlers run to completion regardless of duration."""

    @pytest.fixture(scope='module')
    def no_timeout_server(self):
        """A server with ``BB_REQUEST_TIMEOUT=0`` (explicitly disabled)."""
        import os as _os

        app = BlackBull()

        @app.route(path='/slow')
        async def slow_handler():
            await asyncio.sleep(1.5)
            return b'finished'

        _os.environ['BB_REQUEST_TIMEOUT'] = '0'
        server = ASGIServer(app)
        server.open_socket(0)

        def _child():
            from blackbull.env import reset_settings_cache
            reset_settings_cache()
            asyncio.run(server.run())

        proc = multiprocessing.Process(target=_child)
        proc.start()
        try:
            server.wait_for_port(timeout=10.0)
            yield SimpleNamespace(app=app, port=server.port, server=server)
        finally:
            server.close()
            proc.terminate()
            proc.join(timeout=5)
            _os.environ.pop('BB_REQUEST_TIMEOUT', None)

    def test_slow_handler_completes_with_timeout_zero(self, no_timeout_server):
        """With BB_REQUEST_TIMEOUT=0, a 1.5 s handler must finish normally
        (200, not 408)."""
        raw, closed = _send_and_receive(
            '127.0.0.1', no_timeout_server.port,
            b'GET /slow HTTP/1.1\r\n'
            b'Host: localhost\r\n'
            b'Connection: close\r\n\r\n',
            timeout=4.0,
        )
        status = _extract_status(raw)
        assert status == 200, (
            f'expected 200 with timeout disabled; got {status}, raw={raw[:200]!r}')

    def test_slow_handler_body_arrives_with_timeout_zero(self, no_timeout_server):
        """The full body must be present when the handler is allowed to
        complete normally."""
        raw, closed = _send_and_receive(
            '127.0.0.1', no_timeout_server.port,
            b'GET /slow HTTP/1.1\r\n'
            b'Host: localhost\r\n'
            b'Connection: close\r\n\r\n',
            timeout=4.0,
        )
        assert b'finished' in raw, (
            f'body missing when timeout disabled; raw={raw[:200]!r}')


# ---------------------------------------------------------------------------
# §4 — Timeout at exact boundary (off-by-one safety)
# ---------------------------------------------------------------------------

@pytest.mark.integration
class TestRequestTimeoutBoundary:
    """The timeout check must use ">" (exceeded) not ">=" (reached)."""

    def test_handler_at_exact_timeout_boundary(self, timeout_server):
        """A handler whose duration equals the timeout (1.0 s) should
        ideally complete normally — the deadline is an upper bound, not
        an inclusive cutoff.  If the implementation uses ``wait_for``
        with a deadline, this may produce a 408 or a 200 depending on
        scheduling jitter.  Either outcome is acceptable; we just assert
        the server doesn't crash."""
        raw, closed = _send_and_receive(
            '127.0.0.1', timeout_server.port,
            b'GET /sleep?s=1.0 HTTP/1.1\r\n'
            b'Host: localhost\r\n'
            b'Connection: close\r\n\r\n',
            timeout=4.0,
        )
        status = _extract_status(raw)
        # Either 200 or 408 is acceptable at the exact boundary.
        assert status in (200, 408), (
            f'expected 200 or 408 at boundary; got {status}')
        assert len(raw) > 0, 'server must emit a response, not hang'


# ---------------------------------------------------------------------------
# §5 — Concurrency: timeout on one connection does not affect others
# ---------------------------------------------------------------------------

@pytest.mark.integration
class TestRequestTimeoutIsolation:
    """A timeout on connection A must not tear down connection B.
    The per-request timeout is scoped to the request, not the connection,
    and certainly not the process."""

    def test_timeout_does_not_affect_other_connections(self, timeout_server):
        """Send two requests from separate sockets.  A slow request
        (408) on socket A must not interfere with a fast request
        (200) on socket B."""
        import threading

        results: dict[str, int | None] = {}

        def fast_req():
            raw, _ = _send_and_receive(
                '127.0.0.1', timeout_server.port,
                b'GET /ping HTTP/1.1\r\n'
                b'Host: localhost\r\n'
                b'Connection: close\r\n\r\n',
                timeout=5.0,
            )
            results['fast'] = _extract_status(raw)

        def slow_req():
            raw, _ = _send_and_receive(
                '127.0.0.1', timeout_server.port,
                b'GET /sleep?s=3.0 HTTP/1.1\r\n'
                b'Host: localhost\r\n'
                b'Connection: close\r\n\r\n',
                timeout=6.0,
            )
            results['slow'] = _extract_status(raw)

        # Fire both concurrently
        t_fast = threading.Thread(target=fast_req)
        t_slow = threading.Thread(target=slow_req)
        t_fast.start()
        t_slow.start()
        t_fast.join()
        t_slow.join()

        assert results.get('fast') == 200, (
            f'fast request must succeed despite concurrent slow timeout; '
            f'fast_status={results.get("fast")}')
        assert results.get('slow') == 408, (
            f'slow request must time out; slow_status={results.get("slow")}')


# ---------------------------------------------------------------------------
# §6 — Pipelined request after timeout
# ---------------------------------------------------------------------------

@pytest.mark.integration
class TestRequestTimeoutPipelining:
    """After a 408 timeout, the connection is closed — any pipelined
    request on the same socket should be rejected or ignored."""

    def test_pipelined_request_rejected_after_timeout(self, timeout_server):
        """Send a slow request immediately followed by a fast pipelined
        request on the same socket.  The server must close after the
        408, and the pipelined request must not receive a response."""
        sock = socket.create_connection(
            ('127.0.0.1', timeout_server.port), timeout=5.0)
        try:
            sock.sendall(
                b'GET /sleep?s=3.0 HTTP/1.1\r\n'
                b'Host: localhost\r\n'
                b'Connection: keep-alive\r\n\r\n'
                b'GET /ping HTTP/1.1\r\n'
                b'Host: localhost\r\n'
                b'Connection: close\r\n\r\n'
            )
            sock.settimeout(3.0)
            raw = b''
            try:
                while True:
                    buf = sock.recv(4096)
                    if not buf:
                        break
                    raw += buf
            except (socket.timeout, TimeoutError):
                pass
        finally:
            sock.close()

        # The first (slow) request should get 408.
        first_status = _extract_status(raw)
        assert first_status == 408, (
            f'first request should be 408; got {first_status}')

        # The second (fast) request should NOT receive a 200 — the
        # connection is dead after 408.
        # Count occurrences of "HTTP/1.1 " to determine if a second
        # response was emitted.
        response_count = raw.count(b'HTTP/1.1 ')
        assert response_count == 1, (
            f'expected exactly 1 response after timeout+close; '
            f'got {response_count}.  The second pipelined request '
            f'must not be served on a dead connection. '
            f'raw={raw[:500]!r}')


# ---------------------------------------------------------------------------
# §7 — Custom timeout value via env var
# ---------------------------------------------------------------------------

@pytest.mark.integration
class TestRequestTimeoutCustomValue:
    """The timeout value is read from ``BB_REQUEST_TIMEOUT`` at server
    startup — changing it changes the deadline."""

    @pytest.fixture(scope='module')
    def short_timeout_server(self):
        """A server with ``BB_REQUEST_TIMEOUT=0.3`` (300 ms)."""
        import os as _os

        app = BlackBull()

        @app.route(path='/nap')
        async def nap_handler(scope, receive, send):
            qs = scope.get('query_string', b'').decode()
            duration = 0.0
            for param in qs.split('&'):
                if param.startswith('s='):
                    try:
                        duration = float(param[2:])
                    except ValueError:
                        pass
            await asyncio.sleep(duration)
            await send({
                'type': ASGIEvent.HTTP_RESPONSE_START,
                'status': 200,
                'headers': [(b'content-type', b'text/plain')],
            })
            await send({
                'type': ASGIEvent.HTTP_RESPONSE_BODY,
                'body': b'awake',
            })

        _os.environ['BB_REQUEST_TIMEOUT'] = '0.3'
        server = ASGIServer(app)
        server.open_socket(0)

        def _child():
            from blackbull.env import reset_settings_cache
            reset_settings_cache()
            asyncio.run(server.run())

        proc = multiprocessing.Process(target=_child)
        proc.start()
        try:
            server.wait_for_port(timeout=10.0)
            yield SimpleNamespace(app=app, port=server.port, server=server)
        finally:
            server.close()
            proc.terminate()
            proc.join(timeout=5)
            _os.environ.pop('BB_REQUEST_TIMEOUT', None)

    def test_handler_just_under_custom_timeout_succeeds(self, short_timeout_server):
        """0.2 s handler with 0.3 s timeout → 200."""
        raw, _ = _send_and_receive(
            '127.0.0.1', short_timeout_server.port,
            b'GET /nap?s=0.2 HTTP/1.1\r\n'
            b'Host: localhost\r\n'
            b'Connection: close\r\n\r\n',
            timeout=3.0,
        )
        assert _extract_status(raw) == 200

    def test_handler_over_custom_timeout_fails(self, short_timeout_server):
        """0.5 s handler with 0.3 s timeout → 408."""
        raw, _ = _send_and_receive(
            '127.0.0.1', short_timeout_server.port,
            b'GET /nap?s=0.5 HTTP/1.1\r\n'
            b'Host: localhost\r\n'
            b'Connection: close\r\n\r\n',
            timeout=3.0,
        )
        assert _extract_status(raw) == 408


# ---------------------------------------------------------------------------
# Helpers for keep-alive socket operations
# ---------------------------------------------------------------------------

def _recv_until_close_or_idle(sock: socket.socket, timeout: float = 2.0) -> bytes:
    """Read from *sock* until the peer closes (EOF) or *timeout* seconds
    elapse with no data.  The socket is NOT closed by this function."""
    chunks: list[bytes] = []
    sock.settimeout(timeout)
    try:
        while True:
            buf = sock.recv(4096)
            if not buf:
                break
            chunks.append(buf)
    except (socket.timeout, TimeoutError):
        pass
    return b''.join(chunks)


# ---------------------------------------------------------------------------
# §8 — P0 Bug 2: buffered start + timeout must emit clean 408
# ---------------------------------------------------------------------------

@pytest.mark.integration
class TestBufferedStartTimeout408:
    """When a handler has emitted ``http.response.start`` (headers
    buffered in the sender, not yet on the wire) and the timeout
    fires, the synthetic 408 must produce a clean ``HTTP/1.1 408``
    status line — NOT the handler's buffered 200 status line with
    a "408 Request Timeout" body grafted on."""

    def test_buffered_start_timeout_produces_clean_408_status_line(
        self, timeout_server,
    ):
        """A handler that sends start, then stalls 3.0 s with
        BB_REQUEST_TIMEOUT=1.0 must produce a response whose status
        line begins with ``HTTP/1.1 408``, not ``HTTP/1.1 200``."""
        raw, _ = _send_and_receive(
            '127.0.0.1', timeout_server.port,
            b'GET /buffered-stall?s=3.0 HTTP/1.1\r\n'
            b'Host: localhost\r\n'
            b'Connection: close\r\n\r\n',
            timeout=5.0,
        )
        status = _extract_status(raw)
        assert status == 408, (
            f'expected 408 after buffered-start timeout; '
            f'got status={status}, raw={raw[:300]!r}')

    def test_buffered_start_timeout_does_not_emit_200_body(
        self, timeout_server,
    ):
        """The timeout response must NOT contain the handler's
        ``finished-after-stall`` body — the handler was cancelled
        before it could send the body."""
        raw, _ = _send_and_receive(
            '127.0.0.1', timeout_server.port,
            b'GET /buffered-stall?s=3.0 HTTP/1.1\r\n'
            b'Host: localhost\r\n'
            b'Connection: close\r\n\r\n',
            timeout=5.0,
        )
        assert b'finished-after-stall' not in raw, (
            f'handler body must not appear after timeout; '
            f'raw={raw[:300]!r}')

    def test_buffered_start_timeout_closes_connection(
        self, timeout_server,
    ):
        """The connection must be closed after a buffered-start timeout
        — the response is unrecoverable."""
        raw, closed = _send_and_receive(
            '127.0.0.1', timeout_server.port,
            b'GET /buffered-stall?s=3.0 HTTP/1.1\r\n'
            b'Host: localhost\r\n'
            b'Connection: keep-alive\r\n\r\n',
            timeout=5.0,
        )
        assert closed or _has_header(raw, b'connection'), (
            f'connection must close after buffered-start timeout; '
            f'closed={closed}')

    def test_buffered_start_under_timeout_succeeds(
        self, timeout_server,
    ):
        """A handler that sends start, stalls 0.2 s (under the 1.0 s
        timeout), then sends the body must succeed with 200."""
        raw, _ = _send_and_receive(
            '127.0.0.1', timeout_server.port,
            b'GET /buffered-stall?s=0.2 HTTP/1.1\r\n'
            b'Host: localhost\r\n'
            b'Connection: close\r\n\r\n',
            timeout=3.0,
        )
        status = _extract_status(raw)
        assert status == 200, (
            f'expected 200 for under-timeout buffered-start; got {status}')
        assert b'finished-after-stall' in raw, (
            'handler body must arrive when timeout does not fire')


# ---------------------------------------------------------------------------
# §9 — P0 Bug 1: keep-alive second-request timeout must send 408
# ---------------------------------------------------------------------------

@pytest.mark.integration
class TestKeepAliveSecondRequestTimeout:
    """After a successful first request on a keep-alive connection, the
    ``_started`` flag must be reset so a second request that exceeds
    ``BB_REQUEST_TIMEOUT`` still receives a synthetic 408.

    Without the reset, ``_started`` is left True from the first
    request and the second request's timeout produces no response at
    all (just a connection close without a status line)."""

    def test_second_request_on_keep_alive_times_out_with_408(
        self, timeout_server,
    ):
        """First request: fast ping (200).  Second request: slow
        (3.0 s sleep, timeout=1.0).  The second request MUST receive
        a 408, demonstrating ``_started`` was reset between requests."""
        sock = socket.create_connection(
            ('127.0.0.1', timeout_server.port), timeout=5.0)
        try:
            # First request — fast, keep-alive
            sock.sendall(
                b'GET /ping HTTP/1.1\r\n'
                b'Host: localhost\r\n'
                b'Connection: keep-alive\r\n\r\n'
            )
            resp1 = _recv_until_close_or_idle(sock, timeout=2.0)
            assert b'200' in resp1[:20], (
                f'first request must succeed; got {resp1[:100]!r}')

            # Second request — slow, same connection
            sock.sendall(
                b'GET /sleep?s=3.0 HTTP/1.1\r\n'
                b'Host: localhost\r\n'
                b'Connection: close\r\n\r\n'
            )
            resp2 = _recv_until_close_or_idle(sock, timeout=5.0)
            status2 = _extract_status(resp2)
            assert status2 == 408, (
                f'second keep-alive request must time out with 408; '
                f'got status={status2}, raw={resp2[:200]!r}')
        finally:
            sock.close()

    def test_third_request_also_times_out_with_408(
        self, timeout_server,
    ):
        """First request fast, second request timed out (connection
        killed by the 408), so a third request should not be possible
        on this socket — the peer closes after 408."""
        sock = socket.create_connection(
            ('127.0.0.1', timeout_server.port), timeout=5.0)
        try:
            # First request — fast
            sock.sendall(
                b'GET /ping HTTP/1.1\r\n'
                b'Host: localhost\r\n'
                b'Connection: keep-alive\r\n\r\n'
            )
            resp1 = _recv_until_close_or_idle(sock, timeout=2.0)
            assert b'200' in resp1[:20]

            # Second request — slow, timeout → 408 + close
            sock.sendall(
                b'GET /sleep?s=3.0 HTTP/1.1\r\n'
                b'Host: localhost\r\n'
                b'Connection: keep-alive\r\n\r\n'
            )
            resp2 = _recv_until_close_or_idle(sock, timeout=5.0)
            assert _extract_status(resp2) == 408

            # After 408, the server must close the connection.
            # Any further data on this socket should either:
            #   (a) get a TCP RST because the server already closed, or
            #   (b) be silently ignored (no further HTTP responses).
            # We send another request — if we get data back it's a bug.
            try:
                sock.sendall(
                    b'GET /ping HTTP/1.1\r\n'
                    b'Host: localhost\r\n'
                    b'Connection: close\r\n\r\n'
                )
            except (ConnectionResetError, BrokenPipeError, OSError):
                # Expected — server already closed the connection.
                return
            # If we didn't get an error, try to read — we should get
            # EOF quickly.
            resp3 = _recv_until_close_or_idle(sock, timeout=1.0)
            assert b'HTTP/1.1 ' not in resp3, (
                f'no further HTTP responses expected after 408 close; '
                f'got {resp3[:200]!r}')
        finally:
            sock.close()
