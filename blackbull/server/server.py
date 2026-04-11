import asyncio
import ssl
import traceback
import re
from collections import defaultdict, deque
from unittest import case
from unittest import case
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
from ..frame import FrameFactory, FrameTypes, FrameBase, SettingFlags
from ..logger import get_logger_set
from .response import RespondFactory
from .parser import ParserFactory
from .sender import SenderFactory, WebSocketSender, WSOpcode, WSFrameBits
logger, log = get_logger_set(__name__)



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

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # args: (app, reader, writer, scope)
        if len(args) > 3:
            self.scope = args[-1]
            logger.debug(self.scope)
        
        self._connect_sent = False

    async def receive(self):
        """Read one WebSocket frame and return an ASGI ``websocket.receive`` dict.
        If the frame is a close frame, return an ASGI ``websocket.disconnect`` dict instead.
        """
        if not self._connect_sent:
            self._connect_sent = True
            return {'type': 'websocket.connect'}

        while True:
            opcode, masked, length = await WebSocketSender._read_opcode(self.reader)
            payload = await WebSocketSender._read_payload(self.reader, masked, length)

            if not masked:
                raise ValueError('Received unmasked frame from client, which is a protocol violation.') 
                

            match opcode:
                case WSOpcode.TEXT:  # text frame
                    return {'type': 'websocket.receive', 'text': payload.decode('utf-8'), 'bytes': None}

                case WSOpcode.BINARY:# binary frame
                    return {'type': 'websocket.receive', 'text': None, 'bytes': payload}

                case WSOpcode.CLOSE:           # close frame
                    return {'type': 'websocket.disconnect', 'code': 1000}

                case WSOpcode.PING:           # ping — reply immediately, then read next frame
                    pong = WebSocketSender._encode_frame(payload, opcode=WSOpcode.PONG)
                    self.writer.write(pong)
                    await self.writer.drain()

                case WSOpcode.PONG:           # unsolicited pong — silently drop
                    pass

                case _:
                    logger.warning('Unsupported WebSocket opcode: 0x%02x', opcode)
                    return {'type': 'websocket.receive', 'text': None, 'bytes': payload}

    async def run(self):
        """Complete the upgrade handshake then call the ASGI application."""
        # --- RFC 6455 §4.2.2: server handshake response ---
        key = [x[1] for x in self.scope['headers'] if x[0] == b'sec-websocket-key'][0]
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

        send = SenderFactory.websocket(self.writer)
        await self.app(self.scope, self.receive, send)


class HTTP11Handler(BaseHandler):
    def __init__(self, app, reader, writer, request, *args, **kwargs):
        super().__init__(app, reader, writer, *args, **kwargs)
        self.request = request
        logger.info(self.request)

    def has_request(self):
        return self.request is not None

    async def run(self):
        # self.request holds only the first line; read the remaining headers here.
        self.request += await self.reader.readuntil(b'\r\n\r\n')

        scope = self.parse()

        # Fill client / server / scheme from the transport now that we have it.
        transport = self.writer.transport
        peername = transport.get_extra_info('peername')
        if peername:
            scope['client'] = list(peername[:2])  # (host, port)

        sockname = transport.get_extra_info('sockname')
        if sockname and scope['server'] is None:
            scope['server'] = list(sockname[:2])

        is_tls = transport.get_extra_info('ssl_object') is not None
        if is_tls:
            scope['scheme'] = 'wss' if scope.get('type') == 'websocket' else 'https'

        logger.debug(scope)

        if scope.get('type') == 'websocket':
            await WebSocketHandler(self.app, self.reader, self.writer, scope).run()
            return

        receive = self.make_recepient(scope)
        send = self.make_sender()
        await self.app(scope, receive, send)

    def parse(self):
        """
        Parse header lines of received request and make ASGI scope object.
        """
        lines = self.request.split(b'\r\n') # Lines are bytes, not str, because the request is read as bytes from the stream.

        method, path, version = lines[0].split(b' ')
        path_parsed = urlparse(path)

        mapping = {}
        for line in lines[1:]:
            line = line.strip()
            if b':' in line:
                key, value = line.split(b':', 1)
                mapping[key.lower()] = value.strip()
        logger.debug(mapping)

        scope = {
            'type': 'http',
            'asgi': {'version': '3.0', 'spec_version': '2.0'},
            'http_version': re.sub(r'HTTP/(.*)', r'\1', version.decode('utf-8')),
            'method': method.decode('utf-8'),
            'scheme': 'http',  # upgraded to 'https'/'wss' in run() when TLS
            'path': path.decode('utf-8'),
            'raw_path': path,
            'query_string': path_parsed.query,
            # 'root_path': '',
            'headers': [(key.lower(), value) for key, value in mapping.items()],
                # Below is examples of how headers are represented in ASGI scope['headers'] 
                # as a list of (key, value) tuples, where both key and value are lowercased bytes.
                # (b'host', mapping[b'Host']),
                # (b'accept', b'*/*'),
                # (b'accept-encoding', b'gzip, deflate'),
                # (b'connection', b'keep-alive'),
                # (b'user-agent', b'python-httpx/0.16.1'),
                # (b'key', b'value')
            'client': None,  # set in run() from transport peername
            'server': None,  # set from Host header; fallback to sockname in run()
            # A mutable dict that can be used to store arbitrary data during the connection's lifetime. 
            # The application can read and write to this dict, and it will persist across multiple calls 
            # to the application for the same connection.
            'state': {},
        }

        if b'host' in mapping:
            host, port = mapping[b'host'].split(b':')
            scope['server'] = [host.decode('utf-8'), int(port)]

        if b'upgrade' in mapping:
            scope['type'] = mapping[b'upgrade'].decode('utf-8').lower()
            if scope['type'] == 'websocket':
                scope['scheme'] = 'ws'
        elif b'connection' in mapping:
            scope['scheme'] = mapping[b'connection'].decode('utf-8').lower()

        return scope

    def make_recepient(self, scope):

        async def receive():
            """
            Read the stream, get some data from incoming frames, parse it, and return the ASGI event dict.
            """
            # Determine the content length from headers, if present
            content_length = 0
            # Check if 'Content-Length' or 'Transfer-Encoding' is in scope['headers']
            headers = dict(scope['headers'])
            has_content_length = b'content-length' in headers
            has_transfer_encoding = b'transfer-encoding' in headers

            if has_content_length:
                content_length = int(headers[b'content-length'].decode())
                message_body = await self.reader.read(content_length) if content_length > 0 else b''
            elif has_transfer_encoding and headers[b'transfer-encoding'].strip().lower() == b'chunked':
                parts = []
                while True:
                    size_line = await self.reader.readuntil(b'\r\n')
                    chunk_size = int(size_line.strip(), 16)
                    if chunk_size == 0:
                        await self.reader.readuntil(b'\r\n')  # consume trailing CRLF
                        break
                    parts.append(await self.reader.read(chunk_size))
                    await self.reader.readuntil(b'\r\n')  # consume CRLF after chunk data
                message_body = b''.join(parts)
            elif has_transfer_encoding:
                raise NotImplementedError(
                    f'Transfer-Encoding "{headers[b"transfer-encoding"].decode()}" is not supported.'
                )
            else:
                message_body = b''

            logger.debug(f'Message body: {message_body}')
            return {
                "type": "http.request",
                "body": message_body,
                "more_body": False,  # True if more data is expected
            }

        return receive

    def make_sender(self):
        return SenderFactory.http1(self.writer)


class HTTP2Handler(BaseHandler):
    """An ASGI Server class.
    """
    def __init__(self, app, reader, writer):
        super().__init__(app, reader, writer)
        self.client_stream_window_size = {}
        self.root_stream = Stream(0, None, 1)
        self.factory = FrameFactory()
        # Stream 0: connection-level control-plane sender (SETTINGS, PING, …)
        self._control_sender = SenderFactory.http2(self.writer, self.factory, 0)

    def find_stream(self, id_):
        if id_ == 0:
            return self.root_stream
        else:
            return self.root_stream.find_child(id_)

    def make_sender(self, stream_identifier):
        return SenderFactory.http2(self.writer, self.factory, stream_identifier)

    async def send_frame(self, frame: FrameBase):
        """Send a raw HTTP/2 frame via the control-plane sender."""
        await self._control_sender(frame)

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
        frame = self.factory.load(data)

        self.root_stream.add_child(frame.stream_id)
        if (stream := self.root_stream.find_child(frame.stream_id)) is None:
            logger.error(f'Stream {frame.stream_id} is not found.')
            raise Exception('Unused stream identifier')

        scope = None
        if frame.FrameType() in (FrameTypes.HEADERS, FrameTypes.DATA):
            scope = ParserFactory.Get(frame, stream).parse()

        return frame, stream, scope

    def is_connect(self, frame):
        if not frame or frame.FrameType() == FrameTypes.GOAWAY:
            return False
        return True

    async def run(self):
        # Send the settings at first.
        my_settings = self.factory.create(FrameTypes.SETTINGS, SettingFlags.INIT, 0)
        await self.send_frame(my_settings)
        logger.info(my_settings)

        waiting_continuation = True

        # Then, parse and handle frames in this loop.
        header_frame = None
        while data := await self.receive():

            # Get a frame object by parsing the received data.
            frame = self.factory.load(data)

            # Add the stream identifier of the frame to the root stream's children if it's not already there.
            self.root_stream.add_child(frame.stream_id)
            if (stream := self.root_stream.find_child(frame.stream_id)) is None:
                logger.error(f'Stream {frame.stream_id} is not found.')
                raise Exception('Unused stream identifier')

            scope = None

            send = self.make_sender(stream.identifier)

            match frame.FrameType():
                case FrameTypes.HEADERS:
                    if frame.end_headers:
                        waiting_continuation = False
                        if frame.end_stream:
                            scope = ParserFactory.Get(frame, stream).parse()
                        else:
                            header_frame = frame
                            continue
                    else:
                        waiting_continuation = True
                        header_frame = frame
                        continue

                case FrameTypes.CONTINUATION:
                    if not waiting_continuation:
                        logger.error('Received unexpected CONTINUATION frame without preceding HEADERS frame.')
                        raise Exception('Unexpected CONTINUATION frame')

                    header_frame.raw_block  += frame.payload

                    if frame.end_headers:
                        logger.debug('Received complete HEADERS block after CONTINUATION frames.')
                        waiting_continuation = False
                        header_frame.parse_payload()
                        scope = ParserFactory.Get(header_frame, stream).parse()
                        header_frame = None
                    else:
                        continue  # more CONTINUATION frames expected

                case FrameTypes.DATA:
                    if frame.end_stream:
                        scope1 = ParserFactory.Get(header_frame, stream).parse()
                        scope2 = ParserFactory.Get(frame, stream).parse()
                        scope = {**scope1, **scope2}  # Merge the scopes from HEADERS and DATA frames

                case FrameTypes.GOAWAY:
                    await self.send_frame(self.factory.create(FrameTypes.GOAWAY, SettingFlags.INIT, 0))
                    self.writer.close()
                    break

                case _:
                    await RespondFactory.create(frame).respond(self)

            if scope:
                await self.app(scope, self.receive, send)
                


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
