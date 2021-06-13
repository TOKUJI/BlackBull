import asyncio
import ssl
import traceback
import re
from collections import defaultdict, deque
from urllib.parse import urlparse
from hashlib import sha1
from base64 import b64encode

# private library
from ..utils import HTTP2, pop_safe, EventEmitter, check_port
from ..stream import Stream
from ..rsock import create_socket
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

    if 'Connection' in mapping:
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
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if len(args) > 3:
            self.scope = args[-1]
            logger.debug(self.scope)

    async def run(self):
        logger.warning('Under construction.')
        key = [x[1] for x in self.scope['headers'] if x[0] == b'Sec-WebSocket-Key'][0]
        logger.debug(key)
        accept = b64encode(sha1(key + b'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').digest())
        logger.debug(accept)

        reply = '\r\n'.join(['HTTP/1.1 101 Switching Protocols',
                             'Upgrade: websocket',
                             'Connection: Upgrade',
                             f'Sec-WebSocket-Accept: {accept.decode("ascii")}',
                             ]) \
                + '\r\n\r\n'

        await self.send(reply.encode())
        logger.debug(reply.encode())
        # Shake hands ends

        scope = {'type': 'websocket.connect'}
        self.app(scope, self.receive, self.send)

    async def receive(self):
        return self.reader.readuntil(b'\r\n\r\n')

    async def send(self, x):
        return self.writer.write(x)


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

    @property
    def keyfile(self):
        return self._keyfile if hasattr(self, '_keyfile') else None

    @keyfile.setter
    def keyfile(self, value):
        self._keyfile = value
        self.make_ssl_context()

    @property
    def certfile(self):
        return self._certfile if hasattr(self, '_certfile') else None

    @certfile.setter
    def certfile(self, value):
        self._certfile = value
        self.make_ssl_context()

    def make_ssl_context(self):
        if not self.certfile or not self.keyfile:
            return

        context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        context.set_alpn_protocols(['HTTP/1.1', 'h2'])
        context.load_cert_chain(certfile=self.certfile, keyfile=self.keyfile)
        context.options |= ssl.OP_NO_TLSv1 | ssl.OP_NO_TLSv1_1
        context.options |= ssl.OP_NO_COMPRESSION
        self.ssl_context = context

        if hasattr(self, 'raw_socket'):
            self.socket = self.ssl_context.wrap_socket(self.raw_socket, server_side=True)

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
            return

        self.raw_socket = create_socket(('::1', port))

        if self.raw_socket is None:
            logger.error(f'Failed to open port ({port}.) Try another port.')
            return

        self.port = self.raw_socket.getsockname()[1]

        if self.ssl_context:
            self.socket = self.ssl_context.wrap_socket(self.raw_socket, server_side=True)
        else:
            self.socket = self.raw_socket

    def close_socket(self):
        self.socket.close()

    async def run(self, port=80):
        if self.socket is None:
            self.open_socket(port)
        """Run an asyncio socket server with the setting in this object."""
        socket_server = await asyncio.start_server(self.client_connected_cb, sock=self.socket)
        logger.info(f'Server ({socket_server}) has been created.')

        try:
            await socket_server.serve_forever()

        except KeyboardInterrupt:
            logger.info('KeyboardInterrupt is caught.')

        except asyncio.exceptions.CancelledError as e:
            logger.info(type(e))

        except Exception:
            logger.error(traceback.format_exc())

        finally:
            self.close()
            logger.info('Server has been stopped.')

    def close(self):
        logger.info('ASGIServer.close() is called.')
        logger.info(self.__dict__)
        self.close_socket()
