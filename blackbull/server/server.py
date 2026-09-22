"""The listening server: binds sockets, accepts connections, drives lifespan.

[`Server`][blackbull.server.server.Server] — ``ASGIServer`` is an alias — is
the object under ``BlackBull.run()``.  It resolves its
[`Listener`][blackbull.server.listener.Listener] set into bound sockets, groups
them by TLS context so a listener terminates the certificate it names, and
serves each group through
[`SocketManager`][blackbull.server.server.SocketManager].  Every accepted
connection becomes a buffered protocol and, from there, one
[`ConnectionActor`][blackbull.server.connection_actor.ConnectionActor].
[`LifespanManager`][blackbull.server.server.LifespanManager] drives the ASGI
lifespan handshake around all of it.

``run()`` blocks until ``stop()``.  ``stop()`` closes the listeners first, then
lets the requests already in flight finish inside a drain budget instead of
cancelling them, because a cancelled handler leaves a client holding a
half-written response.  ``open_socket()`` binds without serving — what the
multi-worker master, and a test that needs a port before it forks, both use.
"""
import asyncio
from http import HTTPStatus
import logging
import ssl
import sys
from collections import defaultdict, deque
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import replace
from pathlib import Path
import time

from .._cleanup import combine_cleanup_errors
from ..protocol.rsock import (
    create_configured_sockets, create_unix_socket,
    adopt_inherited_sockets, adopt_listening_fd, close_sockets,
)
from .listener import HTTP, InheritedFd, Listener, Tcp, Unix
from .sender import AbstractWriter
from .recipient import (AbstractReader,
                        _HTTP2_STREAM_QUEUE_DEPTH, _WS_READ_INLINE)
from .cap_log import log_cap_hit
from ..asgi import ASGIEvent
logger = logging.getLogger(__name__)


def _listener_from_args(port, unix_path, inherited_fd, tls=None) -> Listener:
    """The old way of saying it — one port, one path, or one fd — as a listener."""
    if inherited_fd is not None:
        return Listener(InheritedFd(inherited_fd), tls=tls)
    if unix_path is not None:
        return Listener(Unix(unix_path), tls=tls)
    return Listener(Tcp(port), tls=tls)


def _address_of(sock) -> Tcp | Unix:
    sockname = sock.getsockname()
    if isinstance(sockname, str):
        return Unix(sockname)
    return Tcp(sockname[1])

# ``eager_start`` landed in 3.12; the supported floor is 3.11.
_EAGER_TASKS = sys.version_info >= (3, 12)
_CLEANUP_TIMEOUT = 8.0


class _AsyncCleanupBudget:
    """One lazy absolute deadline shared by every async cleanup owner."""

    def __init__(self, timeout: float = _CLEANUP_TIMEOUT):
        self._timeout = timeout
        self._deadline: float | None = None

    def start(self, timeout: float | None = None) -> None:
        if self._deadline is None:
            self._deadline = (asyncio.get_running_loop().time()
                              + (self._timeout if timeout is None else timeout))

    def remaining(self) -> float:
        self.start()
        return max(0.0, self._deadline - asyncio.get_running_loop().time())


async def _cancel_tasks(tasks, budget: _AsyncCleanupBudget) -> Exception | None:
    tasks = [task for task in tasks if task is not None]
    pending = [task for task in tasks if not task.done()]
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.sleep(0)
        pending = [task for task in pending if not task.done()]
    if pending and budget.remaining() > 0:
        _done, pending = await asyncio.wait(pending, timeout=budget.remaining())
    errors = []
    for task in tasks:
        if not task.done() or task.cancelled():
            continue
        try:
            task_error = task.exception()
        except asyncio.CancelledError:
            continue
        except Exception as exc:
            task_error = exc
        if isinstance(task_error, Exception):
            errors.append(task_error)
    if pending:
        errors.append(TimeoutError(
            'async cleanup did not finish before the deadline'))
    return combine_cleanup_errors(*errors)


async def _wait_for_event_ignoring_cancellation(event: asyncio.Event) -> None:
    """Wait for a cleanup boundary even when the caller is cancelled again."""
    waiter = asyncio.create_task(event.wait())
    while not waiter.done():
        try:
            await asyncio.shield(waiter)
        except asyncio.CancelledError:
            logger.info('Server task cancelled during shutdown cleanup.')
    waiter.result()


def _close_servers(servers) -> Exception | None:
    errors = []
    for server in servers:
        try:
            server.close()
        except Exception as exc:
            errors.append(exc)
            logger.exception('Failed to close asyncio server')
    return combine_cleanup_errors(*errors)


async def _wait_async_servers_closed(
        servers, budget: _AsyncCleanupBudget) -> Exception | None:
    errors = []
    waiters = []
    for server in servers:
        wait_closed = getattr(server, 'wait_closed', None)
        if wait_closed is not None:
            waiters.append(asyncio.create_task(wait_closed()))
    if waiters:
        done, pending = await asyncio.wait(waiters, timeout=budget.remaining())
        for task in done:
            if task.cancelled():
                continue
            exc = task.exception()
            errors.append(exc)
        errors.append(await _cancel_tasks(pending, budget))
    return combine_cleanup_errors(*errors)


async def _close_async_servers(servers, budget: _AsyncCleanupBudget) -> Exception | None:
    return combine_cleanup_errors(
        _close_servers(servers),
        await _wait_async_servers_closed(servers, budget))


def _validate_unique_socket_fds(bound_listeners) -> None:
    """Reject aliases before ownership can move to multiple asyncio servers."""
    seen = set()
    for _listener, sockets in bound_listeners:
        for sock in sockets:
            fd = sock.fileno()
            if isinstance(fd, int) and fd >= 0:
                if fd in seen:
                    raise RuntimeError(
                        f'Duplicate listening fd {fd} cannot have multiple owners')
                seen.add(fd)


class LifespanManager:
    """Async context manager that drives the ASGI lifespan protocol.

    On enter: launches the app's lifespan task and delivers 'lifespan.startup'.
    Raises RuntimeError if the app responds with 'lifespan.startup.failed'.
    On exit: delivers 'lifespan.shutdown'; 'lifespan.shutdown.failed' raises
    RuntimeError with the app's message.


    Implemented as a class (not asynccontextmanager) so that __aenter__ and
    __aexit__ can be called independently — e.g. startup() / shutdown() — without
    leaving a zombie async-generator that asyncio tries to finalize on loop close.
    """

    def __init__(self, app, cleanup_budget=None, *, cleanup_timeout=_CLEANUP_TIMEOUT):
        self._app = app
        self._receive_q: asyncio.Queue = asyncio.Queue()
        self._send_q:    asyncio.Queue = asyncio.Queue()
        self._task = None
        self._cleanup_budget = (cleanup_budget
                                or _AsyncCleanupBudget(cleanup_timeout))

    async def __aenter__(self):
        scope = {'type': 'lifespan', 'asgi': {'version': '3.0'}}
        try:
            self._task = asyncio.create_task(
                self._app(scope, self._receive_q.get, self._send_q.put))
            await self._receive_q.put({'type': ASGIEvent.LIFESPAN_STARTUP})
            getter = asyncio.ensure_future(self._send_q.get())
            try:
                await asyncio.wait(
                    {getter, self._task}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                if not getter.done():
                    getter.cancel()
                    await asyncio.gather(getter, return_exceptions=True)
            if getter.done() and not getter.cancelled():
                event = getter.result()
                if event.get('type') == ASGIEvent.LIFESPAN_STARTUP_FAILED:
                    raise RuntimeError(
                        event.get('message', 'Lifespan startup failed'))
                return self
            if self._task.done():
                exc = self._task.exception()
                if exc is not None:
                    raise RuntimeError(
                        f'Lifespan startup failed: {exc!r}') from exc
                return self
            raise RuntimeError('Lifespan startup did not complete')
        except BaseException:
            cleanup_error = await _cancel_tasks(
                [self._task], self._cleanup_budget)
            if cleanup_error is not None:
                logger.error('Lifespan startup rollback failed: %s', cleanup_error)
            raise

    @staticmethod
    def _shutdown_failure(event) -> Exception | None:
        if event.get('type') == ASGIEvent.LIFESPAN_SHUTDOWN_FAILED:
            message = event.get('message') or 'no message'
            # Logged too: raised, it reaches the operator only as an exit status.
            logger.error('Lifespan shutdown failed: %s', message)
            return RuntimeError(f'Lifespan shutdown failed: {message}')
        return None

    async def __aexit__(self, exc_type, exc, traceback):
        task = self._task
        if task is None:
            return False
        # A lifespan task cancelled out from under us — asyncio.run()'s
        # _cancel_all_tasks cancels every outstanding task at once — never
        # emits lifespan.shutdown.complete, so an unconditional wait on the
        # send queue would wedge teardown.  Handshake only while it is alive,
        # and race the ack against the task itself.
        errors = []
        if not task.done():
            await self._receive_q.put({'type': ASGIEvent.LIFESPAN_SHUTDOWN})
            getter = asyncio.ensure_future(self._send_q.get())
            done, pending = await asyncio.wait(
                {getter, task}, timeout=self._cleanup_budget.remaining(),
                return_when=asyncio.FIRST_COMPLETED)
            if not done:
                errors.append(TimeoutError(
                    'lifespan shutdown did not finish before the deadline'))
            elif getter in done and not getter.cancelled():
                errors.append(self._shutdown_failure(getter.result()))
            errors.append(await _cancel_tasks([getter], self._cleanup_budget))
        errors.append(await _cancel_tasks([task], self._cleanup_budget))
        cleanup_error = combine_cleanup_errors(*errors)
        if cleanup_error is not None:
            if exc_type is None:
                raise cleanup_error
            logger.error('Lifespan cleanup failed: %s', cleanup_error)
        return False


@asynccontextmanager
async def SocketManager(socket_cb_pairs, ssl_context, cleanup_budget=None):
    """Async context manager that creates asyncio servers from already-bound sockets.

    *socket_cb_pairs* is an iterable of ``(sock, protocol_factory)`` — each
    socket is served by its own factory.  The shared HTTP listener and each
    port-bound non-ASGI protocol both come from
    [`Server.connection_protocol_factory`][Server.connection_protocol_factory], differing only in whether a
    binding is pre-committed.

    On enter: wraps each socket in ``loop.create_server`` (TCP) or
    ``loop.create_unix_server`` (AF_UNIX) and yields the list.  Accepting does
    not start here — the caller calls ``start_serving()`` on the servers.  Not
    ``start_server``: that pairs a StreamReader/StreamWriter over asyncio's
    own buffering with every connection, and the whole point of the buffered
    protocol is that the connection owns exactly one buffer.
    On exit: closes all asyncio servers.

    Dispatches by ``sock.family``: AF_INET / AF_INET6 take the TCP
    server, AF_UNIX takes the unix server.  Both honour the configured
    ``socket_backlog`` (asyncio's default of 100 is silently re-applied
    via ``sock.listen(backlog)`` otherwise, which produces wrk c=1024
    connect errors).
    """
    import socket as _socket  # noqa: PLC0415
    from ..env import get_settings as _get_settings  # noqa: PLC0415
    _backlog = _get_settings().socket_backlog
    # Some Windows builds do not define AF_UNIX at all.
    _af_unix = getattr(_socket, 'AF_UNIX', None)
    loop = asyncio.get_running_loop()
    budget = cleanup_budget or _AsyncCleanupBudget()
    servers = []
    primary = None
    try:
        for sock, factory in socket_cb_pairs:
            kwargs = {'sock': sock, 'ssl': ssl_context, 'backlog': _backlog,
                      'start_serving': False}
            if ssl_context is not None:
                kwargs['ssl_handshake_timeout'] = 60.0
            if _af_unix is not None and sock.family == _af_unix:
                srv = await loop.create_unix_server(factory, **kwargs)
            else:
                srv = await loop.create_server(factory, **kwargs)
            servers.append(srv)
        yield servers
    except BaseException as exc:
        primary = exc
        raise
    finally:
        cleanup_error = await _close_async_servers(servers, budget)
        if cleanup_error is not None:
            if primary is None:
                raise cleanup_error
            logger.error('Asyncio server cleanup failed: %s', cleanup_error)


def _max_connections_report(resolved: int) -> tuple[str, str]:
    """Describe the connection cap in force, and where it came from.

    ``BB_MAX_CONNECTIONS`` resolves to a plain integer long before it reaches
    the server, so the origin is re-read from the environment here: the number
    alone cannot say whether an operator chose it or the fd budget did, and
    calling a derived value "explicit" sends someone hunting for a setting
    nobody wrote.
    """
    import os  # noqa: PLC0415
    raw = os.environ.get('BB_MAX_CONNECTIONS')
    if not resolved:
        return 'uncapped', 'no cap in force — relying on the OS descriptor limit'
    if raw is None or raw.strip().lower() in ('', 'auto'):
        return str(resolved), 'derived from RLIMIT_NOFILE (BB_MAX_CONNECTIONS=auto)'
    return str(resolved), 'set explicitly via BB_MAX_CONNECTIONS'


class Server:
    """An asyncio socket server that dispatches each connection through the
    app's [`ProtocolRegistry`][blackbull.server.protocol_registry.ProtocolRegistry].

    The shared HTTP listener detects HTTP/1.1 vs HTTP/2 (and upgrades to
    WebSocket); port-bound non-ASGI protocols registered via
    [`BlackBull.raw_handler`][BlackBull.raw_handler] get their own listening socket.
    When ssl_context or certfile is set, the HTTP listener runs as HTTPS.

    ``ASGIServer`` is an alias of this class.
    """
    def __init__(self, app, *,
                 ssl_context=None, certfile=None, keyfile=None, password=None,
                 max_connections: int = 0,
                 stream_queue_depth: int = _HTTP2_STREAM_QUEUE_DEPTH,
                 ws_queue_depth: int = _WS_READ_INLINE,
                 protocol_registry=None,
                 listeners=None,
                 **kwds):
        self.app = app
        # ``None`` = the caller said it the old way; ``open_socket`` builds
        # the one listener that stands for it.
        self._listeners = list(listeners) if listeners else None
        #: ``[(Listener, [socket, ...]), ...]`` — what is actually bound.
        self.bound_listeners: list = []
        self._max_connections = max_connections
        logger.info('max_connections=%s (%s)', *_max_connections_report(max_connections))
        self._lifespan_cm: LifespanManager | None = None
        self._stream_queue_depth = stream_queue_depth
        self._ws_queue_depth = ws_queue_depth
        self._active_connections = 0

        # An app carries a registry only once a raw_handler is registered.
        from .protocol_registry import ProtocolRegistry as _PR  # noqa: PLC0415
        self._protocol_registry = (protocol_registry
                                   or getattr(app, '_protocol_registry', None)
                                   or _PR())
        self.protocol_ports: dict[str, int] = {}
        self._connection_tasks: set = set()
        self._stopping = False
        self._drain_timeout = None
        self._stopped_event = asyncio.Event()
        self._stop_done_event = asyncio.Event()
        self._stop_error: BaseException | None = None
        # Process-wide singletons: looked up once, not once per accept.
        from ..event_aggregator import EventAggregator as _EA  # noqa: PLC0415
        self._cached_dispatcher = getattr(self.app, '_dispatcher', None)
        self._cached_aggregator = (_EA(self._cached_dispatcher)
                                    if self._cached_dispatcher is not None
                                    else None)

        if ssl_context and (certfile or keyfile):
            raise TypeError('SSLContext and certfile (or keyfile) must not be set at the same time')

        self._ssl_context = ssl_context
        self.keyfile = keyfile
        self.certfile = certfile
        self.make_ssl_context()
        self.socket = None
        self.raw_sockets: list = []
        self.port = None
        self.unix_path: str | None = None

    @property
    def ssl_context(self):
        """The context a listener built from ``certfile``/``keyfile`` uses.

        Assigning it re-points every bound listener that was carrying the
        previous one — which is what makes configuring mTLS after
        ``open_socket()`` work, and what keeps ``bound_listeners`` honest about
        what is actually being served.  A listener the caller stated carries
        its own context and is left alone, because identity is the difference
        between "the server's" and "its own".
        """
        return self._ssl_context

    @ssl_context.setter
    def ssl_context(self, value):
        previous = getattr(self, '_ssl_context', None)
        self._ssl_context = value
        for index, (listener, socks) in enumerate(getattr(self, 'bound_listeners', ())):
            if listener.tls is previous:
                self.bound_listeners[index] = (replace(listener, tls=value), socks)

    @property
    def keyfile(self):
        """The TLS private-key path, or ``None``.

        Assigning a path that is not a file raises ``FileNotFoundError`` there
        and then, rather than at handshake time.
        """
        return self._keyfile if hasattr(self, '_keyfile') else None

    @keyfile.setter
    def keyfile(self, value):
        if value and not Path(value).is_file():
            raise FileNotFoundError(f'keyfile not found: {value}')
        self._keyfile = value


    @property
    def certfile(self):
        """The TLS certificate path, or ``None``.

        Assigning a path that is not a file raises ``FileNotFoundError`` there
        and then, rather than at handshake time.
        """
        return self._certfile if hasattr(self, '_certfile') else None

    @certfile.setter
    def certfile(self, value):
        if value and not Path(value).is_file():
            raise FileNotFoundError(f'certfile not found: {value}')
        self._certfile = value


    def make_ssl_context(self):
        logger.debug(self.certfile)
        logger.debug(self.keyfile)
        if not self.certfile or not self.keyfile:
            # ``__init__`` calls this before both properties are set; defer
            # rather than raise, so the half-configured state is not an error.
            return

        context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        context.set_alpn_protocols(['h2', 'http/1.1'])
        context.load_cert_chain(certfile=self.certfile, keyfile=self.keyfile)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.options |= ssl.OP_NO_COMPRESSION
        # Resumption without a full handshake, worth ~1 RTT — to TLS 1.2 only.
        # TLS 1.3 tickets are independent of this flag.
        if hasattr(ssl, 'SESS_CACHE_SERVER'):
            context.set_session_cache_mode(ssl.SESS_CACHE_SERVER)  # type: ignore[attr-defined]
        self.ssl_context = context

        if hasattr(self, 'raw_sockets'):
            # Already-bound sockets need no rewrap; see ``open_socket``.
            pass

    def configure_mtls(self, ca_cert: str) -> None:
        """Require a client certificate signed by *ca_cert*.

        TLS must already be configured; on a plaintext server this raises
        ``RuntimeError`` rather than silently serving without mTLS.
        """
        if self.ssl_context is None:
            raise RuntimeError('configure_mtls() requires TLS to be configured first.')
        self.ssl_context.verify_mode = ssl.CERT_REQUIRED
        self.ssl_context.load_verify_locations(cafile=ca_cert)

    def connection_protocol_factory(self, bound_binding=None):
        """Factory for `loop.create_server` — one buffered protocol per accept.

        The protocol spawns the serving task itself, a protocol factory being
        synchronous.  On an SSL transport ``connection_made`` fires after the
        handshake, so ALPN is already decided when that task runs.
        """
        from .connection_protocol import ConnectionProtocol  # noqa: PLC0415
        from .sender import AsyncioWriter  # noqa: PLC0415
        from ..env import get_settings as _get_settings  # noqa: PLC0415

        server = self
        write_timeout = _get_settings().write_timeout

        class _ServedConnection(ConnectionProtocol):
            def connection_made(self, transport):
                super().connection_made(transport)
                # Eager start runs the serve prologue inside this callback and
                # parks at the same read it would have parked at anyway, one
                # loop iteration earlier — a hop paid once per connection, so
                # it buys churn latency, not keep-alive throughput.  It does
                # not move where a failure lands: a raise before the first
                # suspension completes the task, so ``_serve_done`` still
                # reports it rather than this transport callback.
                if _EAGER_TASKS:
                    # ``loop=`` is load-bearing: without it ``eager_start``
                    # leaves ``_loop`` unset and crashes on 3.12+ (seen on
                    # 3.14: ``'NoneType' object has no attribute 'is_running'``).
                    task = asyncio.Task(self._serve(),
                                        loop=asyncio.get_running_loop(),
                                        eager_start=True)
                else:
                    task = asyncio.create_task(self._serve())
                # A protocol factory cannot await, so the task is detached; the
                # done-callback is what keeps a failure from surfacing as
                # asyncio's "Task exception was never retrieved" at GC time.
                self._serve_task = task
                server._connection_tasks.add(task)
                task.add_done_callback(self._serve_done)

            async def _serve(self):
                try:
                    await server._serve_connection(
                        self.reader,
                        AsyncioWriter(self, write_timeout=write_timeout),
                        bound_binding=bound_binding,
                        transport=self.transport,
                    )
                finally:
                    # A bare close would RST away the response a peer that is
                    # still sending has not read yet.
                    await self.linger_close()

            @staticmethod
            def _serve_done(task):
                server._connection_tasks.discard(task)
                if task.cancelled():
                    return
                exc = task.exception()
                if exc is not None:
                    logger.exception(
                        'connection task failed', exc_info=exc)

        return _ServedConnection

    async def client_connected_cb(self, reader, writer):
        """Accept callback for the shared HTTP listener."""
        await self._serve_connection(reader, writer)

    def _raw_connected_cb(self, binding):
        """Build an accept callback for a port-bound non-ASGI protocol."""
        async def _cb(reader, writer):
            await self._serve_connection(reader, writer, bound_binding=binding)
        return _cb

    def _raw_tls_context(self):
        """TLS context for ``tls=True`` raw bindings.

        When cert/key paths are available a dedicated context is built without
        the HTTP listener's ``h2``/``http/1.1`` ALPN list — a raw-protocol
        client offering its own ALPN token (e.g. ``mqtt``) must not fail the
        handshake on no-overlap.  A caller-supplied ``ssl_context`` is reused
        as-is (there is nothing to rebuild it from).
        """
        if not (self.certfile and self.keyfile):
            return self.ssl_context
        context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        context.load_cert_chain(certfile=self.certfile, keyfile=self.keyfile)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.options |= ssl.OP_NO_COMPRESSION
        if hasattr(ssl, 'SESS_CACHE_SERVER'):
            context.set_session_cache_mode(ssl.SESS_CACHE_SERVER)  # type: ignore[attr-defined]
        return context

    async def _serve_connection(self, reader, writer, *, bound_binding=None,
                                transport=None):
        """Wrap the transport and run one ``ConnectionActor``.

        *bound_binding* is set for port-bound non-ASGI protocols: the
        connection skips HTTP detection and goes straight to the raw handler.

        *transport* is passed explicitly by the buffered-protocol path, which
        has no `StreamWriter` to carry it — and peer/socket names and the TLS
        object are read from it, so it cannot be inferred.
        """
        from .conn_id import new_connection_id  # noqa: PLC0415
        from .connection_actor import ConnectionActor  # noqa: PLC0415
        from .sender import AsyncioWriter  # noqa: PLC0415
        from .recipient import AsyncioReader  # noqa: PLC0415

        if transport is None:
            transport = getattr(writer, 'transport', None)
        peername = transport.get_extra_info('peername') if transport else None
        sockname = transport.get_extra_info('sockname') if transport else None
        # ASGI 3.0 §Connection Scope requires ``scope['client']`` and
        # ``scope['server']`` to be ``(host, port)``.  What the transport hands
        # back is not: AF_UNIX gives a bare path string (and an empty peername),
        # AF_INET6 a 4-tuple ``(host, port, flowinfo, scope_id)``.  Normalise
        # both here so the actor layer never special-cases a family.
        if isinstance(sockname, str):
            sockname = (sockname, None)
        if isinstance(peername, str):
            peername = (peername or '', None)
        if isinstance(sockname, tuple) and len(sockname) > 2:
            sockname = sockname[:2]
        if isinstance(peername, tuple) and len(peername) > 2:
            peername = peername[:2]
        ssl_object = transport.get_extra_info('ssl_object') if transport else None
        ssl_flag = ssl_object is not None
        alpn = ssl_object.selected_alpn_protocol() if ssl_object else None

        # No setsockopt on this path, by design: SO_SNDBUF / SO_RCVBUF /
        # TCP_USER_TIMEOUT are set once on the listening socket and inherited,
        # and idle keep-alive ghosts are HTTP1Actor's timer to evict.
        wrapped_reader = (reader if isinstance(reader, AbstractReader)
                          else AsyncioReader(reader))
        if isinstance(writer, AbstractWriter):
            wrapped_writer = writer
        else:
            from ..env import get_settings as _get_settings  # noqa: PLC0415
            wrapped_writer = AsyncioWriter(
                writer, write_timeout=_get_settings().write_timeout)

        aggregator = self._cached_aggregator

        if self._max_connections and self._active_connections >= self._max_connections:
            logger.warning(
                'Connection limit reached (%d/%d) — 503 to %s',
                self._active_connections, self._max_connections, peername,
            )
            # This fires before ConnectionActor binds a CapHitCounter, so the
            # contextvar is unset and the record is emitted unconditionally —
            # safe, because nobody gets a connection past the cap to flood it.
            log_cap_hit('max_connections',
                        requested=self._active_connections + 1,
                        limit=self._max_connections,
                        peer=peername, protocol='tcp')
            # h1 is the safe guess: the peer has not spoken.
            if bound_binding is None and alpn != 'h2':
                try:
                    await wrapped_writer.write(
                        b'HTTP/1.1 503 Service Unavailable\r\n'
                        b'retry-after: 1\r\n'
                        b'content-length: 0\r\n'
                        b'connection: close\r\n'
                        b'\r\n')
                except Exception:
                    logger.debug(
                        '503 write failed for %s (peer disconnected?)',
                        peername)
                # The close decides; ``docs/about/internals.md`` §Rejecting
                # requires lingering.
                await wrapped_writer.close()
            else:
                # Nothing was written, so there is no response to protect.
                await wrapped_writer.close()
            return

        self._active_connections += 1
        try:
            actor = ConnectionActor(
                wrapped_reader, wrapped_writer, self.app, aggregator,
                peername=peername, sockname=sockname, ssl=ssl_flag,
                alpn=alpn,
                stream_queue_depth=self._stream_queue_depth,
                ws_queue_depth=self._ws_queue_depth,
                registry=self._protocol_registry,
                bound_binding=bound_binding,
                connection_id=new_connection_id(),
            )
            await actor.run()
        finally:
            self._active_connections -= 1

    def open_socket(self, port=0, unix_path: str | None = None,
                    inherited_fd: int | None = None):
        """Bind every listener this server was asked for.

        A caller that named ``listeners=`` gets those.  A caller that said it
        the old way — a port, a Unix path, or an inherited fd — gets one
        listener built from those arguments, so there is one binding path and
        not two.
        """
        from ..env import get_settings as _get_settings  # noqa: PLC0415
        _cfg = _get_settings()
        if self.bound_listeners:
            self._publish_socket_view()
            return

        acquired: list[tuple[Listener, list]] = []
        protocol_ports: dict[str, int] = {}
        listeners = self._listeners
        try:
            if listeners is None:
                inherited = adopt_inherited_sockets()
                if inherited:
                    acquired.append((
                        Listener(_address_of(inherited[0]), tls=self.ssl_context),
                        inherited,
                    ))
                    listeners = None
                else:
                    listeners = [
                        _listener_from_args(port, unix_path, inherited_fd,
                                            self.ssl_context)]

            if not acquired:
                for listener in listeners:
                    acquired.append((listener, self._bind_listener(listener, _cfg)))

            self._bind_protocol_sockets(
                _cfg, acquired=acquired, protocol_ports=protocol_ports)
            _validate_unique_socket_fds(acquired)
            raw_sockets, resolved_port, resolved_unix = self._socket_view(acquired)
        except BaseException:
            close_sockets(
                sock for _listener, socks in acquired for sock in socks)
            raise

        self._listeners = listeners
        self.bound_listeners = acquired
        self.raw_sockets = raw_sockets
        self.port = resolved_port
        self.unix_path = resolved_unix
        self.protocol_ports = protocol_ports

    def _bind_listener(self, listener, _cfg) -> list:
        """Bind one listener and return its sockets."""
        where = listener.where

        if isinstance(where, InheritedFd):
            return [adopt_listening_fd(where.fd)]

        if isinstance(where, Unix):
            sock = create_unix_socket(
                where.path,
                backlog=_cfg.socket_backlog,
                sndbuf=_cfg.socket_sndbuf,
                rcvbuf=_cfg.socket_rcvbuf,
            )
            if sock is None:
                raise RuntimeError(
                    f'Failed to bind AF_UNIX socket on {where.path!r}.')
            return [sock]

        socks = create_configured_sockets(
            where.port, _cfg, reuseport=_cfg.socket_reuseport, host=where.host)
        if not socks:
            # Binding is the availability check.  A connect probe before it was
            # racy, IPv4-localhost only, and hid the OS error.
            logger.error(f'Failed to bind port {where.port}. Try another port.')
            raise RuntimeError(
                f'Failed to bind port {where.port} (see log for the OS error, '
                f'e.g. address already in use). Try another port.')
        return socks

    def _publish_socket_view(self) -> None:
        """Expose the bound listeners the way the rest of the server reads them.

        ``raw_sockets`` is every HTTP socket — what the multi-worker master
        hands each worker.  ``port`` / ``unix_path`` describe the first one.
        """
        self.raw_sockets, self.port, self.unix_path = self._socket_view(
            self.bound_listeners)

    @staticmethod
    def _socket_view(bound_listeners):
        raw_sockets = [sock for listener, socks in bound_listeners
                       for sock in socks if listener.speaks == HTTP]
        first = raw_sockets[0] if raw_sockets else None
        sockname = first.getsockname() if first is not None else None
        if isinstance(sockname, str):
            return raw_sockets, None, sockname
        elif sockname is not None:
            return raw_sockets, sockname[1], None
        return raw_sockets, None, None

    def _bind_protocol_sockets(self, _cfg, *, acquired, protocol_ports):
        """Bind a listening socket per port-bound non-ASGI protocol.

        Each [`RawBinding`][] registered with a ``port`` gets its own
        dual-stack socket set, recorded in [`protocol_ports`][].  Sockets are
        bound bare here; [`run`][] layers TLS onto the listeners whose
        binding set ``tls=True``, cleartext otherwise.  These ports get no
        ``SO_REUSEPORT``: a stateful protocol needs a single owning worker.
        """
        # Iterate the bindings, not a port-keyed view of them: several may ask
        # for port=0, which a port key would collapse into one listener.
        for binding in self._protocol_registry.raw_bindings.values():
            if binding.port is None:
                continue
            if binding.tls and self.ssl_context is None:
                raise RuntimeError(
                    f'Raw protocol binding {binding.name!r} requires TLS '
                    f'(tls=True) but the server has no certificate configured '
                    f'— pass certfile/keyfile or an ssl_context.')
            tls_context = self._raw_tls_context() if binding.tls else None
            port = binding.port
            socks = create_configured_sockets(port, _cfg, reuseport=False)
            if not socks:
                raise RuntimeError(
                    f'Failed to bind {binding.name!r} on port {port}.')
            # Track the sockets before asking them for metadata: even an
            # unusual getsockname failure must unwind through open_socket's
            # transaction rather than strand the descriptors.
            acquired.append((
                Listener(Tcp(port), speaks=binding.name, tls=tls_context),
                socks))
            bound_port = socks[0].getsockname()[1]
            protocol_ports[binding.name] = bound_port
            acquired[-1] = (
                Listener(Tcp(bound_port), speaks=binding.name,
                         tls=tls_context),
                socks)
            logger.info('Protocol %r listening on port %d', binding.name, bound_port)

    def close_socket(self):
        cleanup_error = self._close_socket()
        if cleanup_error is not None:
            raise cleanup_error

    def _close_socket(self):
        fallback = list(self.raw_sockets)
        listeners = self._take_bound_listeners()
        sockets = [sock for _listener, group in listeners for sock in group]
        if not sockets:
            sockets = fallback
        return close_sockets(sockets)

    def _take_bound_listeners(self):
        listeners = self.bound_listeners
        self.bound_listeners = []
        self.raw_sockets = []
        self.port = None
        self.unix_path = None
        self.protocol_ports = {}
        return listeners

    async def startup(self):
        """Drive the ASGI lifespan startup handshake.

        Launches the app's lifespan task, delivers 'lifespan.startup', and
        waits for 'lifespan.startup.complete'.  Raises RuntimeError on
        'lifespan.startup.failed'.  Stores the context manager so that
        shutdown() can deliver 'lifespan.shutdown' to the same task.
        """
        self._lifespan_cm = LifespanManager(self.app)
        await self._lifespan_cm.__aenter__()

    async def shutdown(self):
        """Drive the ASGI lifespan shutdown handshake.

        Raises ``RuntimeError`` if ``startup()`` was never called.  After a
        failed ``startup()`` it cleans up quietly, so ``try``/``finally`` is safe.
        """
        if self._lifespan_cm is None:
            raise RuntimeError(
                'Server.shutdown() called without Server.startup(); there is '
                'no lifespan to shut down.')
        await self._lifespan_cm.__aexit__(None, None, None)

    async def run(self, port=80):
        """Run an asyncio socket server with the setting in this object."""
        budget = _AsyncCleanupBudget()
        self._cleanup_budget = budget
        startup_committed = False
        primary = None
        cleanup_error = None
        try:
            if not self.bound_listeners:
                if self.raw_sockets:
                    self.bound_listeners = [
                        (Listener(_address_of(sock), tls=self.ssl_context), [sock])
                        for sock in self.raw_sockets]
                else:
                    self.open_socket(port)

            _validate_unique_socket_fds(self.bound_listeners)

            groups: dict[object, list] = defaultdict(list)
            for listener, socks in self.bound_listeners:
                binding = (None if listener.speaks == HTTP else
                           self._protocol_registry.raw_bindings.get(listener.speaks))
                factory = self.connection_protocol_factory(binding)
                for sock in socks:
                    groups[listener.tls].append((sock, factory))

            async with AsyncExitStack() as stack:
                servers = []
                for context, pairs in groups.items():
                    servers += await stack.enter_async_context(
                        SocketManager(pairs, context, budget))
                self._running_servers = servers
                logger.info(
                    'Bound %d server(s); accepting when lifespan startup completes',
                    len(servers))
                async with LifespanManager(self.app, budget):
                    logger.info(f'Server(s) created: {servers}')
                    for srv in servers:
                        if self._stopping:
                            break
                        await srv.start_serving()
                    startup_committed = True
                    try:
                        await self._stopped_event.wait()
                    except KeyboardInterrupt:
                        logger.info('KeyboardInterrupt received — shutting down.')
                    except asyncio.CancelledError:
                        # Cancellation after accepting starts retains the public
                        # run() contract: unwind cleanly instead of propagating.
                        logger.info('Server task cancelled.')
                    except Exception as exc:
                        logger.error('Server error: %s', exc)

                    if self._stopping:
                        await _wait_for_event_ignoring_cancellation(
                            self._stop_done_event)
                        if self._stop_error is not None:
                            raise self._stop_error
        except BaseException as exc:
            primary = exc
            if not startup_committed:
                cleanup_error = await self._cancel_connection_tasks(budget)
            raise
        finally:
            socket_error = self._close_socket()
            cleanup_error = combine_cleanup_errors(cleanup_error, socket_error)
            if cleanup_error is not None:
                if primary is None:
                    raise cleanup_error
                logger.error('Server cleanup failed: %s', cleanup_error)

        logger.info('Server has been stopped.')

    async def stop(self, drain_timeout: float = 8.0) -> None:
        """Stop accepting, then let the connections already being served finish.

        Nothing in flight is cancelled while *drain_timeout* lasts: a cancelled
        handler leaves a client holding a half-written response.  Whatever is
        left at the deadline is cancelled — a shutdown that must complete still
        completes.  Keep the budget inside
        ``MultiWorkerServer.shutdown_timeout`` so the drain ends here and not
        in a SIGKILL.
        """
        if self._stopping:
            await self._stop_done_event.wait()
            if self._stop_error is not None:
                raise self._stop_error
            return
        self._stopping = True
        self._drain_timeout = drain_timeout
        budget = getattr(self, '_cleanup_budget', None)
        if budget is None:
            budget = self._cleanup_budget = _AsyncCleanupBudget(drain_timeout)
        budget.start(drain_timeout)

        # Close every listener first, so the drain is over a set that only
        # shrinks.  A failed close must not strand the remaining listeners.
        running_servers = list(getattr(self, '_running_servers', ()))
        errors = [_close_servers(running_servers)]
        self._stopped_event.set()

        try:
            try:
                await self._drain(drain_timeout)
            except (asyncio.CancelledError, Exception) as exc:
                errors.append(exc)
                logger.exception('Failed to drain connections during shutdown')

            try:
                errors.append(
                    await _wait_async_servers_closed(running_servers, budget))
            except (asyncio.CancelledError, Exception) as exc:
                errors.append(exc)
        except (KeyboardInterrupt, SystemExit) as exc:
            errors.append(exc)
            raise
        finally:
            cleanup_error = combine_cleanup_errors(*errors)
            self._stop_error = cleanup_error
            self._stop_done_event.set()
        if cleanup_error is not None:
            raise cleanup_error

    async def _drain(self, drain_timeout: float) -> None:
        """Let the connections already being served finish.  Idempotent.

        Called by [`stop`][], and again by [`run`][] on its way out: a
        drain started from a signal handler is racing its caller's teardown,
        and returning into ``asyncio.run()`` would cancel both the drain and
        the request it is protecting.  Whichever arrives second finds nothing
        left to wait for.
        """
        pending = [t for t in self._connection_tasks if not t.done()]
        if not pending:
            return
        logger.info('Draining %d connection(s), up to %.1fs',
                    len(pending), drain_timeout)
        budget = getattr(self, '_cleanup_budget', None)
        timeout = (budget.remaining() if budget is not None else drain_timeout)
        _done, still = await asyncio.wait(pending, timeout=timeout)
        if still:
            logger.warning(
                '%d connection(s) did not finish within %.1fs — cancelling',
                len(still), drain_timeout)
            for task in still:
                task.cancel()
            if budget is None:
                await asyncio.gather(*still, return_exceptions=True)
            else:
                cleanup_error = await _cancel_tasks(still, budget)
                if cleanup_error is not None:
                    raise cleanup_error

    async def _cancel_connection_tasks(self, budget) -> Exception | None:
        return await _cancel_tasks(
            [task for task in self._connection_tasks if not task.done()], budget)

    def wait_for_port(self, timeout: float = 10.0, poll_interval: float = 0.1):
        if self.port is None:
            raise RuntimeError("Server port is not set")

        # A request, not just a connect: the listen socket exists in the parent
        # from before the fork, so only a served response proves that a child's
        # event loop is running.
        import http.client
        deadline = time.time() + timeout
        while True:
            try:
                conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=1)
                conn.request('GET', '/_healthz')
                conn.getresponse()
                conn.close()
                return True
            except http.client.RemoteDisconnected:
                # A TLS listener hanging up on our cleartext request is still
                # a live loop answering.
                return True
            except OSError:
                if time.time() >= deadline:
                    raise TimeoutError(
                        f"Port {self.port} on 127.0.0.1 did not open within {timeout} seconds"
                    )
                time.sleep(poll_interval)

    def close(self):
        logger.info('Server.close() is called.')
        logger.info(self.__dict__)
        self.close_socket()


ASGIServer = Server
