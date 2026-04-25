import asyncio
import ssl
from functools import wraps
from collections import defaultdict
import concurrent.futures
from typing import Any

# private library
from ..utils import HTTP2, EventEmitter
from ..protocol.rsock import create_socket
from ..protocol.frame import FrameFactory, FrameTypes, DataFrameFlags, HeaderFrameFlags, SettingFrameFlags, PingFrameFlags
from ..protocol.stream import Stream
from ..logger import get_logger_set, log
logger, _ = get_logger_set('client')

# Largest single read off the underlying transport per receive_frame() call.
_READ_BUFFER_SIZE = 16384


from .response import ResponderFactory

def connect(fn):
    @wraps(fn)
    async def _fn(*args, **kwds):
        # open streams, send user_name, user_password and get authentification
        await args[0].connect()
        return await fn(*args, **kwds)
    return _fn


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
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None

        self.factory = FrameFactory()
        self.root_stream = Stream(0, None, 1)

        self.connect_event = asyncio.Event()
        self.disconnect_event = asyncio.Event()
        self.receiver_frame_event = asyncio.Event()
        self.connected = False


        # self._handlers = {}

        self.events: dict[str, Any] = {'disconnect': self.disconnect_event,
                                        'connect': self.connect_event
                                        }

        self.event_emitter = EventEmitter()

    @log(logger)
    def register_event(self, event_id, condition=lambda x: True):
        """
        event_id 
        """
        logger.debug(f'An event for stream No. {event_id} is prepared')
        self.events[event_id] = [asyncio.Event(), condition]
        return self.events[event_id][0]

    @log(logger)
    def emit_event(self, frame):
        self.event_emitter.emit(self.receiver_frame_event, frame)


    def create_stream(self, stream_id, parent, weight):
        if stream_id in [c.stream_id for c in self.root_stream.get_children()]:
            # TODO: raise some exception.
            logger.error(f'Stream {stream_id} is already used.')

        stream = self.root_stream.add_child(stream_id)
        return stream


    def find_stream(self, stream_id):
        if stream_id == 0:
            return self.root_stream
        else:
            return self.root_stream.find_child(stream_id)


    def get_stream(self):
        """
        Search an available stream then lock and return it.
        TODO: if there is no available stream, then derive a stream from an existing stream.
        """

        eos = 1 # EOS stands for End Of Streams
        children = self.root_stream.get_children() # get_children returns a list of streams

        for stream in children:
            if stream.stream_id % 2 == 1:
                if not stream.is_locked:
                    return stream
                if stream.stream_id > eos:
                    eos += 2

        logger.debug(f'Maximum identifier of the stream is {eos - 2}')
        return self.create_stream(eos, 0, 1)


    async def connect(self):
        """
        Open the connection, but does not log in.
        """
        if self.is_connected:
            return

        try:
            self.reader, self.writer =\
                await asyncio.open_connection('localhost', self.port, ssl=self.ssl)
            logger.debug('connection is open')

            self.writer.write(HTTP2)
            setting = self.factory.create(FrameTypes.SETTINGS, SettingFrameFlags.INIT, 0)
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


    @property
    def is_connected(self) -> bool:
        return self.connected

    @log(logger)
    async def receive_frame(self):
        """ Receives data from the reader, creates a frame."""
        read_task: asyncio.Task | None = None
        disconnect_task: asyncio.Task | None = None
        assert self.reader is not None
        try:
            # @TODO Absorbs data as many as possible. Adds an except section below.
            read_task = asyncio.create_task(self.reader.read(_READ_BUFFER_SIZE))
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
            if read_task:
                read_task.cancel()
            if disconnect_task:
                disconnect_task.cancel()

        return frame


    def listen(self):
        return asyncio.create_task(self.handle_response())


    async def handle_response(self):
        frame = await self.receive_frame()

        while frame:
            logger.debug(f'handle_response() got a frame: {frame}')
            await ResponderFactory.create(frame).respond(self)

            self.emit_event(frame)

            frame = await self.receive_frame()


    async def ping(self):
        ping = self.factory.create(FrameTypes.PING, PingFrameFlags.ACK, 0, data=b'\x00\x00\x00\x00\x00\x00\x00\x00')
        await self.send_frame(ping)


    async def send_frame(self, frame):
        data = frame.save()
        logger.debug(f'sending {frame}, {data}')

        assert self.writer is not None
        self.writer.write(data)
        await self.writer.drain()


    def __del__(self):
        self.disconnect()
