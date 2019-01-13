import asyncio
import ssl
from collections import defaultdict, deque
from urllib.parse import urlparse
from functools import partial

# private library
from .util import HTTP2
from .rsock import create_socket
from .frame import FrameFactory, FrameTypes, FrameBase, HeadersFlags, DataFlags
from .logger import get_logger_set
logger, log = get_logger_set('server')

class HandlerBase:
    """docstring for HandlerBase"""
    def __init__(self, app, reader, writer):
        self.app = app
        self.reader = reader
        self.writer = writer

    def run(self):
        raise NotImplementedError()

class HTTP2Handler(HandlerBase):
    def __init__(self, app, reader, writer):
        super().__init__(app, reader, writer)
        self.client_stream_window_size = {}
        self.streams = defaultdict(deque)
        self.factory = FrameFactory()


    async def send_frame(self, frame: FrameBase):
        """Send a frame to the recipient."""
        self.writer.write(frame.save())
        await self.writer.drain()


    async def parse_stream(self):
        """Read the stream, get some data of the incoming frame, and parse it"""

        # to distinguish the type of incoming frame
        data = await self.reader.read(9)
        logger.debug('parse_stream(): {}'.format(data))
        if len(data) != 9:
            return

        size = int.from_bytes(data[:3], 'big', signed=False)
        data += await self.reader.read(size)

        frame = self.factory.load(data)
        return frame

    async def make_scope(self, headers=None, data=None):
        logger.info(headers)
        scope = {}
        scope['type'] = 'http'
        scope['http_version'] = '2'
        scope['method'] = headers[':method']
        scope['scheme'] = headers[':scheme']
        scope['path'] = headers[':path']

        parsed = urlparse(headers[':path'])
        scope['query_string'] = parsed.query
        scope['root_path'] = ''
        scope['headers'] = [(k, v) for k, v in headers.items()]
        scope['client'] = None
        scope['server'] = headers[':authority'].split(':')

        return scope

    def make_sender(self, stream_identifier):
        async def send(data: dict):
            nonlocal stream_identifier
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
        if frame.FrameType() == FrameTypes.HEADERS:
            logger.debug('Handling HEADERS')
            scope = await self.make_scope(headers=frame)
            fn = self.app(scope)

            receive = partial(self.make_scope, headers=frame)
            await fn(receive, self.make_sender(frame.stream_identifier))

        elif frame.FrameType() == FrameTypes.SETTINGS:
            if frame.flags == 0x0:
                if hasattr(frame, 'initial_window_size'):
                    self.initial_window_size = frame.initial_window_size
                if hasattr(frame, 'header_table_size'):
                    # TODO: update header_table_size
                    pass
                res = self.factory.create(FrameTypes.SETTINGS,
                                          0x1,
                                          frame.stream_identifier)
                await self.send_frame(res)

            elif frame.flags == 0x1:
                logger.debug('Got ACK')

        elif frame.FrameType() == FrameTypes.PING:
            res = self.factory.create(FrameTypes.PING,
                                      0x1,
                                      frame.stream_identifier,
                                      data=frame.payload)
            await self.send_frame(res)

        elif frame.FrameType() == FrameTypes.WINDOW_UPDATE:
            if frame.stream_identifier == 0:
                self.client_window_size = frame.window_size
            else:
                self.client_stream_window_size[frame.stream_identifier] = frame.window_size

    def is_connect(self, frame):
        if not frame or frame.FrameType() == FrameTypes.GOAWAY:
            return False
        return True


    async def run(self):
        # Send the settings at first.
        my_settings = self.factory.create(FrameTypes.SETTINGS, 0x0, 0)
        await self.send_frame(my_settings)

        frame = await self.parse_stream()
        while self.is_connect(frame):

            if frame.has_continuation():
                self.streams[frame.stream_identifier].append(frame)
                continue

            elif frame.stream_identifier in self.streams and \
                 frame.FrameType() in (FrameTypes.DATA, FrameTypes.HEADERS):

                logger.debug('{} is a part of previous frames'.format(frame.stream_identifier))
                self.streams[frame.stream_identifier].append(frame)
                if frame.flags & DataFlags.END_STREAM.value:
                    logger.debug('find end_stream')
                    await self.handle_request(self.streams[frame.stream_identifier].popleft())

            else:
                logger.debug('Handle this frame solely: {}'.format(frame))
                await self.handle_frame(frame)

            frame = await self.parse_stream()


class ASGIServer:
    """ An ASGI Server class. When ssl_context or certfile is set,
    this server runs as a HTTPS server.
    """

    def __init__(self, app, *,
                 ssl_context =None, certfile=None, keyfile=None, password=None, **kwds):
        self.app = app

        # Create TLS context
        if ssl_context and certfile:
            raise TypeError('SSLContext and certfile must not be set at the same time')

        if ssl_context:
            self.ssl = ssl_context
        elif certfile:
            context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
            context.set_alpn_protocols(['HTTP/1.1', 'h2']) # to enable HTTP/2, add 'h2'
            context.load_cert_chain('server.crt', keyfile='server.key')
            context.options |= ssl.OP_NO_TLSv1 | ssl.OP_NO_TLSv1_1
            context.options |= ssl.OP_NO_COMPRESSION
            self.ssl = context

    async def client_connected_cb(self, reader, writer):
        """Handler must handles every exception in it and not raise any exception."""
        request_data = await reader.read(24)
        if request_data == HTTP2:
            logger.info('HTTP/2 connection is requested.')
            handler = HTTP2Handler(self.app, reader, writer)

        else:
            logger.info('HTTP1.1 connection is requested.')
            request_data += await reader.read(8000)
            handler = HTTP1_1Handler(self.app, reader, writer)
            handler.request_data = request_data

        await handler.run()
        writer.close()

    async def run(self, port=80):
        rsock_ = create_socket((None, port))
        if self.ssl:
            self.socket = self.ssl.wrap_socket(rsock_, server_side=True)
        await asyncio.start_server(self.client_connected_cb, sock=self.socket)

    def route(self, method='GET', path='/'):
        return self._route.route(method=method, path=path)

