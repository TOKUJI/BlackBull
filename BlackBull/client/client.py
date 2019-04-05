import asyncio
import ssl
from functools import wraps
import concurrent.futures

# private library
from ..util import HTTP2
from ..rsock import create_socket
from ..frame import FrameFactory, FrameTypes, DataFlags, HeadersFlags, SettingFlags, Stream
from ..logger import get_logger_set
logger, log = get_logger_set('client')

from .response import RespondFactory

def connect(fn):
    @wraps(fn)
    async def _fn(*args, **kwds):
        # open streams, send user_name, user_password and get authentification
        await args[0].connect()

        # if 'root' not in args[0].streams:
        #     args[0].streams['root'] = Stream(0, None)
        #     # post a request, 
        #     header = args[0].factory.create(FrameTypes.HEADERS,
        #                                  HeadersFlags.END_HEADERS,
        #                                  args[0].streams['root'].identifier)
        #     data = args[0].factory.create(FrameTypes.DATA,
        #                                DataFlags.END_STREAM,
        #                                args[0].streams['root'].identifier,
        #                                data=b'') # user_name, user_password, 

        return await fn(*args, **kwds)
    return _fn


class Listener:
    def __init__(self, condition, *, event=None, callback=None):
        self.event = event
        self.condition = condition
        self.callback = callback

    def match(self, frame):
        return self.condition(frame)

    def __call__(self, frame):
        if self.match(frame):
            logger.debug('This listener got the frame that matches the condition.')
            if self.event:
                self.event.set()
                logger.debug('The event has been set')
            if self.callback:
                self.callback(frame)


def create_ssl_context(debug=True):
    context = ssl.create_default_context()
    context.set_alpn_protocols(['HTTP/1.1', 'h2']) # to enable HTTP/2, add 'h2'
    if debug:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    context.options |= ssl.OP_NO_TLSv1 | ssl.OP_NO_TLSv1_1
    context.options |= ssl.OP_NO_COMPRESSION
    return context


class Client:
    def __init__(self, *, name, port=80, debug=True):
        self.name = name
        self.port = port
        self.ssl = create_ssl_context(debug)
        self.reader = None
        self.writer = None

        self.factory = FrameFactory()
        self.root_stream = Stream(0, None, 1)

        self.connect_event = asyncio.Event()
        self.disconnect_event = asyncio.Event()
        self.connected = False

        # self._handlers = {}

        self.events = {'disconnect': self.disconnect_event,
                       'connect': self.connect_event
                       }


    # def add_handler(self, key, handler):
    #     self._handlers[key] = handler


    # def remove_handler(self, key):
    #     if self._handlers.has_key(key):
    #         self._handlers.pop(key)


    @log
    def register_event(self, event_id, condition=lambda x: True):
        """
        event_id 
        """
        self.events[event_id] = [asyncio.Event(), condition]
        return self.events[event_id][0]

    @log
    def emit_event(self, frame):
        if frame.stream_identifier not in self.events:
            return

        event, condition = self.events[frame.stream_identifier]
        if condition(frame):
            logger.debug(f'{frame} raises an event.')
            event.set()


    def create_stream(self, identifier, parent, weight):
        if identifier in [c.identifier for c in self.root_stream.get_children()]:
            # TODO: raise some exception.
            logger.error(f'Stream {identifier} is already used.')

        stream = self.root_stream.add_child(identifier)
        return stream


    def find_stream(self, identifier):
        if identifier == 0:
            return self.root_stream
        else:
            return self.root_stream.find_child(identifier)


    async def get_stream(self):
        """
        Search an available stream then lock and return it.
        TODO: if there is no available stream, then derive a stream from an existing stream.
        """

        eos = 1 # EOS stands for End Of Streams
        children = self.root_stream.get_children() # get_children returns a list of streams

        for stream in children:
            if stream.identifier % 2 == 1:
                if not stream.is_locked():
                    return await stream.lock()
                if stream.identifier > eos:
                    eos += 2

        logger.debug(f'Maximum identifier of the stream is {eos - 2}')
        return await self.create_stream(eos, 0, 1).lock()


    async def connect(self):
        """
        Open the connection, but does not log in.
        """
        if self.is_connected():
            return

        try:
            self.reader, self.writer =\
                await asyncio.open_connection('localhost', self.port, ssl=self.ssl)
            logger.debug('connection is open')

            self.writer.write(HTTP2)
            setting = self.factory.create(FrameTypes.SETTINGS, SettingFlags.INIT, 0)
            await self.send_frame(setting)
            self.create_stream(1, 0, 1)

            self.connect_event.set()
            self.disconnect_event.clear()
            self.connected = True

        except asyncio.TimeoutError:
            logger.error('failed to open connection')


    def disconnect(self):
        self.disconnect_event.set()
        self.connect_event.clear()
        self.connected = False

        if self.writer:
            self.writer.close()


    def is_connected(self):
        return self.connected

    @log
    async def receive_frame(self):
        try:
            read_task = asyncio.create_task(self.reader.read(16384))
            disconnect_task = asyncio.create_task(self.disconnect_event.wait())
            done, pending = await asyncio.wait([read_task, disconnect_task],
                                               return_when=asyncio.FIRST_COMPLETED)

            data = done.pop().result()
            if len(data) == 0:
                logger.info('StreamReader got EOF')
                self.disconnect()
                return

            frame = self.factory.load(data)

        finally:
            read_task.cancel()
            disconnect_task.cancel()

        return frame


    def listen(self):
        return asyncio.create_task(self.handle_response())


    async def handle_response(self):
        frame = await self.receive_frame()

        while frame:
            logger.debug('handle_response() got a frame: {}'.format(frame))
            await RespondFactory.create(frame).respond(self)

            # for handler in self._handlers.values():
            #     logger.debug(handler)
            #     await handler(frame)

            self.emit_event(frame)

            # if frame.stream_identifier in self.events:
            #     self.events[frame.stream_identifier].set()

            frame = await self.receive_frame()


    async def ping(self):
        ping = self.factory.create(FrameTypes.PING, 0x0, 0, data=b'\x00\x00\x00\x00\x00\x00\x00\x00')
        await self.send_frame(ping)


    async def send_frame(self, frame):
        data = frame.save()
        logger.debug(f'sending {frame}, {data}')

        self.writer.write(data)
        await self.writer.drain()


    def __del__(self):
        self.disconnect()
