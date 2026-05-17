import asyncio

from http import HTTPStatus
import logging
import ssl
import traceback
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path
import time
import socket

# private library
from ..utils import HTTP2, pop_safe, check_port
from ..event import Event
from ..protocol.rsock import create_dual_stack_sockets
from ..protocol.frame import FrameFactory
from ..protocol.frame_types import ErrorCodes, FrameTypes, FrameBase
from .response import ResponderFactory
from .parser import ParserFactory
from .sender import SenderFactory, AbstractWriter
from .recipient import (RecipientFactory, HTTP2Recipient, AbstractReader, AsyncioReader,
                        IncompleteReadError, _HTTP2_STREAM_QUEUE_DEPTH, _WS_EVENT_QUEUE_DEPTH)
from .access_log import AccessLogRecord, _make_capturing_send
from .constants import ASGIEvent
from .headers import Headers
from .http2_actor import HTTP2Actor
logger = logging.getLogger(__name__)
_access_logger = logging.getLogger('blackbull.access')

async def _run_with_log(coro, record: AccessLogRecord,
                        dispatcher=None, scope: dict | None = None) -> None:
    """Await *coro* then emit one access log entry on 'blackbull.access'.

    CancelledError re-raised so TaskGroup cancellation propagates correctly.
    All other exceptions are logged and swallowed so a failing stream does not
    cancel sibling streams through the TaskGroup.
    """
    try:
        if dispatcher is not None and scope is not None and scope.get('type') == 'http':
            await dispatcher.emit(Event(
                'request_received',
                detail={
                    'scope':        scope,
                    'client_ip':    record.client_ip,
                    'method':       record.method,
                    'path':         record.path,
                    'http_version': record.http_version,
                    'headers':      scope.get('headers', []),
                },
            ))
        await coro
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception('HTTP/2 stream raised an unhandled exception')
    finally:
        _access_logger.info(record.format(), extra=record.as_extra())
        if (dispatcher is not None and scope is not None
                and scope.get('type') == 'http'
                and not scope.get('_disconnected')):
            await dispatcher.emit(Event(
                'request_completed',
                detail={
                    'scope': scope,
                    'client_ip': record.client_ip,
                    'method': record.method,
                    'path': record.path,
                    'http_version': record.http_version,
                    'status': record.status,
                    'response_bytes': record.response_bytes,
                    'duration_ms': record.duration_ms(),
                },
            ))



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
        event = await self._send_q.get()
        if event.get('type') == ASGIEvent.LIFESPAN_STARTUP_FAILED:
            raise RuntimeError(event.get('message', 'Lifespan startup failed'))
        return self

    async def __aexit__(self, *_):
        await self._receive_q.put({'type': ASGIEvent.LIFESPAN_SHUTDOWN})
        await self._send_q.get()   # lifespan.shutdown.complete
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task   # drain finally blocks inside the lifespan app
            except asyncio.CancelledError:
                pass
        return False


@asynccontextmanager
async def SocketManager(cb, raw_sockets, ssl_context):
    """Async context manager that creates asyncio servers from already-bound sockets.

    On enter: wraps each raw socket in asyncio.start_server and yields the list.
    On exit: closes all asyncio servers.
    """
    servers = []
    for sock in raw_sockets:
        srv = await asyncio.start_server(cb, sock=sock, ssl=ssl_context)
        servers.append(srv)
    try:
        yield servers
    finally:
        for srv in servers:
            srv.close()


class ASGIServer:
    """ An asyncio socket server with which reads first several bytes and dispatches
    transactions to HTTP2/HTTP1.1/WebSocket server.
    When ssl_context or certfile is set, this server runs as a HTTPS server.
    """
    def __init__(self, app, *,
                 ssl_context=None, certfile=None, keyfile=None, password=None,
                 max_connections: int = 500,
                 stream_queue_depth: int = _HTTP2_STREAM_QUEUE_DEPTH,
                 ws_queue_depth: int = _WS_EVENT_QUEUE_DEPTH,
                 **kwds):
        self.app = app
        self._max_connections = max_connections
        self._stream_queue_depth = stream_queue_depth
        self._ws_queue_depth = ws_queue_depth
        self._active_connections = 0

        # Create TLS context
        if ssl_context and (certfile or keyfile):
            raise TypeError('SSLContext and certfile (or keyfile) must not be set at the same time')

        self.ssl_context = ssl_context
        self.keyfile = keyfile
        self.certfile = certfile
        self.make_ssl_context()
        self.socket = None
        self.port = None

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

    async def client_connected_cb(self, reader, writer):
        """Called when the server receives a new TCP connection."""
        from .connection_actor import ConnectionActor  # noqa: PLC0415
        from ..event_aggregator import EventAggregator  # noqa: PLC0415
        from .sender import AsyncioWriter  # noqa: PLC0415
        from .recipient import AsyncioReader  # noqa: PLC0415

        transport = getattr(writer, 'transport', None)
        peername = transport.get_extra_info('peername') if transport else None
        sockname = transport.get_extra_info('sockname') if transport else None
        ssl_flag = (transport.get_extra_info('ssl_object') is not None
                    if transport else False)

        sock = transport.get_extra_info('socket') if transport else None
        if sock is not None:
            import socket as _socket  # noqa: PLC0415
            from ..env import get_settings as _get_settings  # noqa: PLC0415
            _cfg = _get_settings()
            # TCP keep-alive: evict ghost connections after ~80 s of silence
            # (60 s idle + 3 × 10 s probes) without an application-layer timeout.
            sock.setsockopt(_socket.SOL_SOCKET, _socket.SO_KEEPALIVE, 1)
            if hasattr(_socket, 'TCP_KEEPIDLE'):   # Linux
                sock.setsockopt(_socket.IPPROTO_TCP, _socket.TCP_KEEPIDLE, 60)
            if hasattr(_socket, 'TCP_KEEPINTVL'):
                sock.setsockopt(_socket.IPPROTO_TCP, _socket.TCP_KEEPINTVL, 10)
            if hasattr(_socket, 'TCP_KEEPCNT'):
                sock.setsockopt(_socket.IPPROTO_TCP, _socket.TCP_KEEPCNT, 3)
            # Enlarge kernel send/receive buffers (configurable via BB_SOCKET_SNDBUF/RCVBUF).
            # The kernel doubles the requested value; 0 leaves the default unchanged.
            if _cfg.socket_sndbuf:
                sock.setsockopt(_socket.SOL_SOCKET, _socket.SO_SNDBUF, _cfg.socket_sndbuf)
            if _cfg.socket_rcvbuf:
                sock.setsockopt(_socket.SOL_SOCKET, _socket.SO_RCVBUF, _cfg.socket_rcvbuf)

        wrapped_reader = (reader if isinstance(reader, AbstractReader)
                          else AsyncioReader(reader))
        wrapped_writer = (writer if isinstance(writer, AbstractWriter)
                          else AsyncioWriter(writer))

        dispatcher = getattr(self.app, '_dispatcher', None)
        aggregator = EventAggregator(dispatcher) if dispatcher is not None else None

        if self._active_connections >= self._max_connections:
            logger.warning(
                'Connection limit reached (%d/%d) — rejecting %s',
                self._active_connections, self._max_connections, peername,
            )
            await wrapped_writer.close()
            return

        self._active_connections += 1
        try:
            actor = ConnectionActor(
                wrapped_reader, wrapped_writer, self.app, aggregator,
                peername=peername, sockname=sockname, ssl=ssl_flag,
                stream_queue_depth=self._stream_queue_depth,
                ws_queue_depth=self._ws_queue_depth,
            )
            await actor.run()
        finally:
            self._active_connections -= 1

    def open_socket(self, port=0):
        if not check_port(port=port):
            logger.error(f'Port ({port}) is not available. Try another port.')
            raise RuntimeError(f'Port ({port}) is not available. Try another port.')

        from ..env import get_settings as _get_settings  # noqa: PLC0415
        raw_sockets = create_dual_stack_sockets(port, backlog=_get_settings().socket_backlog)

        if not raw_sockets:
            logger.error(f'Failed to open port ({port}). Try another port.')
            return

        self.raw_sockets = raw_sockets

        # Derive the actual port from the first successfully bound socket
        # (matters when port=0 was requested, i.e. the OS picks a free port).
        self.port = self.raw_sockets[0].getsockname()[1]

        # Do NOT wrap sockets with ssl_context here.
        # asyncio.start_server() accepts raw TCP sockets via sockets= and
        # handles the TLS handshake itself when ssl= is also provided.
        # Pre-wrapping with ssl_context.wrap_socket() causes a double-TLS
        # layer and breaks the handshake.

    def close_socket(self):
        for s in getattr(self, 'raw_sockets', []):
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

        # SocketManager wraps each raw socket in asyncio.start_server and
        # closes all servers on exit.  LifespanManager drives the ASGI
        # lifespan protocol; nesting it inside SocketManager guarantees:
        #   startup completes before serve_forever() is called, and
        #   shutdown completes before sockets are closed.
        async with SocketManager(self.client_connected_cb,
                                 self.raw_sockets, self.ssl_context) as servers:
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
        logger.info('ASGIServer.close() is called.')
        logger.info(self.__dict__)
        self.close_socket()
