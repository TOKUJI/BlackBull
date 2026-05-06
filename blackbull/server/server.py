import asyncio
from dataclasses import dataclass, field
from http import HTTPStatus
import logging
import ssl
import traceback
import uuid
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path
import time
import socket

# private library
from ..utils import HTTP2, pop_safe, check_port
from ..event import Event
from ..protocol.stream import Stream, StreamState
from ..protocol.rsock import create_dual_stack_sockets
from ..protocol.frame import ErrorCodes, FrameFactory, FrameTypes, FrameBase, parse_priority_field, PseudoHeaders
from .response import ResponderFactory
from .parser import ParserFactory
from .sender import SenderFactory, AbstractWriter
from .recipient import RecipientFactory, HTTP2Recipient, AbstractReader, AsyncioReader, IncompleteReadError
from .headers import Headers
from .http2_actor import HTTP2Actor
logger = logging.getLogger(__name__)
_access_logger = logging.getLogger('blackbull.access')

_DEFAULT_PRIORITY: dict[str, int | bool] = {'urgency': 3, 'incremental': False}


def _resolve_priority(stream: 'Stream', scope: dict) -> dict[str, int | bool]:
    """Return the HTTP/2 priority hint for a newly-built scope.

    Precedence (highest first):
    1. A PRIORITY_UPDATE frame received before HEADERS (stored on stream).
    2. The ``priority`` HTTP header sent in the request.
    3. RFC 9218 §4.1 defaults (urgency=3, incremental=False).
    """
    if stream.priority_hint is not None:
        return stream.priority_hint
    raw = scope.get('headers', Headers([])).get(b'priority', b'')
    if raw:
        return parse_priority_field(raw.decode('ascii', errors='replace'))
    return dict(_DEFAULT_PRIORITY)


# ---------------------------------------------------------------------------
# Access logging
# ---------------------------------------------------------------------------

@dataclass
class AccessLogRecord:
    """Per-request record populated in two phases.

    Phase 1 (after parse): client_ip, method, path, http_version.
    Phase 2 (during send): status, response_bytes.
    For WebSocket sessions, close_code is captured on disconnect instead.
    Emitted as one INFO line on 'blackbull.access' after the response completes.
    """
    client_ip:      str
    method:         str
    path:           str
    http_version:   str
    status:         int | str = '-'
    response_bytes: int       = 0
    close_code:     int | None = None
    _started_at:    float     = field(default_factory=time.monotonic, repr=False)

    @classmethod
    def from_scope(cls, scope: dict) -> 'AccessLogRecord':
        client = scope.get('client') or ['-']
        return cls(
            client_ip    = str(client[0]),
            method       = scope.get('method', '-'),
            path         = scope.get('path', '-'),
            http_version = scope.get('http_version', '-'),
        )

    def duration_ms(self) -> float:
        return (time.monotonic() - self._started_at) * 1000

    def format(self) -> str:
        if self.close_code is not None:
            return (f'{self.client_ip} '
                    f'"{self.method} {self.path} WS/{self.http_version}" '
                    f'101 close={self.close_code} '
                    f'{self.duration_ms():.0f}ms')
        return (f'{self.client_ip} '
                f'"{self.method} {self.path} HTTP/{self.http_version}" '
                f'{self.status} {self.response_bytes} '
                f'{self.duration_ms():.0f}ms')

    def as_extra(self) -> dict:
        d: dict = {
            'client_ip':      self.client_ip,
            'method':         self.method,
            'path':           self.path,
            'http_version':   self.http_version,
            'status':         self.status,
            'response_bytes': self.response_bytes,
            'duration_ms':    self.duration_ms(),
        }
        if self.close_code is not None:
            d['close_code'] = self.close_code
        return d


def _make_capturing_send(send, record: AccessLogRecord):
    """Wrap *send* to update *record* with status and response size as events flow through."""
    async def capturing_send(event, *args, **kwargs):
        if isinstance(event, dict):
            if event.get('type') == 'http.response.start':
                record.status = event.get('status', '-')
            elif event.get('type') == 'http.response.body':
                record.response_bytes += len(event.get('body', b''))
        await send(event, *args, **kwargs)
    return capturing_send


def _make_capturing_receive(receive, record: AccessLogRecord):
    """Wrap *receive* to capture the WebSocket close code on disconnect."""
    async def capturing_receive():
        event = await receive()
        if isinstance(event, dict) and event.get('type') == 'websocket.disconnect':
            record.close_code = event.get('code', 1000)
        return event
    return capturing_receive


def _make_disconnect_detecting_receive(receive, scope: dict, dispatcher, log_record: AccessLogRecord):
    """Wrap *receive* to emit request_disconnected when http.disconnect is seen.

    Sets scope['_disconnected'] = True on first detection so that the
    request_completed emit site can skip firing the completion event.
    The flag is idempotent — if the handler calls receive() again after the
    first disconnect, the event is not emitted twice.
    """
    async def detecting_receive():
        event = await receive()
        if isinstance(event, dict) and event.get('type') == 'http.disconnect':
            if not scope.get('_disconnected'):
                scope['_disconnected'] = True
                await dispatcher.emit(Event(
                    'request_disconnected',
                    detail={
                        'scope': scope,
                        'client_ip': log_record.client_ip,
                        'method': log_record.method,
                        'path': log_record.path,
                        'http_version': log_record.http_version,
                    },
                ))
        return event
    return detecting_receive


def _make_ws_accept_detecting_send(send, scope: dict, dispatcher):
    """Wrap WebSocket *send* to emit websocket_connected on websocket.accept.

    Sets scope['_connection_id'] on first accept so subsequent events
    (websocket_message, websocket_disconnected) can reference the same ID.
    Emits after calling the original send so the handshake is fully processed
    before observers are notified.
    """
    async def detecting_send(event, *args, **kwargs):
        await send(event, *args, **kwargs)
        if (isinstance(event, dict)
                and event.get('type') == 'websocket.accept'
                and not scope.get('_connection_id')):
            connection_id = str(uuid.uuid4())
            scope['_connection_id'] = connection_id
            await dispatcher.emit(Event(
                'websocket_connected',
                detail={
                    'scope': scope,
                    'connection_id': connection_id,
                    'client_ip': scope['client'][0] if scope.get('client') else '',
                    'path': scope.get('path', ''),
                    'subprotocol': event.get('subprotocol') or None,
                },
            ))
    return detecting_send


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
        await self._receive_q.put({'type': 'lifespan.startup'})
        event = await self._send_q.get()
        if event.get('type') == 'lifespan.startup.failed':
            raise RuntimeError(event.get('message', 'Lifespan startup failed'))
        return self

    async def __aexit__(self, *_):
        await self._receive_q.put({'type': 'lifespan.shutdown'})
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
                 ssl_context=None, certfile=None, keyfile=None, password=None, **kwds):
        self.app = app

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

        wrapped_reader = (reader if isinstance(reader, AbstractReader)
                          else AsyncioReader(reader))
        wrapped_writer = (writer if isinstance(writer, AbstractWriter)
                          else AsyncioWriter(writer))

        dispatcher = getattr(self.app, '_dispatcher', None)
        aggregator = EventAggregator(dispatcher) if dispatcher is not None else None

        actor = ConnectionActor(
            wrapped_reader, wrapped_writer, self.app, aggregator,
            peername=peername, sockname=sockname, ssl=ssl_flag,
        )
        await actor.run()

    def open_socket(self, port=0):
        if not check_port(port=port):
            logger.error(f'Port ({port}) is not available. Try another port.')
            raise RuntimeError(f'Port ({port}) is not available. Try another port.')

        raw_sockets = create_dual_stack_sockets(port)

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

        deadline = time.time() + timeout
        while True:
            try:
                with socket.create_connection(('::1', self.port), timeout=1):
                    return True
            except OSError:
                if time.time() >= deadline:
                    raise TimeoutError(
                        f"Port {self.port} on ::1 did not open within {timeout} seconds"
                    )
                time.sleep(poll_interval)

    def close(self):
        logger.info('ASGIServer.close() is called.')
        logger.info(self.__dict__)
        self.close_socket()
