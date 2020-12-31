import asyncio
import ssl
from collections import defaultdict, deque
import traceback

# private library
from ..utils import HTTP2, pop_safe, EventEmitter, check_port
from ..stream import Stream
from ..rsock import create_socket
from ..frame import FrameFactory, FrameTypes, FrameBase, HeadersFlags, DataFlags, SettingFlags
from ..logger import get_logger_set
from .response import RespondFactory
logger, log = get_logger_set('server')


class HandlerBase:
    def __init__(self, app, reader, writer, *args, **kwargs):
        """docstring for HandlerBase
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

    async def run(self):
        logger.error('Not implemented.')
        raise NotImplementedError()


class HTTP1_1Handler(HandlerBase):
    def __init__(self, app, reader, writer, request, *args, **kwargs):
        super().__init__(app, reader, writer, *args, **kwargs)
        self.request = request

    def has_request(self):
        logger.info(self.request)
        return self.request is not None

    async def run(self):

        while self.has_request():
            scope = self.parse()
            receive = self.make_recepient()
            send = self.make_sender()
            await self.app(scope, receive, send)

            self.request = await self.reader.read()

    def parse(self):
        """
        Parse received request and make ASGI scope object.
        """
        scope = {
            'type': 'http',
            'http_version': '1.1',
            'method': 'GET',
            'path': '/json',
            'raw_path': b'/json',
            'root_path': '',
            'scheme': 'https',
            'query_string': b'',
            'headers': [
                (b'host', b'localhost:8000'),
                (b'accept', b'*/*'),
                (b'accept-encoding', b'gzip, deflate'),
                (b'connection', b'keep-alive'),
                (b'user-agent', b'python-httpx/0.16.1'),
                (b'key', b'value')
                ],
            'client': ['127.0.0.1', 37412],
            'server': ['127.0.0.1', 8000],
            'asgi': {'version': '3.0'}
        }
        lines = self.request.split(b'\r\n')
        logger.info(lines)

    def make_recepient(self):
        pass

    def make_sender(self):
        pass


class HTTP2Handler(HandlerBase):
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

    async def send_frame(self, frame: FrameBase):
        """Send a frame to the recipient."""
        logger.debug('sending {}'.format(frame))
        self.writer.write(frame.save())
        await self.writer.drain()

    async def parse_stream(self):
        """Read the stream, get some data of the incoming frame, and parse it"""

        # to distinguish the type of incoming frame
        data = await self.reader.read(9)
        if len(data) == 0:
            logger.info('StreamReader got EOF')
            return

        logger.debug(f'parse_stream(): {data}')

        size = int.from_bytes(data[:3], 'big', signed=False)
        data += await self.reader.read(size)  # Add error handling for the case of insufficient data

        frame = self.factory.load(data)
        return frame

    def make_header(self):
        """ Make or update the header by the scope. """
        pass

    def make_sender(self, stream_identifier):
        async def send(data):
            nonlocal stream_identifier
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
                return
            self.writer.write(frame.save())

        return send

    async def handle_frame(self, frame):
        if frame.FrameType() in (FrameTypes.HEADERS, FrameTypes.DATA):
            await RespondFactory.create(frame).respond(self)

        elif frame.FrameType() in (FrameTypes.PING, FrameTypes.WINDOW_UPDATE, FrameTypes.SETTINGS, FrameTypes.PRIORITY):
            await RespondFactory.create(frame).respond(self)

        elif frame.FrameType() in (FrameTypes.RST_STREAM,):
            await RespondFactory.create(frame).respond(self)

        else:
            logger.warn(f'something wrong happend while handling this frame: {frame}')

    def is_connect(self, frame):
        if not frame or frame.FrameType() == FrameTypes.GOAWAY:
            return False
        return True

    async def run(self):
        # Send the settings at first.
        my_settings = self.factory.create(FrameTypes.SETTINGS, SettingFlags.INIT, 0)

        # Then, parse and handle frames in this do-while loop
        frame = await self.parse_stream()

        while self.is_connect(frame):
            await self.handle_frame(frame)
            frame = await self.parse_stream()


class ASGIServer:
    """An ASGI Server class. When ssl_context or certfile is set,
    this server runs as a HTTPS server.
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
        context.set_alpn_protocols(['HTTP/1.1', 'h2'])  # to enable HTTP/2, add 'h2'
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

        if request_data == HTTP2[:-8]:
            request_data += await reader.read(8)

            if request_data == HTTP2:
                logger.info('HTTP/2 connection is requested.')
                handler = HTTP2Handler(self.app, reader, writer)
            else:
                raise ValueError(f'Received an invalid request ({request_data}).')

        else:
            logger.info('HTTP1.1 connection is requested.')
            request_data += await reader.read()
            handler = HTTP1_1Handler(self.app, reader, writer, request_data)

        try:
            await handler.run()
        except Exception:
            logger.error(traceback.format_exc())

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
        """Run an asyncio server with the setting in this object."""
        self.server = await asyncio.start_server(self.client_connected_cb, sock=self.socket)
        logger.info(f'Server ({self.server}) has been created.')

        try:
            await self.server.serve_forever()

        except KeyboardInterrupt:
            logger.info('KeyboardInterrupt is caught.')

        except asyncio.exceptions.CancelledError as e:
            logger.info(type(e))

        except Exception:
            logger.error(traceback.format_exc())

        finally:
            logger.info('Server has been stopped.')

    def close(self):
        logger.info('ASGIServer.close() is called.')
        logger.info(self.__dict__)
        self.close_socket()
