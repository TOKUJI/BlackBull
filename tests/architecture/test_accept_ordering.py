"""What may reach an app before its lifespan startup has completed: nothing.

A 200 from an app whose startup hook is still parked is worse than a refused
connection — the client cannot tell it apart from a healthy answer, so it
caches and acts on state the app has not finished building.  These tests pin
the ordering from outside the server: bytes on the wire, and the app's own
handler count.  How the ordering is implemented is deliberately not visible
here, so any mechanism that keeps a client from being served early passes.

The window's other half is the burst that outnumbers the cap: what a refused
client receives, however late it reads, and what that refusal costs the
process.  Those tests are at the end of the file, under their own heading.

``tests/architecture/test_shutdown_observable.py`` is the same shape for the
other end of the lifecycle.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import time
from collections import Counter
from contextlib import asynccontextmanager
from functools import partial
from http import HTTPMethod
from pathlib import Path

import pytest

from blackbull import BlackBull
from blackbull.env import get_settings, reset_settings_cache
from blackbull.server.server import Server

REQUEST = b'GET / HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n'
BODY = b'ordered'

#: What a client may observe while startup is incomplete: the connection is
#: refused, or it is established and stays silent for the read deadline.
SILENT = ('refused', 'no-bytes')


def _app_with_startup(started: asyncio.Event, release: asyncio.Event,
                      calls: list, *, fail: bool = False):
    """A BlackBull app whose startup hook parks until *release* is set."""
    app = BlackBull()

    @app.on_startup
    async def _startup():
        started.set()
        await release.wait()
        if fail:
            raise RuntimeError('startup blew up')

    @app.route(path='/', methods=[HTTPMethod.GET])
    async def _index():
        calls.append(1)
        return BODY

    return app


def _tcp_connector(port: int):
    return partial(asyncio.open_connection, '127.0.0.1', port)


def _unix_connector(path: Path):
    return partial(asyncio.open_unix_connection, str(path))


async def _probe(connect, *, read_timeout: float = 0.5) -> tuple[str, bytes]:
    """One client's view: connect, send a GET, read inside the deadline."""
    try:
        reader, writer = await asyncio.wait_for(connect(), timeout=read_timeout)
    except asyncio.TimeoutError:
        return 'connect-timeout', b''
    except OSError as exc:
        return 'refused', f'{type(exc).__name__}: {exc}'.encode()
    try:
        writer.write(REQUEST)
        with contextlib.suppress(OSError):
            await writer.drain()
        try:
            return 'read', await asyncio.wait_for(reader.read(4096),
                                                  timeout=read_timeout)
        except asyncio.TimeoutError:
            return 'no-bytes', b''
        except OSError as exc:
            return f'read-failed({type(exc).__name__})', b''
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(writer.wait_closed(), timeout=2)


async def _await_served(connect, *, timeout: float = 5.0) -> bytes:
    """Retry until the app answers 200, rather than sleeping a fixed guess."""
    deadline = time.monotonic() + timeout
    while True:
        outcome, data = await _probe(connect, read_timeout=0.5)
        if b'HTTP/1.1 200' in data:
            return data
        if time.monotonic() >= deadline:
            raise AssertionError(
                f'no 200 within {timeout}s; last observation: {outcome}')
        await asyncio.sleep(0.05)


async def _await_path(path: Path, *, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f'{path} never appeared within {timeout}s')


@asynccontextmanager
async def _serving(server: Server):
    """``run()`` as a task, with the stop/settle/close sequence guaranteed.

    ``close_socket()`` runs only after the body has finished, so a body that
    observes the port as unreachable is evidence about what ``run()`` and
    ``stop()`` did: closing it here first would make that observation true
    however they behaved.
    """
    runner = asyncio.create_task(server.run())
    try:
        yield runner
    finally:
        await asyncio.wait_for(server.stop(drain_timeout=1.0), timeout=5)
        # Settle rather than await: a startup failure surfaces here, and the
        # test that expects it has already asserted on the exception.
        await asyncio.wait({runner}, timeout=5)
        server.close_socket()


def _listening_server(app, tmp_path: Path | None = None):
    """A bound server plus the connector that reaches it."""
    server = Server(app)
    if tmp_path is None:
        server.open_socket(0)
        return server, _tcp_connector(server.port)
    path = tmp_path / 'accept-ordering.sock'
    server.open_socket(unix_path=str(path))
    return server, _unix_connector(path)


@pytest.mark.asyncio
@pytest.mark.timeout(60)
@pytest.mark.parametrize('transport', ['tcp', 'af_unix'])
async def test_a_request_before_startup_completes_is_not_served(
        transport, tmp_path):
    """The window before ``startup`` completes answers nothing.

    Also the positive control for the other three assertions in this file:
    once the release event fires, a *fresh* connection must be answered, so
    "serve nothing, ever" cannot pass this test.
    """
    started, release, calls = asyncio.Event(), asyncio.Event(), []
    app = _app_with_startup(started, release, calls)
    server, connect = _listening_server(
        app, tmp_path if transport == 'af_unix' else None)

    async with _serving(server):
        await asyncio.wait_for(started.wait(), timeout=5)

        window = await _probe(connect, read_timeout=0.5)
        assert window[0] in SILENT, (
            f'observation during the startup window: {window!r}')
        assert b'HTTP/1.1' not in window[1], (
            f'the startup window answered: {window[1][:64]!r}')

        # Wire silence is not enough: a handler that starts work and only
        # writes later is still a request processed before startup finished.
        assert calls == [], (
            f'{len(calls)} request(s) entered the app before startup completed')

        release.set()
        body = await _await_served(connect)
        assert BODY in body, body


@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_a_lifespan_ignoring_asgi_callable_is_still_served(monkeypatch):
    """An app that never acks startup means "lifespan unsupported", not "wait".

    A raw ASGI callable returns from the lifespan scope without sending
    ``lifespan.startup.complete``; ``LifespanManager`` treats that as an app
    with no lifespan and serves it.  An implementation that waits for the ack
    itself, rather than for the manager, stops here forever.
    """
    monkeypatch.setenv('BB_FORCE_ASGI_SCOPE', '1')
    reset_settings_cache()

    async def raw_app(scope, receive, send):
        if scope['type'] != 'http':
            return
        await send({'type': 'http.response.start', 'status': 200,
                    'headers': [(b'content-length', str(len(BODY)).encode())]})
        await send({'type': 'http.response.body', 'body': BODY})

    server = Server(raw_app)
    server.open_socket(0)

    async with _serving(server):
        body = await _await_served(_tcp_connector(server.port))
        assert BODY in body, body


@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_a_failed_startup_serves_no_request():
    """Startup that raises must serve zero requests, during and after."""
    started, release, calls = asyncio.Event(), asyncio.Event(), []
    app = _app_with_startup(started, release, calls, fail=True)
    server, connect = _listening_server(app)

    async with _serving(server) as runner:
        await asyncio.wait_for(started.wait(), timeout=5)
        window = await _probe(connect, read_timeout=0.5)
        release.set()
        with pytest.raises(RuntimeError, match='startup blew up'):
            await asyncio.wait_for(runner, timeout=5)

        # ``run()`` has returned and nothing here has closed the listener, so
        # the socket being gone is what ``run()`` did on its way out.  The
        # window may be either observation; after the failure it must refuse.
        after = await _probe(connect, read_timeout=0.5)

    assert window[0] in SILENT, f'during failed startup: {window!r}'
    assert after[0] == 'refused', f'after failed startup: {after!r}'
    assert b'HTTP/1.1' not in window[1] + after[1]
    assert calls == [], f'{len(calls)} request(s) served by a failed app'


@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_stop_during_startup_ends_run_without_error():
    """``stop()`` arriving before startup completes must wind down cleanly."""
    started, release, calls = asyncio.Event(), asyncio.Event(), []
    app = _app_with_startup(started, release, calls)
    server, connect = _listening_server(app)

    async with _serving(server) as runner:
        await asyncio.wait_for(started.wait(), timeout=5)
        stopper = asyncio.create_task(server.stop(drain_timeout=1.0))
        release.set()
        await asyncio.wait_for(runner, timeout=5)
        await asyncio.wait_for(stopper, timeout=5)

        after = await _probe(connect, read_timeout=0.5)

    assert after[0] == 'refused', f'still reachable after run(): {after!r}'


@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_run_does_not_return_before_on_shutdown_completes(tmp_path):
    """The other end of the ordering: ``run()`` outlives its shutdown hook."""
    release = asyncio.Event()
    sentinel = tmp_path / 'shutdown-entered'

    app = BlackBull()

    @app.on_shutdown
    async def _stop():
        sentinel.write_text('entered')
        await release.wait()

    @app.route(path='/', methods=[HTTPMethod.GET])
    async def _index():
        return BODY

    server, connect = _listening_server(app)

    async with _serving(server) as runner:
        await _await_served(connect)
        stopper = asyncio.create_task(server.stop(drain_timeout=1.0))
        await _await_path(sentinel)
        # ``asyncio.wait``, not ``wait_for``: an expired ``wait_for`` would
        # cancel the runner whose completion is the thing being asserted.
        await asyncio.wait({runner}, timeout=0.3)
        assert not runner.done(), (
            'run() returned while on_shutdown was still parked')

        release.set()
        await asyncio.wait_for(runner, timeout=5)
        await asyncio.wait_for(stopper, timeout=5)

        after = await _probe(connect, read_timeout=0.5)

    assert sentinel.exists(), 'on_shutdown never ran'
    assert after[0] == 'refused', f'still reachable after run(): {after!r}'


# --- the cap: what a refused client gets, and what the refusal costs --------
#
# The cap is judged at accept, so a burst that queues during the startup
# window meets it all at once when serving starts.  These tests hold every
# handler slot open for the duration, which is what makes "how many were
# served" a property of the cap rather than of how fast the sweep runs: no
# slot can free up, so no later connection can be admitted.  The window's
# numeric bound (``socket_backlog``, a kernel queue) is deliberately not
# asserted here: observing it means holding the window open past the client's
# SYN retransmission budget (~2 min) so the excess gives up, which is too slow
# and too load-sensitive for this suite.

#: Unambiguously past a cap of 2, small enough to keep the suite's socket
#: churn modest.  What the cap decides is the split, not the scale.
WINDOW_BURST = 32

#: Per side (reading / streaming) of the refusal sweep in the resource test.
SWEEP_BURST = 50

#: The refusal as it must arrive: a complete, parseable HTTP/1.1 response.
REFUSAL = (b'HTTP/1.1 503 Service Unavailable\r\n'
           b'retry-after: 1\r\n'
           b'content-length: 0\r\n'
           b'connection: close\r\n'
           b'\r\n')

#: Where the resource bounds are observed.  Both are this process's, since
#: the probe and the server share one.
PROC_STATUS = Path('/proc/self/status')
PROC_FD = Path('/proc/self/fd')


def _held_handler_app(started, release, slots_filled, handlers, calls,
                      *, free_calls: int = 0):
    """Startup parks until *release*; the handler parks until *handlers*.

    Holding the handlers is what makes the served count a property of the cap
    rather than of timing: with no slot free, no later connection can be
    admitted however long the sweep takes.  ``slots_filled`` fires once two
    handlers are parked, which is that saturated state.  *free_calls* handler
    calls answer immediately instead — how a test establishes that the server
    is serving without spending a slot on the question.
    """
    app = BlackBull()
    parked = 0

    @app.on_startup
    async def _startup():
        started.set()
        await release.wait()

    @app.route(path='/', methods=[HTTPMethod.GET])
    async def _index():
        nonlocal parked
        calls.append(1)
        if len(calls) <= free_calls:
            return BODY
        parked += 1
        if parked == 2:
            slots_filled.set()
        await handlers.wait()
        parked -= 1
        return BODY

    return app


def _capped_server(app) -> Server:
    server = Server(app, max_connections=2)
    server.open_socket(0)
    return server


async def _open_client(connect):
    """One client, connected with its request already written."""
    reader, writer = await connect()
    writer.write(REQUEST)
    with contextlib.suppress(OSError):
        await writer.drain()
    return reader, writer


async def _open_burst(connect, n: int) -> list:
    return await asyncio.gather(*(_open_client(connect) for _ in range(n)))


async def _open_parked(connect, n: int):
    """*n* clients, each parked in ``read()`` as soon as it has written.

    The order a refusal is decided in is not the order the test connects in,
    so a client whose read has not started yet is a different observation
    from one already waiting — these are the waiting kind.
    """
    async def one():
        reader, writer = await _open_client(connect)
        return (reader, writer), asyncio.create_task(_read_response_head(reader))

    pairs = await asyncio.gather(*(one() for _ in range(n)))
    return [pair[0] for pair in pairs], [pair[1] for pair in pairs]


async def _close_burst(clients) -> None:
    for _reader, writer in clients:
        writer.close()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(writer.wait_closed(), timeout=2)


async def _read_response_head(reader, *, timeout: float = 15.0):
    """What one client's first read of its response returns, classified.

    The head terminator, not EOF: the question is whether the response is
    there to be read, and a read that kept going past it would report the
    reset that follows an already-delivered response as if the response had
    never arrived.  A reset, an empty EOF, a timeout or a truncated head are
    the failures, so they are what this returns as such.
    """
    try:
        head = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'),
                                      timeout=timeout)
    except ConnectionResetError:
        return 'reset', b''
    except asyncio.TimeoutError:
        return 'timeout', b''
    except (asyncio.IncompleteReadError, EOFError):
        return 'eof', b''
    except OSError as exc:
        return f'error({type(exc).__name__})', b''
    if head.startswith(b'HTTP/1.1 200 '):
        return '200', head
    if head.startswith(b'HTTP/1.1 503 '):
        return '503', head
    return 'malformed', head


async def _await_refusal(connect, *, timeout: float = 15.0) -> None:
    """Retry a fresh connection until the cap refuses one.

    A refusal proves the probe was accepted, and the accept queue is FIFO, so
    it also proves every connection that arrived earlier has been accepted and
    answered.  That is what "the sweep is over" means here — the fact a fixed
    sleep would only be guessing at.
    """
    deadline = time.monotonic() + timeout
    last = 'no attempt'
    while time.monotonic() < deadline:
        try:
            reader, writer = await connect()
        except OSError as exc:
            last = f'connect failed: {type(exc).__name__}'
            continue
        try:
            writer.write(REQUEST)
            with contextlib.suppress(OSError):
                await writer.drain()
            kind, data = await _read_response_head(reader, timeout=1.0)
            last = f'{kind}: {data[:48]!r}'
            if kind == '503':
                return
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(writer.wait_closed(), timeout=2)
    raise AssertionError(f'no refusal within {timeout}s; last observation {last}')


def _assert_cap_split(observed) -> None:
    """Exactly two served and everything else refused with a complete 503."""
    counts = Counter(kind for kind, _ in observed)
    assert counts.get('200') == 2 and counts.get('503') == len(observed) - 2, (
        f'expected 2 served and {len(observed) - 2} refused; got {dict(counts)}')
    for kind, head in observed:
        if kind == '200':
            assert head.endswith(b'\r\n\r\n'), head[:200]
        elif kind == '503':
            assert head == REFUSAL, (
                f'refusal was not the complete response: {head[:200]!r}')


@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_a_parked_burst_read_after_the_sweep_is_refused_with_a_503():
    """A refusal must survive a client that reads it only afterwards.

    The refused connection is closed before anything has read its request, so
    the request bytes are still in the kernel receive queue when the 503 goes
    out.  Closing with unread bytes queued is answered with RST, which
    discards a response the peer has not read yet — so what a client gets must
    not depend on whether it happened to be reading at the instant the sweep
    reached it.
    """
    started, release = asyncio.Event(), asyncio.Event()
    slots_filled, handlers, calls = asyncio.Event(), asyncio.Event(), []
    app = _held_handler_app(started, release, slots_filled, handlers, calls)
    server = _capped_server(app)
    connect = _tcp_connector(server.port)
    clients = []
    try:
        async with _serving(server):
            await asyncio.wait_for(started.wait(), timeout=5)

            clients = await _open_burst(connect, WINDOW_BURST)
            assert len(clients) == WINDOW_BURST
            assert calls == [], (
                f'{len(calls)} request(s) entered the app during the window')

            release.set()
            await asyncio.wait_for(slots_filled.wait(), timeout=15)
            await _await_refusal(connect)
            handlers.set()

            observed = [await _read_response_head(reader)
                        for reader, _ in clients]
    finally:
        await _close_burst(clients)

    _assert_cap_split(observed)


@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_a_parked_burst_read_during_the_sweep_is_refused_with_a_503():
    """The same burst, with every client already parked in ``read()``.

    This is the reading order the refusal always worked for.  Its job here is
    to hold that property still: a fix for the late reader must not trade one
    reading order for the other.
    """
    started, release = asyncio.Event(), asyncio.Event()
    slots_filled, handlers, calls = asyncio.Event(), asyncio.Event(), []
    app = _held_handler_app(started, release, slots_filled, handlers, calls)
    server = _capped_server(app)
    connect = _tcp_connector(server.port)
    clients = []
    try:
        async with _serving(server):
            await asyncio.wait_for(started.wait(), timeout=5)
            clients, reads = await _open_parked(connect, WINDOW_BURST)

            release.set()
            await asyncio.wait_for(slots_filled.wait(), timeout=15)
            await _await_refusal(connect)
            handlers.set()

            observed = await asyncio.gather(*reads)
    finally:
        await _close_burst(clients)

    _assert_cap_split(observed)


@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_a_burst_against_a_serving_server_is_refused_with_a_503():
    """The control: the same burst once startup is no longer in the way.

    The first handler call answers, which is how the test knows accepts have
    begun; every later one parks, so the cap saturates exactly as it does in
    the window.  A refusal decided before the request has even arrived must
    keep the meaning it always had.
    """
    started, release = asyncio.Event(), asyncio.Event()
    slots_filled, handlers, calls = asyncio.Event(), asyncio.Event(), []
    app = _held_handler_app(started, release, slots_filled, handlers, calls,
                            free_calls=1)
    server = _capped_server(app)
    connect = _tcp_connector(server.port)
    clients = []
    try:
        async with _serving(server):
            await asyncio.wait_for(started.wait(), timeout=5)
            release.set()
            await _await_served(connect)

            # One connection ahead of the burst, so the burst is refused
            # whatever the answered probe's slot does in the meantime.
            lead = await _open_client(connect)
            lead_read = asyncio.create_task(_read_response_head(lead[0]))
            burst, burst_reads = await _open_parked(connect, WINDOW_BURST)
            clients = [lead, *burst]
            reads = [lead_read, *burst_reads]

            await asyncio.wait_for(slots_filled.wait(), timeout=15)
            await _await_refusal(connect)
            handlers.set()

            observed = await asyncio.gather(*reads)
    finally:
        await _close_burst(clients)

    _assert_cap_split(observed)


@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_the_window_admits_what_the_cap_would_refuse(monkeypatch):
    """The cap does not bound the startup window; ``socket_backlog`` does.

    Pinned with a non-default backlog so the observation cannot be an
    accident of the default: the whole burst is established while the startup
    hook is parked and the cap has seen none of it, and the excess is refused
    — not lost — once accepts start.  The clients are already parked in
    ``read()`` when the window closes, so this case observes the window
    alone; whether a *late* reader also gets its refusal is the subject of
    the tests above.  The queue's own numeric bound is a kernel fact measured
    outside the process, and is recorded with the measurement rather than
    asserted here.
    """
    monkeypatch.setenv('BB_SOCKET_BACKLOG', '64')
    reset_settings_cache()

    started, release = asyncio.Event(), asyncio.Event()
    slots_filled, handlers, calls = asyncio.Event(), asyncio.Event(), []
    app = _held_handler_app(started, release, slots_filled, handlers, calls)
    clients = []
    try:
        assert get_settings().socket_backlog == 64

        server = _capped_server(app)
        connect = _tcp_connector(server.port)
        async with _serving(server):
            await asyncio.wait_for(started.wait(), timeout=5)

            clients, reads = await _open_parked(connect, WINDOW_BURST)
            assert len(clients) == WINDOW_BURST
            assert calls == [], (
                f'{len(calls)} request(s) entered the app during the window')

            release.set()
            await asyncio.wait_for(slots_filled.wait(), timeout=15)
            await _await_refusal(connect)
            handlers.set()

            observed = await asyncio.gather(*reads)
    finally:
        reset_settings_cache()
        await _close_burst(clients)

    _assert_cap_split(observed)


def _rss_kb() -> int:
    for line in PROC_STATUS.read_text().splitlines():
        if line.startswith('VmRSS:'):
            return int(line.split()[1])
    raise AssertionError(f'VmRSS is not in {PROC_STATUS}')


def _fd_count() -> int:
    return len(os.listdir(PROC_FD))


async def _sample_resources(samples: list, stop: asyncio.Event) -> None:
    """Record fd count and resident size on a fixed cadence.

    A cadence, not a timing assumption: the bounds below are about a peak
    reached somewhere inside the sweep, and no single instant can show one.
    """
    while not stop.is_set():
        samples.append((_fd_count(), _rss_kb()))
        await asyncio.sleep(0.01)


async def _stream_without_reading(writer, *, deadline: float, started_at: float):
    """Push bytes at a server that is not reading them, until it closes us.

    What the peer never reads is held by the two kernels, so the byte count
    here is a capacity floor rather than a measure of what the server kept.
    """
    written = 0
    while time.monotonic() < deadline:
        try:
            writer.write(b'x' * 4096)
            await writer.drain()
        except OSError as exc:
            return {'bytes': written, 'outcome': type(exc).__name__,
                    'elapsed': time.monotonic() - started_at}
        written += 4096
        await asyncio.sleep(0)
    return {'bytes': written, 'outcome': 'deadline',
            'elapsed': time.monotonic() - started_at}


async def _await_fd_count(target: int, *, timeout: float = 15.0) -> int:
    deadline = time.monotonic() + timeout
    count = _fd_count()
    while count != target and time.monotonic() < deadline:
        await asyncio.sleep(0.02)
        count = _fd_count()
    return count


@pytest.mark.asyncio
@pytest.mark.timeout(120)
@pytest.mark.skipif(not PROC_STATUS.is_file(),
                    reason='observes fd count and RSS through /proc')
async def test_a_refused_connection_is_not_a_resource_hold():
    """A refusal may cost a bounded linger, never a hold the peer chose.

    The interesting failures are opposite in shape and both are checked here:
    reading every unread byte before closing makes the refusal's cost the
    peer's to set, and holding the connection open to avoid the reset makes
    its duration the peer's to set.  So, for a burst that is entirely refusal
    traffic: the server closes each refused connection within a second of the
    sweep reaching it, reads at most 8 MiB from any one of them, returns to
    its idle descriptor count, and grows resident memory by at most 32 MiB.
    """
    started, release = asyncio.Event(), asyncio.Event()
    slots_filled, handlers, calls = asyncio.Event(), asyncio.Event(), []
    app = _held_handler_app(started, release, slots_filled, handlers, calls)
    server = _capped_server(app)
    connect = _tcp_connector(server.port)

    clients: list = []
    samples: list = []
    stop = asyncio.Event()
    sampler = None
    observed, streamed = [], []
    try:
        async with _serving(server):
            await asyncio.wait_for(started.wait(), timeout=5)
            idle_fds, idle_rss = _fd_count(), _rss_kb()

            # Holders first: accepts are FIFO, so the two connections the cap
            # admits are holders and every streamer meets the refusal.
            holders = await _open_burst(connect, SWEEP_BURST)
            streamers = await _open_burst(connect, SWEEP_BURST)
            clients = [*holders, *streamers]

            sampler = asyncio.create_task(_sample_resources(samples, stop))
            release.set()
            started_at = time.monotonic()
            deadline = started_at + 2.0
            reads = [asyncio.create_task(_read_response_head(reader))
                     for reader, _ in holders]
            writes = [asyncio.create_task(
                _stream_without_reading(writer, deadline=deadline,
                                        started_at=started_at))
                for _, writer in streamers]

            streamed = await asyncio.gather(*writes)
            handlers.set()
            observed = await asyncio.gather(*reads)

            stop.set()
            await sampler
            samples.append((_fd_count(), _rss_kb()))

            # The test's own ends, closed before the count is read: a client
            # still holding its socket is this process holding a descriptor,
            # and the question is whether the *server* gave its half back.
            await _close_burst(clients)
            settled_fds = await _await_fd_count(idle_fds)
    finally:
        stop.set()
        if sampler is not None and not sampler.done():
            sampler.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await sampler
        await _close_burst(clients)

    offered = sum(s['bytes'] for s in streamed)
    # The load has to have been real for the bounds to mean anything: every
    # streamer must have been writing when the server closed it, not timing
    # out on its own deadline.
    assert all(s['outcome'] != 'deadline' for s in streamed), (
        f'the server never closed these streamers: {streamed}')
    assert min(s['bytes'] for s in streamed) > 0 and offered >= 1024 * 1024, (
        f'the sweep offered {offered} bytes in total')

    # a. The server closes a refused streamer within a second of the sweep.
    assert max(s['elapsed'] for s in streamed) <= 1.0, (
        f"refused streamer held for {max(s['elapsed'] for s in streamed):.3f}s")
    # b. ...having read at most 8 MiB from any one of them.
    assert max(s['bytes'] for s in streamed) <= 8 * 1024 * 1024, (
        f"server read {max(s['bytes'] for s in streamed)} bytes from one client")
    # c. Descriptors return to idle: every in-process client owns two of them
    #    (its own end and the accepted end), so the peak is bounded by 2 x the
    #    burst plus the listener and probe descriptors.
    peak_fds = max(count for count, _ in samples)
    assert peak_fds <= idle_fds + 2 * len(clients) + 8, (
        f'fd peak {peak_fds} against idle {idle_fds}')
    assert settled_fds == idle_fds, (
        f'fds settled at {settled_fds}, idle was {idle_fds}')
    # d. And the process grew by at most 32 MiB while the burst offered its
    #    bytes — a coarse bound, since the probe's own buffers are inside it.
    peak_rss = max(rss for _, rss in samples)
    assert peak_rss - idle_rss <= 32 * 1024, (
        f'RSS grew by {peak_rss - idle_rss} KB (idle {idle_rss} KB)')

    # The two admitted connections are the first two holders: the refusal
    # sweep did not spill into the served ones.
    holders_seen = Counter(kind for kind, _ in observed)
    assert holders_seen == {'200': 2, '503': SWEEP_BURST - 2}, (
        f'holder observations: {holders_seen}')
