import asyncio
import ssl
import traceback
import re
from collections import defaultdict, deque
from urllib.parse import urlparse
from hashlib import sha1
from base64 import b64encode
from pathlib import Path
import time
import socket

# private library
from ..utils import HTTP2, pop_safe, EventEmitter, check_port
from ..stream import Stream
from ..rsock import create_dual_stack_sockets
from ..frame import FrameFactory, FrameTypes, FrameBase, HeadersFlags, DataFlags, SettingFlags
from ..logger import get_logger_set
from .response import RespondFactory
from .parser import ParserFactory
logger, log = get_logger_set(__name__)


def parse(request):
    """
    Parse received request and make ASGI scope object.
    """
    lines = request.decode('utf-8').split('\r\n')

    method, path, version = lines[0].split(' ')

    mapping = {}
    for line in lines[1:]:
        if m := re.match(r'(.*?): (.*)', line):
            mapping[m[1]] = m[2]

    logger.info(mapping)

    scope = {
        'type': 'http',
        'http_version': re.sub(r'HTTP/(.*)', r'\1', version),
        'method': method,
        'path': path,
        'scheme': 'http',
        'headers': [
            # (b'host', mapping['Host'].encode()),
            # (b'accept', b'*/*'),
            # (b'accept-encoding', b'gzip, deflate'),
            # (b'connection', b'keep-alive'),
            # (b'user-agent', b'python-httpx/0.16.1'),
            # (b'key', b'value')
            ],
        'asgi': {'version': '3.0'}
    }

    if 'Host' in mapping:
        host, port = mapping['Host'].split(':')
        port = int(port)
        scope['server'] = [host, port]

    if 'Upgrade' in mapping:
        scope['type'] = mapping['Upgrade']
        if scope['type'] == 'websocket':
            scope['scheme'] = 'ws'

    # Note: 'Connection: Upgrade' must NOT overwrite the scheme; the Upgrade
    # header block above already set the correct scheme (e.g. 'ws').
    # Only set scheme from Connection when there is no Upgrade header.
    if 'Connection' in mapping and 'Upgrade' not in mapping:
        scope['scheme'] = mapping['Connection']

    for line in lines[1:]:
        scope['headers'].append(
            tuple(x.encode() for x in line.split(': '))
            )

    return scope


class HTTPServerBase:
    """
    The common roles of HTTPServers are
    * Parsing received binary
    * Wrapping sending binary
    """
    def __init__(self, app, reader, writer, *args, **kwargs):
        """docstring for HTTPServerBase
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


class WebsocketHandler(HTTPServerBase):
    """Server-side WebSocket handler.

    Responsibilities
    ----------------
    1. Complete the HTTP→WebSocket upgrade handshake (RFC 6455 §4.2.2).
    2. Expose an ASGI-compatible ``receive`` coroutine that reads raw WebSocket
       frames from the TCP stream and returns ASGI event dicts.
    3. Expose an ASGI-compatible ``send`` coroutine that accepts ASGI event
       dicts and writes the corresponding WebSocket frames to the TCP stream.
    4. Call the application coroutine with the original HTTP-upgrade scope
       (type='websocket', path=...) so the router can dispatch to the correct
       handler.
    """

    # ------------------------------------------------------------------ #
    # WebSocket frame helpers (RFC 6455 §5)                               #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _encode_frame(payload: bytes, opcode: int = 0x1) -> bytes:
        """Encode *payload* as an unmasked WebSocket data frame.

        ``opcode`` defaults to 0x1 (text); pass 0x2 for binary.
        The server MUST NOT mask frames it sends to the client (RFC 6455 §5.1).
        """
        length = len(payload)
        # FIN=1, RSV=0, opcode in low nibble of first byte
        header = bytes([0x80 | opcode])
        if length < 126:
            header += bytes([length])
        elif length < 65536:
            header += bytes([126]) + length.to_bytes(2, 'big')
        else:
            header += bytes([127]) + length.to_bytes(8, 'big')
        return header + payload

    @staticmethod
    async def _read_frame(reader) -> tuple[int, bytes]:
        """Read one WebSocket frame from *reader*.

        Returns ``(opcode, payload)`` where *payload* is already unmasked.
        Raises ``ConnectionError`` on EOF.
        """
        header = await reader.readexactly(2)
        # byte 0: FIN | RSV | opcode
        opcode = header[0] & 0x0F
        # byte 1: MASK | payload-length
        masked = bool(header[1] & 0x80)
        length = header[1] & 0x7F

        if length == 126:
            length = int.from_bytes(await reader.readexactly(2), 'big')
        elif length == 127:
            length = int.from_bytes(await reader.readexactly(8), 'big')

        if masked:
            mask = await reader.readexactly(4)
            raw = await reader.readexactly(length)
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(raw))
        else:
            payload = await reader.readexactly(length)

        return opcode, payload

    # ------------------------------------------------------------------ #
    # Constructor                                                          #
    # ------------------------------------------------------------------ #

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # args: (app, reader, writer, scope)
        if len(args) > 3:
            self.scope = args[-1]
            logger.debug(self.scope)

    # ------------------------------------------------------------------ #
    # ASGI interface                                                       #
    # ------------------------------------------------------------------ #

    async def receive(self):
        """Read one WebSocket frame and return an ASGI ``websocket.receive`` dict.

        Bug 4 fixed: the old implementation returned a *coroutine object*
        instead of awaiting it, and used HTTP line-delimited reading which
        is completely wrong for the WebSocket binary framing protocol.
        """
        opcode, payload = await self._read_frame(self.reader)

        if opcode in (0x1, 0x9):     # text frame or ping
            return {'type': 'websocket.receive', 'text': payload.decode('utf-8'), 'bytes': None}
        elif opcode == 0x2:           # binary frame
            return {'type': 'websocket.receive', 'text': None, 'bytes': payload}
        elif opcode == 0x8:           # close frame
            return {'type': 'websocket.disconnect', 'code': 1000}
        else:
            logger.warning('Unsupported WebSocket opcode: 0x%02x', opcode)
            return {'type': 'websocket.receive', 'text': None, 'bytes': payload}

    async def send(self, event: dict):
        """Accept an ASGI event dict and write the corresponding WebSocket frame.

        Bug 5 fixed: the old implementation passed the raw dict to
        ``writer.write()``, which expects *bytes*, not a dict.
        """
        event_type = event.get('type', '')

        if event_type == 'websocket.send':
            if 'text' in event and event['text'] is not None:
                frame = self._encode_frame(event['text'].encode('utf-8'), opcode=0x1)
            else:
                frame = self._encode_frame(event.get('bytes', b''), opcode=0x2)
            self.writer.write(frame)
            await self.writer.drain()

        elif event_type == 'websocket.close':
            # Send a close frame (opcode 0x8) with optional status code 1000
            frame = self._encode_frame(b'\x03\xe8', opcode=0x8)
            self.writer.write(frame)
            await self.writer.drain()

        elif event_type == 'websocket.accept':
            # The handshake reply is sent in run(); nothing more needed here.
            pass

        else:
            logger.warning('WebsocketHandler.send: unknown event type %r', event_type)

    # ------------------------------------------------------------------ #
    # Lifecycle                                                            #
    # ------------------------------------------------------------------ #

    async def run(self):
        """Complete the upgrade handshake then call the ASGI application.

        Bug 3 fixed: the old code called ``self.app(...)`` without ``await``,
        creating a coroutine object that was immediately discarded.

        Bug 6 fixed: the old code replaced the scope with
        ``{'type': 'websocket.connect'}`` which lacks ``path``, making the
        router unable to dispatch to the correct handler.  The original
        HTTP-upgrade scope (already containing ``type='websocket'`` and
        ``path``) is passed directly to the application instead.
        """
        # --- RFC 6455 §4.2.2: server handshake response ---
        key = [x[1] for x in self.scope['headers'] if x[0] == b'Sec-WebSocket-Key'][0]
        logger.debug('WebSocket key: %s', key)
        accept = b64encode(sha1(key + b'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').digest())

        reply = (
            'HTTP/1.1 101 Switching Protocols\r\n'
            'Upgrade: websocket\r\n'
            'Connection: Upgrade\r\n'
            f'Sec-WebSocket-Accept: {accept.decode("ascii")}\r\n'
            '\r\n'
        )
        self.writer.write(reply.encode())
        await self.writer.drain()
        logger.debug('WebSocket handshake complete.')

        # --- Dispatch to the ASGI application ---
        # Bug 6: pass the original upgrade scope (type='websocket', path=...)
        # so the router can find the registered handler.
        await self.app(self.scope, self.receive, self.send)


class HTTP1_1Handler(HTTPServerBase):
    def __init__(self, app, reader, writer, request, *args, **kwargs):
        super().__init__(app, reader, writer, *args, **kwargs)
        self.request = request
        logger.info(self.request)

    def has_request(self):
        return self.request is not None

    async def run(self):
        self.request += await self.reader.readuntil(b'\r\n\r\n')

        scope = self.parse()
        logger.info(scope)

        receive = self.make_recepient()
        send = self.make_sender()
        await self.app(scope, receive, send)

    def parse(self):
        """
        Parse received request and make ASGI scope object.
        """
        lines = self.request.decode('utf-8').split('\r\n')

        method, path, version = lines[0].split(' ')

        mapping = {}
        for line in lines[1:]:
            if m := re.match(r'(.*?): (.*)', line):
                mapping[m[1]] = m[2]

        logger.info(mapping)

        scope = {
            'type': 'http',
            'http_version': re.sub(r'HTTP/(.*)', r'\1', version),
            'method': method,
            'path': path,
            'scheme': 'http',
            'headers': [
                # (b'host', mapping['Host'].encode()),
                # (b'accept', b'*/*'),
                # (b'accept-encoding', b'gzip, deflate'),
                # (b'connection', b'keep-alive'),
                # (b'user-agent', b'python-httpx/0.16.1'),
                # (b'key', b'value')
                ],
            'asgi': {'version': '3.0'}
        }

        if 'Host' in mapping:
            host, port = mapping['Host'].split(':')
            port = int(port)
            scope['server'] = [host, port]

        if 'Upgrade' in mapping:
            scope['type'] = mapping['Upgrade']
            if scope['type'] == 'websocket':
                scope['scheme'] = 'ws'

        if 'Connection' in mapping:
            scope['scheme'] = mapping['Connection']

        return scope

    def make_recepient(self):

        async def receive():
            return await self.reader.readuntil(b'\r\n\r\n')
        return receive

    def make_sender(self):
        async def send(x):
            logger.debug(x)
            await self.writer.write(x)
        return send


class HTTP2Server(HTTPServerBase):
    """An ASGI Server class.
    """
    def __init__(self, app, reader, writer):
        super().__init__(app, reader, writer)
        self.client_stream_window_size = {}
        self.root_stream = Stream(0, None, 1)
        self.factory = FrameFactory()

    def find_stream(self, id_):
        if id_ == 0:
            return self.root_stream
        else:
            return self.root_stream.find_child(id_)

    def make_sender(self, stream_identifier):
        async def send(data):
            logger.debug(data)
            if data['type'] == 'http.response.start':
                frame = self.factory.create(FrameTypes.HEADERS,
                                            HeadersFlags.END_HEADERS.value,
                                            stream_identifier)
                frame[':status'] = data['status']
                for k, v in data['headers']:
                    frame[k] = v

            elif data['type'] == 'http.response.body':
                frame = self.factory.create(FrameTypes.DATA,
                                            DataFlags.END_STREAM.value,
                                            stream_identifier,
                                            data=data['body'])

            else:
                logger.info(data)

            self.writer.write(frame.save())

        return send

    async def send_frame(self, frame: FrameBase):
        """Send a frame to the recipient."""
        logger.debug(f'Sending {frame}')
        self.writer.write(frame.save())
        await self.writer.drain()

    async def receive(self) -> bytes:
        """Read the stream, get some data from incoming frames, and parse it"""
        # to distinguish the type of incoming frame
        if (data := await self.reader.read(9)) == 0:
            logger.info('StreamReader got EOF')
            return

        size = int.from_bytes(data[:3], 'big', signed=False)
        data += await self.reader.read(size)  # Add error handling for the case of insufficient data

        return data

    def parse(self, data):
        # Todo: should use this function in run() method.
        pass
        # frame = self.factory.load(data)
        # return frame

    def is_connect(self, frame):
        if not frame or frame.FrameType() == FrameTypes.GOAWAY:
            return False
        return True

    async def run(self):
        # Send the settings at first.
        my_settings = self.factory.create(FrameTypes.SETTINGS, SettingFlags.INIT, 0)
        await self.send_frame(my_settings)
        logger.info(my_settings)

        # Then, parse and handle frames in this loop.
        while data := await self.receive():

            frame = self.factory.load(data)
            self.root_stream.add_child(frame.stream_id)
            if (stream := self.root_stream.find_child(frame.stream_id)) is None:
                logger.error(f'Stream {frame.stream_id} is not found.')
                raise Exception('Unused stream identifier')
            send = self.make_sender(stream.identifier)

            if frame.FrameType() in (FrameTypes.HEADERS, FrameTypes.DATA):
                # As HEADERS and DATA frames should be parsed,
                scope = ParserFactory.Get(frame, stream).parse()
                await self.app(scope, self.receive, send)

            else:
                await RespondFactory.create(frame).respond(self)


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
        context.set_alpn_protocols(['HTTP/1.1', 'h2'])
        context.load_cert_chain(certfile=self.certfile, keyfile=self.keyfile)
        context.options |= ssl.OP_NO_TLSv1 | ssl.OP_NO_TLSv1_1
        context.options |= ssl.OP_NO_COMPRESSION
        self.ssl_context = context

        if hasattr(self, 'raw_sockets'):
            # raw_sockets are already bound; asyncio.start_server will handle
            # TLS via ssl= so no manual wrapping is needed here.
            pass

    async def client_connected_cb(self, reader, writer):
        """
        This function is called when the server receives an access request from a client.
        Handler must handles every exception in it and not raise any exception.
        """
        request_data = await reader.readline()

        try:
            if request_data == HTTP2[:-8]:
                request_data += await reader.read(8)

                if request_data == HTTP2:
                    logger.info('HTTP/2 connection is requested.')
                    handler = HTTP2Server(self.app, reader, writer)
                else:
                    raise ValueError(f'Received an invalid request ({request_data}).')

            else:
                logger.info('HTTP1.1 connection is requested.')
                request_data += await reader.readuntil(b'\r\n\r\n')
                scope = parse(request_data)

                # Check if the connection is to use websocket.
                if scope['type'] == 'websocket':
                    handler = WebsocketHandler(self.app, reader, writer, scope)

                else:
                    handler = HTTP1_1Handler(self.app, reader, writer, scope)

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

    async def run(self, port=80):
        """Run an asyncio socket server with the setting in this object."""
        if not hasattr(self, 'raw_sockets') or not self.raw_sockets:
            self.open_socket(port)

        # asyncio.start_server() / loop.create_server() only accepts a single
        # socket via sock=.  To serve on multiple sockets (one IPv4 + one IPv6)
        # we create one asyncio.Server per raw socket and run them concurrently.
        # Each server receives the raw TCP socket plus ssl= so asyncio handles
        # the TLS handshake internally (pre-wrapping with ssl_context.wrap_socket
        # would cause a double-TLS layer and a broken handshake).
        servers = []
        for sock in self.raw_sockets:
            srv = await asyncio.start_server(
                self.client_connected_cb,
                sock=sock,
                ssl=self.ssl_context,
            )
            servers.append(srv)

        logger.info(f'Server(s) created: {servers}')

        try:
            await asyncio.gather(*(srv.serve_forever() for srv in servers))

        except KeyboardInterrupt:
            logger.info('KeyboardInterrupt is caught.')

        except asyncio.exceptions.CancelledError as e:
            logger.info(type(e))

        except Exception:
            logger.error(traceback.format_exc())

        finally:
            for srv in servers:
                srv.close()
            self.close()
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
