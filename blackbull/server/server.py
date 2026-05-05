import asyncio
from dataclasses import dataclass, field
from http import HTTPStatus
import logging
import ssl
import traceback
import re
import uuid
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from urllib.parse import urlparse
from hashlib import sha1
from base64 import b64encode
from pathlib import Path
import time
import socket

# private library
from ..utils import HTTP2, pop_safe, check_port
from ..event import Event
from ..protocol.stream import Stream, StreamState
from ..protocol.rsock import create_dual_stack_sockets
from ..protocol.frame import ErrorCodes, FrameFactory, FrameTypes, FrameBase, parse_priority_field, PseudoHeaders
from ..logger import log
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



class BaseHandler:
    """
    The common roles of HTTPServers are
    * Parsing received binary
    * Wrapping sending binary
    """
    def __init__(self, app, reader, writer, *args, **kwargs):
        """docstring for BaseHandler
        Parameters
        ----------
        app:
            An ASGI application that handles the scope (when app is called for the first time).
            Then the app receives and send (when it is called for the second time)
        reader:
            An reader object that receives TCP/IP sockets.
        writer:
            An writer object that sends TCP/IP sockets.
        """
        self.app = app
        self.reader = reader
        self.writer = writer

    def parse(self, data):
        """
        Parse data and make a frame.
        or
        Parse received request and make ASGI scope object.

        which is consistent for this application?

        """
        logger.error('Not implemented.')
        raise NotImplementedError()

    async def run(self):
        logger.error('Not implemented.')
        raise NotImplementedError()


class WebSocketHandler(BaseHandler):
    """Server-side WebSocket handler.

    Responsibilities
    ----------------
    1. Complete the HTTP→WebSocket upgrade handshake (RFC 6455 §4.2.2).
    2. Expose an ASGI-compatible ``receive`` coroutine that reads raw WebSocket
       frames from the TCP stream and returns ASGI event dicts.
    3. Delegate all frame encoding/writing to ``WebSocketSender``.
    4. Call the application coroutine with the original HTTP-upgrade scope.
    """

    ACCEPT_VERSION = b'13'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # args: (app, reader, writer, scope)
        if len(args) > 3:
            self.scope = args[-1]
            logger.debug(self.scope)
            if not isinstance(self.scope['headers'], Headers):
                self.scope['headers'] = Headers(self.scope['headers'])
            logger.debug(self.scope)

    async def run(self, *, record: 'AccessLogRecord | None' = None):
        """Complete the upgrade handshake then call the ASGI application."""
        send = SenderFactory.http1(self.writer)

        # --- RFC 6455 §4.2.2: server handshake response ---
        key = [x[1] for x in self.scope['headers'] if x[0] == b'sec-websocket-key'][0]
        logger.debug('WebSocket key: %s', key)
        accept = b64encode(sha1(key + b'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').digest())

        version = self.scope['headers'].get(b'sec-websocket-version')
        if version != self.ACCEPT_VERSION:
            await send(b'', HTTPStatus.BAD_REQUEST, [(b'sec-websocket-version', self.ACCEPT_VERSION)])
            return

        # Subprotocol negotiation: pick the first client-offered protocol that
        # the server supports.  Falls back to None (no protocol) when the app
        # exposes no registry or when none of the client's offers match.
        subprotocol: bytes | None = None
        raw = self.scope['headers'].get(b'sec-websocket-protocol', b'')
        if raw:
            client_protos = [p.strip() for p in raw.split(b',')]
            available = set(getattr(self.app, 'available_ws_protocols', []))
            subprotocol = next((p for p in client_protos if p in available), None)

        handshake_headers = Headers([
            (b'upgrade', b'websocket'),
            (b'connection', b'upgrade'),
            (b'sec-websocket-accept', accept),
        ])
        if subprotocol:
            handshake_headers.append(b'sec-websocket-protocol', subprotocol)

        await send(b'', HTTPStatus.SWITCHING_PROTOCOLS, handshake_headers)
        logger.debug('WebSocket handshake complete.')

        send = SenderFactory.websocket(self.writer)
        _dispatcher = getattr(self.app, '_dispatcher', None)
        if _dispatcher is not None:
            send = _make_ws_accept_detecting_send(send, self.scope, _dispatcher)
        receive = RecipientFactory.websocket(
            self.reader, self.writer,
            dispatcher=_dispatcher,
            scope=self.scope,
        )
        if record is not None:
            receive = _make_capturing_receive(receive, record)
        await self.app(self.scope, receive, send)


class HTTP11Handler(BaseHandler):
    """Thin wrapper — delegates the keep-alive loop to HTTP1Actor.

    ``parse()``, ``_fill_connection_info()``, and ``has_request()`` are kept
    for callers that use them directly (e.g. test helpers and parsers).
    """

    @log
    def __init__(self, app, reader, writer, request: bytes, *args, **kwargs):
        if not isinstance(reader, AbstractReader):
            reader = AsyncioReader(reader)
        super().__init__(app, reader, writer, *args, **kwargs)
        self.request = request

    def has_request(self):
        return self.request is not None

    async def run(self):
        from .http1_actor import HTTP1Actor  # noqa: PLC0415 — lazy to avoid circular dep
        from .sender import AsyncioWriter   # noqa: PLC0415
        transport = self.writer.transport

        peername = transport.get_extra_info('peername') if transport else None
        sockname = transport.get_extra_info('sockname') if transport else None
        ssl = transport.get_extra_info('ssl_object') is not None if transport else False

        actor = HTTP1Actor(
            self.reader, AsyncioWriter(self.writer), self.app,
            aggregator=None,  # use legacy direct-dispatcher path
            request=self.request,
            peername=peername,
            sockname=sockname,
            ssl=ssl,
        )
        await actor.run()
    
    def _fill_connection_info(self, scope: dict) -> None:
        """Populate scope['client'], scope['server'], and scope['scheme'] from the transport."""
        transport = self.writer.transport
        if peername := transport.get_extra_info('peername'):
            scope['client'] = list(peername[:2])
        if (sockname := transport.get_extra_info('sockname')) and scope['server'] is None:
            scope['server'] = list(sockname[:2])
        if transport.get_extra_info('ssl_object') is not None:
            scope['scheme'] = 'wss' if scope.get('type') == 'websocket' else 'https'

    def parse(self, data=None):
        """
        Parse header lines of received request and make ASGI scope object.
        """
        lines = self.request.split(b'\r\n') # Lines are bytes, not str, because the request is read as bytes from the stream.

        method, path, version = lines[0].split(b' ')
        path_parsed = urlparse(path)

        raw: list[tuple[bytes, bytes]] = []
        for line in lines[1:]:
            line = line.strip()
            if b':' in line:
                key, value = line.split(b':', 1)
                raw.append((key.lower(), value.strip()))
        headers = Headers(raw)
        logger.debug(headers)

        scope = {
            'type': 'http',
            'asgi': {'version': '3.0', 'spec_version': '2.0'},
            'http_version': re.sub(r'HTTP/(.*)', r'\1', version.decode('utf-8')),
            'method': method.decode('utf-8'),
            'scheme': 'http',  # upgraded to 'https'/'wss' in run() when TLS
            'path': path_parsed.path.decode('utf-8'),
            'raw_path': path,
            'query_string': path_parsed.query,
            'root_path': headers.get(b'X-Forwarded-Prefix', b'').decode('utf-8'),
            'headers': headers,
            'client': None,  # set in run() from transport peername
            'server': None,  # set from Host header; fallback to sockname in run()
            'state': {},
        }

        if headers.getlist(b'host'):
            host, port = headers.get(b'host').split(b':')
            scope['server'] = [host.decode('utf-8'), int(port)]

        if headers.getlist(b'upgrade'):
            scope['type'] = headers.get(b'upgrade').decode('utf-8').lower()
            if scope['type'] == 'websocket':
                scope['scheme'] = 'ws'
        elif headers.getlist(b'connection'):
            scope['scheme'] = headers.get(b'connection').decode('utf-8').lower()

        return scope

    def make_sender(self):
        return SenderFactory.http1(self.writer)


class HTTP2Handler(HTTP2Actor):
    """Thin wrapper — delegates to HTTP2Actor. Preserved for backward compatibility."""

    @log
    def __init__(self, app, reader, writer):
        transport = getattr(writer, 'transport', None)
        peername = transport.get_extra_info('peername') if transport else None
        sockname = transport.get_extra_info('sockname') if transport else None
        ssl_flag = transport.get_extra_info('ssl_object') is not None if transport else False

        if reader is not None and not isinstance(reader, AbstractReader):
            reader = AsyncioReader(reader)

        from .sender import AsyncioWriter  # noqa: PLC0415
        wrapped_writer = writer if isinstance(writer, AbstractWriter) else AsyncioWriter(writer)

        super().__init__(
            reader, wrapped_writer, app, aggregator=None,
            peername=peername, sockname=sockname, ssl=ssl_flag,
        )
        # Keep the raw writer accessible for legacy callers that call .close() etc.
        self.writer = writer


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
        """
        This function is called when the server receives an access request from a client.
        Handler must handles every exception in it and not raise any exception.
        """
        request_data = await reader.readuntil(b'\r\n')

        try:
            if request_data == HTTP2[:-8]:
                request_data += await reader.read(8)

                if request_data == HTTP2:
                    logger.info('HTTP/2 connection is requested.')
                    handler = HTTP2Handler(self.app, reader, writer)
                else:
                    raise ValueError(f'Received an invalid request ({request_data}).')

            else:
                logger.info('HTTP1.1 connection is requested.')
                handler = HTTP11Handler(self.app, reader, writer, request_data)

            await handler.run()

        except Exception:
            logger.error(traceback.format_exc())

        finally:
            writer.close()

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
