import asyncio
import contextlib

from http import HTTPStatus
import logging
import ssl
import sys
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path
import time

# private library
from ..protocol.rsock import (
    create_dual_stack_sockets, create_unix_socket,
    adopt_inherited_sockets, adopt_listening_fd,
)
from .sender import AbstractWriter
from .recipient import (AbstractReader,
                        _HTTP2_STREAM_QUEUE_DEPTH, _WS_READ_INLINE)
from .cap_log import log_cap_hit
from ..asgi import ASGIEvent
logger = logging.getLogger(__name__)

#: ``eager_start`` landed in 3.12; the supported floor is 3.11.  A task
#: constructed with it runs its coroutine synchronously until the first
#: suspension instead of queueing that first step for the next loop iteration.
_EAGER_TASKS = sys.version_info >= (3, 12)


class LifespanManager:
    """Async context manager that drives the ASGI lifespan protocol.

    On enter: launches the app's lifespan task and delivers 'lifespan.startup'.
    Raises RuntimeError if the app responds with 'lifespan.startup.failed'.
    On exit: delivers 'lifespan.shutdown' and waits for 'lifespan.shutdown.complete'.

    Implemented as a class (not asynccontextmanager) so that __aenter__ and
    __aexit__ can be called independently — e.g. startup() / shutdown() — without
    leaving a zombie async-generator that asyncio tries to finalize on loop close.
    """

    def __init__(self, app):
        self._app = app
        self._receive_q: asyncio.Queue = asyncio.Queue()
        self._send_q:    asyncio.Queue = asyncio.Queue()
        self._task = None

    async def __aenter__(self):
        scope = {'type': 'lifespan', 'asgi': {'version': '3.0'}}
        self._task = asyncio.create_task(
            self._app(scope, self._receive_q.get, self._send_q.put))
        await self._receive_q.put({'type': ASGIEvent.LIFESPAN_STARTUP})
        # Race the startup ack against the lifespan task itself.  A lifespan
        # app that dies before acking — e.g. a startup hook that raises and
        # takes the task down with it — would otherwise strand __aenter__ on
        # an empty send queue forever and the server would neither start nor
        # error.  Mirrors the FIRST_COMPLETED race in __aexit__.
        getter = asyncio.ensure_future(self._send_q.get())
        try:
            await asyncio.wait(
                {getter, self._task}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            if not getter.done():
                getter.cancel()
        if getter.done() and not getter.cancelled():
            event = getter.result()
            if event.get('type') == ASGIEvent.LIFESPAN_STARTUP_FAILED:
                raise RuntimeError(event.get('message', 'Lifespan startup failed'))
            return self
        # The task finished before acking startup.
        if self._task.done():
            exc = self._task.exception()
            if exc is not None:
                # The lifespan app raised before acking — a real startup
                # failure.  Without this raise, __aenter__ strands on an
                # empty send queue and the server never starts.
                raise RuntimeError(f'Lifespan startup failed: {exc!r}') from exc
            # The app returned without implementing the lifespan protocol — a
            # bare ASGI app that ignores the lifespan scope.  ASGI treats this
            # as "lifespan unsupported": proceed to serve rather than hang or
            # error.
            return self
        raise RuntimeError('Lifespan startup did not complete')

    async def __aexit__(self, *_):
        task = self._task
        if task is None:
            return False
        # Drive the shutdown handshake only while the lifespan app is alive.
        # A lifespan task that was cancelled out from under us — e.g. by
        # asyncio.run()'s _cancel_all_tasks during interpreter teardown, which
        # cancels *every* outstanding task at once — will never emit
        # lifespan.shutdown.complete.  Waiting unconditionally on the send
        # queue would then block __aexit__ forever and wedge the whole
        # teardown (observed as an H2 "deadlock" in the flow-control
        # conformance subprocess).  Race the acknowledgement against the task
        # itself so a dead lifespan app can never strand us.
        if not task.done():
            await self._receive_q.put({'type': ASGIEvent.LIFESPAN_SHUTDOWN})
            getter = asyncio.ensure_future(self._send_q.get())
            try:
                await asyncio.wait(
                    {getter, task}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                getter.cancel()
        if not task.done():
            task.cancel()
        try:
            await task   # drain finally blocks inside the lifespan app
        except asyncio.CancelledError:
            pass  # task was just cancelled; its unwind is expected.
        return False


@asynccontextmanager
async def SocketManager(socket_cb_pairs, ssl_context):
    """Async context manager that creates asyncio servers from already-bound sockets.

    *socket_cb_pairs* is an iterable of ``(sock, protocol_factory)`` — each
    socket is served by its own factory.  The shared HTTP listener and each
    port-bound non-ASGI protocol both come from
    :meth:`Server.connection_protocol_factory`, differing only in whether a
    binding is pre-committed.

    On enter: wraps each socket in ``loop.create_server`` (TCP) or
    ``loop.create_unix_server`` (AF_UNIX) and yields the list.  Not
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
    # AF_UNIX is absent on platforms without Unix-domain socket support
    # (notably some Windows builds where socket.AF_UNIX is not defined).
    # Use a sentinel so the family comparison never raises AttributeError.
    _af_unix = getattr(_socket, 'AF_UNIX', None)
    loop = asyncio.get_running_loop()
    servers = []
    for sock, factory in socket_cb_pairs:
        # ssl_handshake_timeout is meaningful only when SSL is enabled.
        kwargs = {'sock': sock, 'ssl': ssl_context, 'backlog': _backlog}
        if ssl_context is not None:
            kwargs['ssl_handshake_timeout'] = 60.0
        if _af_unix is not None and sock.family == _af_unix:
            # AF_UNIX needs the dedicated unix-server entry point — the
            # TCP create_server() rejects non-INET families at family-
            # validation time.
            srv = await loop.create_unix_server(factory, **kwargs)
        else:
            srv = await loop.create_server(factory, **kwargs)
        servers.append(srv)
    try:
        yield servers
    finally:
        for srv in servers:
            srv.close()


def _max_connections_report(resolved: int) -> tuple[str, str]:
    """Describe the connection cap in force, and where it came from.

    ``BB_MAX_CONNECTIONS`` resolves to a plain integer long before it
    reaches the server, so the number alone cannot say whether an
    operator chose it or the fd budget did.  Calling a derived value
    "explicit" would send someone hunting for a setting nobody wrote,
    which is the opposite of what logging it is for.
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
    app's :class:`~blackbull.server.protocol_registry.ProtocolRegistry`.

    The shared HTTP listener detects HTTP/1.1 vs HTTP/2 (and upgrades to
    WebSocket); port-bound non-ASGI protocols registered via
    :meth:`BlackBull.raw_handler` get their own listening socket.
    When ssl_context or certfile is set, the HTTP listener runs as HTTPS.

    Formerly ``ASGIServer`` — that name remains as a backward-compat alias.
    """
    def __init__(self, app, *,
                 ssl_context=None, certfile=None, keyfile=None, password=None,
                 max_connections: int = 0,
                 stream_queue_depth: int = _HTTP2_STREAM_QUEUE_DEPTH,
                 ws_queue_depth: int = _WS_READ_INLINE,
                 protocol_registry=None,
                 **kwds):
        self.app = app
        self._max_connections = max_connections
        # A derived default depends on the host, so the only way an operator
        # learns the value in force is by being told it — including where it
        # came from.  Reporting a derived number as "explicit" would send
        # someone hunting for a setting nobody wrote.  The origin is read from
        # the environment rather than passed in, because every caller that
        # resolves the value throws that fact away.
        logger.info('max_connections=%s (%s)', *_max_connections_report(max_connections))
        self._stream_queue_depth = stream_queue_depth
        self._ws_queue_depth = ws_queue_depth
        self._active_connections = 0

        # Protocol registry: explicit arg wins, else the app's (a BlackBull
        # carries one once a raw_handler is registered), else a default holding
        # only the built-in http1/http2 bindings.
        from .protocol_registry import ProtocolRegistry as _PR  # noqa: PLC0415
        self._protocol_registry = (protocol_registry
                                   or getattr(app, '_protocol_registry', None)
                                   or _PR())
        # name -> bound port, populated by open_socket for port-bound protocols.
        self.protocol_ports: dict[str, int] = {}
        # list of (raw_sockets, binding) bound for non-ASGI protocols.
        self._protocol_sockets: list = []
        # Live connection tasks.  Held so a shutdown has something to wait on:
        # each task owned its own lifetime and nothing aggregated them, so
        # "let the in-flight finish" had no referent.  Discarded by the same
        # done-callback that already reports failures.
        self._connection_tasks: set = set()
        # Set when a graceful stop begins: listeners are closed and no new
        # connection is accepted, but the ones already being served are not
        # touched.
        self._stopping = False
        # Cache the dispatcher + aggregator pair once — both are
        # process-wide singletons.  Looking them up per accept is wasted
        # work on the hot connection-burst path.
        from ..event_aggregator import EventAggregator as _EA  # noqa: PLC0415
        self._cached_dispatcher = getattr(self.app, '_dispatcher', None)
        self._cached_aggregator = (_EA(self._cached_dispatcher)
                                    if self._cached_dispatcher is not None
                                    else None)

        # Create TLS context
        if ssl_context and (certfile or keyfile):
            raise TypeError('SSLContext and certfile (or keyfile) must not be set at the same time')

        self.ssl_context = ssl_context
        self.keyfile = keyfile
        self.certfile = certfile
        self.make_ssl_context()
        self.socket = None
        self.port = None
        self.unix_path: str | None = None

    @property
    def keyfile(self):
        return self._keyfile if hasattr(self, '_keyfile') else None

    @keyfile.setter
    def keyfile(self, value):
        if value and not Path(value).is_file():
            raise FileNotFoundError(f'keyfile not found: {value}')
        self._keyfile = value


    @property
    def certfile(self):
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
            # One or both paths not yet assigned (called during __init__ before
            # both properties are set).  Silently defer until both are ready.
            return

        context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        context.set_alpn_protocols(['h2', 'http/1.1'])
        context.load_cert_chain(certfile=self.certfile, keyfile=self.keyfile)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.options |= ssl.OP_NO_COMPRESSION
        # Enable server-side session cache so reconnecting clients can resume
        # without a full handshake (saves ~1 RTT and CPU on TLS 1.2 connections).
        # TLS 1.3 uses its own 0-RTT ticket mechanism independently of this flag.
        if hasattr(ssl, 'SESS_CACHE_SERVER'):
            context.set_session_cache_mode(ssl.SESS_CACHE_SERVER)  # type: ignore[attr-defined]
        self.ssl_context = context

        if hasattr(self, 'raw_sockets'):
            # raw_sockets are already bound; asyncio.start_server will handle
            # TLS via ssl= so no manual wrapping is needed here.
            pass

    def configure_mtls(self, ca_cert: str) -> None:
        if self.ssl_context is None:
            raise RuntimeError('configure_mtls() requires TLS to be configured first.')
        self.ssl_context.verify_mode = ssl.CERT_REQUIRED
        self.ssl_context.load_verify_locations(cafile=ca_cert)

    def connection_protocol_factory(self, bound_binding=None):
        """Factory for `loop.create_server` — one buffered protocol per accept.

        Replaces the ``start_server`` callback pair: instead of a StreamReader
        and StreamWriter over asyncio's own buffering, the connection owns a
        single buffer the kernel writes into, and the actor reads by cursor.

        The protocol spawns the serving task itself because a protocol factory
        is synchronous.  ``connection_made`` fires after the TLS handshake on
        an SSL transport, so ALPN is already decided by the time the task runs
        — same ordering the callback form relied on.
        """
        from .connection_protocol import ConnectionProtocol  # noqa: PLC0415
        from .sender import AsyncioWriter  # noqa: PLC0415
        from ..env import get_settings as _get_settings  # noqa: PLC0415

        server = self
        write_timeout = _get_settings().write_timeout

        class _ServedConnection(ConnectionProtocol):
            def connection_made(self, transport):
                super().connection_made(transport)
                # Started eagerly: the serve prologue — deadline handle,
                # detection order, first peek — runs inside this callback and
                # parks at the same read it would have parked at anyway, one
                # loop iteration earlier.  A connection pays that hop once, so
                # it is churn latency rather than keep-alive throughput.
                #
                # Eager start does not change where a failure lands: a
                # coroutine that raises before its first suspension completes
                # the task with that exception rather than raising out of this
                # transport callback, so ``_serve_done`` still reports it.
                if _EAGER_TASKS:
                    # The explicit ``loop=`` is load-bearing, not cosmetic:
                    # ``Task(..., eager_start=True)`` without it leaves
                    # ``_loop`` unset and crashes on 3.12+ (verified on 3.14:
                    # ``'NoneType' object has no attribute 'is_running'``).
                    task = asyncio.Task(self._serve(),
                                        loop=asyncio.get_running_loop(),
                                        eager_start=True)
                else:
                    task = asyncio.create_task(self._serve())
                # A protocol factory cannot await, so the task is detached.
                # Held for the connection's lifetime and given a done-callback
                # so a failure surfaces as a log line rather than asyncio's
                # "Task exception was never retrieved" at GC time.
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
                    # Lingering close, not a bare close: a peer still sending
                    # when we answered would otherwise RST away the response
                    # it was waiting for.
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
        """Wrap the transport and run one :class:`ConnectionActor`.

        *bound_binding* is set for port-bound non-ASGI protocols —
        the connection skips HTTP detection and is handed straight to the
        binding's raw handler.

        *transport* is passed explicitly by the buffered-protocol path, which
        has no `StreamWriter` to carry it.  Peer/socket names and the TLS
        object are read from it, so it is the one thing that path cannot infer.
        """
        from .conn_id import new_connection_id  # noqa: PLC0415
        from .connection_actor import ConnectionActor  # noqa: PLC0415
        from .sender import AsyncioWriter  # noqa: PLC0415
        from .recipient import AsyncioReader  # noqa: PLC0415

        if transport is None:
            transport = getattr(writer, 'transport', None)
        peername = transport.get_extra_info('peername') if transport else None
        sockname = transport.get_extra_info('sockname') if transport else None
        # AF_UNIX sockname is the path string; AF_INET[6] is a tuple.
        # ASGI 3.0 expects ``scope['server']`` to be an iterable of
        # ``(host, port)`` — encode UDS as ``(path, None)`` here so the
        # actor layer doesn't have to special-case.  Peername on a UDS
        # is typically an empty string; surface it as ``(path, None)``
        # for symmetry.
        if isinstance(sockname, str):
            sockname = (sockname, None)
        if isinstance(peername, str):
            peername = (peername or '', None)
        # AF_INET6 get_extra_info returns a 4-tuple
        # ``(host, port, flowinfo, scope_id)``; ASGI 3.0 §Connection Scope
        # requires ``scope['client']`` / ``scope['server']`` to be
        # ``(host, port)``.  Truncate so IPv6 peers get spec-compliant
        # 2-tuples instead of ``['::1', port, 0, 0]``.
        if isinstance(sockname, tuple) and len(sockname) > 2:
            sockname = sockname[:2]
        if isinstance(peername, tuple) and len(peername) > 2:
            peername = peername[:2]
        ssl_object = transport.get_extra_info('ssl_object') if transport else None
        ssl_flag = ssl_object is not None
        alpn = ssl_object.selected_alpn_protocol() if ssl_object else None

        # Defense against dead/stuck peers — moved off the hot accept path:
        #   SO_SNDBUF / SO_RCVBUF / TCP_USER_TIMEOUT are on the LISTENING
        #     socket and inherited (set once at open_socket time).
        #   Idle keep-alive ghosts are evicted by an app-level timer in
        #     HTTP1Actor (``BB_KEEP_ALIVE_TIMEOUT``, default 5 s) — the
        #     uvicorn / granian / Caddy pattern.
        # Net cost: 0 setsockopt syscalls per accept (was 6, then 4).

        wrapped_reader = (reader if isinstance(reader, AbstractReader)
                          else AsyncioReader(reader))
        if isinstance(writer, AbstractWriter):
            wrapped_writer = writer
        else:
            from ..env import get_settings as _get_settings  # noqa: PLC0415
            wrapped_writer = AsyncioWriter(
                writer, write_timeout=_get_settings().write_timeout)

        aggregator = self._cached_aggregator

        # max_connections == 0 disables the cap entirely (rely on OS fd
        # limits).  Otherwise, send a well-formed HTTP/1.1 503 +
        # Retry-After so load-balancers and health-checks can interpret
        # the response — better than a silent reset, which looks like a
        # crash from the LB's perspective.  For ALPN-negotiated h2 we
        # don't have the SETTINGS exchange to send GOAWAY cleanly, so a
        # straight close is the safest answer there.
        if self._max_connections and self._active_connections >= self._max_connections:
            logger.warning(
                'Connection limit reached (%d/%d) — 503 to %s',
                self._active_connections, self._max_connections, peername,
            )
            # ASGIServer-level cap fires before ConnectionActor binds a
            # CapHitCounter, so the contextvar is unset; this call emits
            # unconditionally.  An adversary cannot flood the log
            # because they cannot accept a connection past the cap to
            # begin with.
            log_cap_hit('max_connections',
                        requested=self._active_connections + 1,
                        limit=self._max_connections,
                        peer=peername, protocol='tcp')
            if bound_binding is None and alpn != 'h2':
                # HTTP/1.1 (or undetected cleartext — h1 is the safe
                # default since the client hasn't spoken yet).  Minimal
                # response: no body, content-length: 0, connection:
                # close.  Retry-After in seconds.  A port-bound non-ASGI
                # protocol gets a plain close — we don't know its framing.
                try:
                    await wrapped_writer.write(
                        b'HTTP/1.1 503 Service Unavailable\r\n'
                        b'retry-after: 1\r\n'
                        b'content-length: 0\r\n'
                        b'connection: close\r\n'
                        b'\r\n')
                except Exception:
                    # Peer may already be gone or transport broken; the
                    # close() below still runs.  No further action.
                    logger.debug(
                        '503 write failed for %s (peer disconnected?)',
                        peername)
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
        # When the master re-execs itself for an auto-reload, it hands
        # off the bound listening sockets via fd inheritance + the
        # BB_INHERIT_FDS env var.  Adopt them instead of binding so the
        # listener stays continuous across the reload (no port-release
        # race, no missed SYNs).
        raw_sockets = adopt_inherited_sockets()
        if raw_sockets:
            self.raw_sockets = raw_sockets
            sockname = raw_sockets[0].getsockname()
            # AF_UNIX getsockname() returns the bound path string; TCP
            # returns ``(host, port)``.  Store both so downstream callers
            # (workers, integration tests) don't have to special-case.
            if isinstance(sockname, str):
                self.port = None
                self.unix_path = sockname
            else:
                self.port = sockname[1]
                self.unix_path = None
            return

        from ..env import get_settings as _get_settings  # noqa: PLC0415
        _cfg = _get_settings()

        if inherited_fd is not None:
            # Systemd-style socket activation.  Bind / listen already
            # happened in the supervisor; just adopt the fd and capture
            # its sockname for ``port`` / ``unix_path`` so downstream
            # code paths (lifespan, workers) see the same shape as
            # a normal bind.
            sock = adopt_listening_fd(inherited_fd)
            self.raw_sockets = [sock]
            sockname = sock.getsockname()
            if isinstance(sockname, str):
                self.port = None
                self.unix_path = sockname
            else:
                # AF_INET sockname is ``(host, port)``; AF_INET6 is
                # ``(host, port, flowinfo, scopeid)`` — port is always [1].
                self.port = sockname[1]
                self.unix_path = None
            return

        if unix_path is not None:
            # AF_UNIX path: no port check, no dual-stack pairing, no TCP
            # sockopts.  ``create_unix_socket`` handles stale-file cleanup
            # and chmod for the typical reverse-proxy-runs-in-same-group
            # deployment.
            sock = create_unix_socket(
                unix_path,
                backlog=_cfg.socket_backlog,
                sndbuf=_cfg.socket_sndbuf,
                rcvbuf=_cfg.socket_rcvbuf,
            )
            if sock is None:
                raise RuntimeError(
                    f'Failed to bind AF_UNIX socket on {unix_path!r}.'
                )
            self.raw_sockets = [sock]
            self.port = None
            self.unix_path = unix_path
            return

        raw_sockets = create_dual_stack_sockets(
            port,
            backlog=_cfg.socket_backlog,
            sndbuf=_cfg.socket_sndbuf,
            rcvbuf=_cfg.socket_rcvbuf,
            user_timeout_ms=_cfg.tcp_user_timeout_ms,
            keepalive=False,  # replaced by app-level keep_alive_timeout
            # Honour BB_SOCKET_REUSEPORT on the HTTP listener so forked
            # workers can co-bind the same port and the kernel load-balances
            # accepts across them.  (Stateful protocol ports below are bound
            # WITHOUT reuseport on purpose — they must have a single owner.)
            reuseport=_cfg.socket_reuseport,
        )

        if not raw_sockets:
            # No connect-probe pre-check (that shape was racy, IPv4-localhost
            # only, and hid the OS error): binding is the check.  The specific
            # OS failure (e.g. EADDRINUSE naming the address) was already
            # logged by _bind_socket.
            logger.error(f'Failed to bind port {port}. Try another port.')
            raise RuntimeError(
                f'Failed to bind port {port} (see log for the OS error, '
                f'e.g. address already in use). Try another port.')

        self.raw_sockets = raw_sockets

        # Derive the actual port from the first successfully bound socket
        # (matters when port=0 was requested, i.e. the OS picks a free port).
        self.port = self.raw_sockets[0].getsockname()[1]
        self.unix_path = None

        # Do NOT wrap sockets with ssl_context here.
        # asyncio.start_server() accepts raw TCP sockets via sockets= and
        # handles the TLS handshake itself when ssl= is also provided.
        # Pre-wrapping with ssl_context.wrap_socket() causes a double-TLS
        # layer and breaks the handshake.

        self._bind_protocol_sockets(_cfg)

    def _bind_protocol_sockets(self, _cfg):
        """Bind a listening socket per port-bound non-ASGI protocol.

        Each :class:`RawBinding` registered with a ``port`` gets its own
        dual-stack socket set.  ``port=0`` lets the OS pick a free port (used by
        tests); the bound port is recorded in :attr:`protocol_ports`.  Sockets
        are bound bare here; :meth:`run` layers TLS onto the listeners whose
        binding set ``tls=True``, cleartext otherwise.
        """
        # Iterate the bindings themselves, not the port-keyed view — several
        # bindings may all ask for port=0 (OS-assigned, common in tests), and
        # keying by port would silently collapse them to one listener.
        for binding in self._protocol_registry.raw_bindings.values():
            if binding.port is None:
                continue
            port = binding.port
            socks = create_dual_stack_sockets(
                port,
                backlog=_cfg.socket_backlog,
                sndbuf=_cfg.socket_sndbuf,
                rcvbuf=_cfg.socket_rcvbuf,
                user_timeout_ms=_cfg.tcp_user_timeout_ms,
                keepalive=False,
            )
            if not socks:
                logger.error('Failed to bind %s on port %d.', binding.name, port)
                continue
            bound_port = socks[0].getsockname()[1]
            self.protocol_ports[binding.name] = bound_port
            self._protocol_sockets.append((socks, binding))
            logger.info('Protocol %r listening on port %d', binding.name, bound_port)

    def close_socket(self):
        for s in getattr(self, 'raw_sockets', []):
            s.close()
        for socks, _ in self._protocol_sockets:
            for s in socks:
                s.close()

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
        """Drive the ASGI lifespan shutdown handshake."""
        await self._lifespan_cm.__aexit__(None, None, None)

    async def run(self, port=80):
        """Run an asyncio socket server with the setting in this object."""
        if not hasattr(self, 'raw_sockets') or not self.raw_sockets:
            self.open_socket(port)

        # SocketManager wraps each socket in asyncio.start_server and closes all
        # servers on exit.  The shared HTTP listener uses client_connected_cb;
        # each port-bound non-ASGI protocol uses its own raw callback.  Raw
        # sockets are cleartext unless the binding was registered with
        # ``tls=True``, which serves them through the same TLS
        # machinery as the HTTPS listener.
        # LifespanManager drives the ASGI lifespan protocol; nesting it inside
        # SocketManager guarantees: startup completes before serve_forever() is
        # called, and shutdown completes before sockets are closed.
        pairs = [(s, self.connection_protocol_factory())
                 for s in self.raw_sockets]
        raw_clear = [(s, self.connection_protocol_factory(binding))
                     for socks, binding in self._protocol_sockets
                     if not binding.tls for s in socks]
        raw_tls = [(s, self.connection_protocol_factory(binding))
                   for socks, binding in self._protocol_sockets
                   if binding.tls for s in socks]
        if raw_tls and self.ssl_context is None:
            names = ', '.join(binding.name
                              for _socks, binding in self._protocol_sockets
                              if binding.tls)
            raise RuntimeError(
                f'Raw protocol binding(s) [{names}] require TLS (tls=True) '
                f'but the server has no certificate configured — pass '
                f'certfile/keyfile or an ssl_context.')
        async with SocketManager(pairs, self.ssl_context) as servers, \
                SocketManager(raw_clear, None) as raw_servers, \
                SocketManager(raw_tls,
                              self._raw_tls_context() if raw_tls else None
                              ) as raw_tls_servers:
            servers = servers + raw_servers + raw_tls_servers
            self._running_servers = servers
            async with LifespanManager(self.app):
                logger.info(f'Server(s) created: {servers}')
                try:
                    async with asyncio.TaskGroup() as tg:
                        for srv in servers:
                            tg.create_task(srv.serve_forever())

                except* KeyboardInterrupt:
                    logger.info('KeyboardInterrupt received — shutting down.')

                except* asyncio.CancelledError:
                    logger.info('Server task cancelled.')

                except* Exception as eg:
                    logger.error('Server error: %s', eg)

        logger.info('Server has been stopped.')

    async def stop(self, drain_timeout: float = 8.0) -> None:
        """Stop accepting, then let the connections already being served finish.

        What the loop owes work still on it when the process is asked to stop:
        a request that was accepted gets to produce its response, and a
        connection that was not yet accepted never becomes one.  Nothing is
        cancelled while the budget lasts — a cancelled handler is a client
        holding a half-written response, which is worse than the wait.

        *drain_timeout* must sit **inside** the supervisor's own wait
        (``MultiWorkerServer.shutdown_timeout``), so the drain ends here rather
        than in a SIGKILL.  Whatever has not finished by then is cancelled,
        because a shutdown that must complete still completes.
        """
        if self._stopping:
            return
        self._stopping = True

        # Close the listeners first, so the drain below is over a set that can
        # only shrink.  ``close()`` is synchronous and stops accept(); the
        # sockets are released when the SocketManager unwinds.
        for srv in getattr(self, '_running_servers', ()):
            srv.close()

        pending = [t for t in self._connection_tasks if not t.done()]
        if pending:
            logger.info('Draining %d connection(s), up to %.1fs',
                        len(pending), drain_timeout)
            _done, still = await asyncio.wait(pending, timeout=drain_timeout)
            if still:
                logger.warning(
                    '%d connection(s) did not finish within %.1fs — cancelling',
                    len(still), drain_timeout)
                for task in still:
                    task.cancel()
                await asyncio.gather(*still, return_exceptions=True)

        for srv in getattr(self, '_running_servers', ()):
            with contextlib.suppress(Exception):
                await srv.wait_closed()

    def wait_for_port(self, timeout: float = 10.0, poll_interval: float = 0.1):
        if self.port is None:
            raise RuntimeError("Server port is not set")

        # Connect via IPv4 127.0.0.1 (the same path external clients such as
        # nginx use) and send a minimal HTTP request so that the check only
        # succeeds once the child process's asyncio event loop has actually
        # accepted the connection and is processing data — not merely because
        # the OS-level listen socket was set up in the parent before fork.
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
                # TLS server accepted the TCP connection then closed it because
                # we sent a plain HTTP request — the asyncio loop is live.
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


# Backward-compat alias: the class was named ``ASGIServer`` before it gained
# non-ASGI (raw protocol) listeners.  Existing imports
# (``from blackbull.server import ASGIServer``) keep working.
ASGIServer = Server
