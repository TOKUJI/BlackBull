import asyncio
import ssl
from collections import defaultdict, deque
from urllib.parse import urlparse

# private library
from ..util import HTTP2, pop_safe
from ..rsock import create_socket
from ..frame import FrameFactory, FrameTypes, FrameBase, HeadersFlags, DataFlags, Stream
from ..logger import get_logger_set
from .response import RespondFactory
logger, log = get_logger_set('server')

class HandlerBase:
    def __init__(self, app, reader, writer):
        """docstring for HandlerBase
        Parameters
        ----------
        app:
            An ASGI application that handles scope (when app is called for the
            first time) and receive and send (when it is called for the second
            time)
        reader:
            An reader object that receives from TCP/IP socket.
        writer:
            An writer object that send to TCP/IP socket.
        """
        self.app = app
        self.reader = reader
        self.writer = writer

    def run(self):
        raise NotImplementedError()

class HTTP2Handler(HandlerBase):
    def __init__(self, app, reader, writer):
        super().__init__(app, reader, writer)
        self.client_stream_window_size = {}
        self.streams = {'root': Stream(0, None, 1)}
        self.factory = FrameFactory()


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

        logger.debug('parse_stream(): {}'.format(data))

        size = int.from_bytes(data[:3], 'big', signed=False)
        data += await self.reader.read(size) # Add error handling for the case of insufficient data

        frame = self.factory.load(data)
        return frame


    async def make_scope(self, headers=None, *, scope=None):
        if scope is None:
            scope = {}
            scope['type'] = 'http'
            scope['http_version'] = '2'

        logger.debug(scope)

        pop_safe(':method', headers, scope, new_key='method')
        pop_safe(':scheme', headers, scope, new_key='scheme')
        pop_safe(':path', headers, scope, new_key='path')

        if 'path' in scope:
            parsed = urlparse(scope['path'])
            scope['query_string'] = parsed.query
            scope['root_path'] = ''
            scope['client'] = None

        if ':authority' in headers:
            scope['headers'] = headers.pop(':authority').split(':')

        scope.update(headers)

        return scope


    async def make_event(self, data=None, *, event=None):
        logger.info(data.payload)
        if not event:
            event = {'event': {'type': 'http.request', 'body': data.payload}}

        return event            



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
        # if you create Responder object frame by frame,
        # they cannot share scope object. TODO:
        if frame.FrameType() == FrameTypes.HEADERS:
            logger.debug('Handling HEADERS')
            await RespondFactory.create(frame).respond(self)

        elif frame.FrameType() == FrameTypes.DATA:
            logger.debug('Handling DATA')
            await RespondFactory.create(frame).respond(self)

        elif frame.FrameType() in (FrameTypes.PING, FrameTypes.WINDOW_UPDATE, FrameTypes.SETTINGS, FrameTypes.PRIORITY):
            await RespondFactory.create(frame).respond(self)

        else:
            logger.warn('something wrong happend while handling this frame: {}'.format(frame))


    def is_connect(self, frame):
        if not frame or frame.FrameType() == FrameTypes.GOAWAY:
            return False
        return True


    async def run(self):
        # Send the settings at first.
        my_settings = self.factory.create(FrameTypes.SETTINGS, 0x0, 0)
        await self.send_frame(my_settings)

        # Then, parse and handle frames in this do-while loop
        frame = await self.parse_stream()
        while self.is_connect(frame):

            # if frame.has_continuation():
            #     self.streams[frame.stream_identifier]
            #     continue

            if frame.stream_identifier in self.streams and \
                 frame.FrameType() in (FrameTypes.DATA, FrameTypes.HEADERS):

                logger.debug('{} is a part of previous frames'.format(frame.stream_identifier))
                await self.handle_frame(frame)
                # self.streams[frame.stream_identifier].append(frame)
                # if frame.flags & DataFlags.END_STREAM.value:
                #     logger.debug('find end of stream')
                #     await self.handle_request(self.streams[frame.stream_identifier].popleft())

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

