from functools import partial

from ..stream import Stream
from ..frame import FrameTypes
from ..logger import get_logger_set, log

logger, _ = get_logger_set('server.response')

empty_event = {'event':{'type': 'http.request', 'body': b''}}


class RespondFactory:
    @staticmethod
    def create(frame):
        factory = {klass.FrameType(): klass for klass in RespondBase.__subclasses__()}
        return factory[frame.FrameType()](frame)


class RespondBase:
    def __init__(self, frame):
        self.frame = frame


    async def respond(self, handler):
        raise NotImplementedError()


    @staticmethod
    def FrameType():
        raise NotImplementedError()


class Respond2Ping(RespondBase):
    async def respond(self, handler):
        res = handler.factory.create(FrameTypes.PING,
                                     0x1,
                                     self.frame.stream_identifier,
                                    data=self.frame.payload)
        await handler.send_frame(res)


    @staticmethod
    def FrameType():
        return FrameTypes.PING


class Respond2WindowUpdate(RespondBase):
    async def respond(self, handler):
        if self.frame.stream_identifier == 0:
            handler.client_window_size = self.frame.window_size
        else:
            handler.client_stream_window_size[self.frame.stream_identifier] = self.frame.window_size


    @staticmethod
    def FrameType():
        return FrameTypes.WINDOW_UPDATE


class Respond2Settings(RespondBase):
    async def respond(self, handler):
        if self.frame.flags == 0x0:
            if hasattr(self.frame, 'initial_window_size'):
                handler.initial_window_size = self.frame.initial_window_size
            if hasattr(self.frame, 'header_table_size'):
                # TODO: update header_table_size
                pass
            res = handler.factory.create(FrameTypes.SETTINGS,
                                         0x1,
                                         self.frame.stream_identifier)
            await handler.send_frame(res)

        elif self.frame.flags == 0x1:
            logger.debug('Got ACK. Do nothing.')


    @staticmethod
    def FrameType():
        return FrameTypes.SETTINGS


class Respond2Priority(RespondBase):
    async def respond(self, handler):
        if self.frame.exclusion:
            for x in handler.root_stream.get_children():
                if x.parent == self.frame.dependent_stream:
                    x.parent = self.frame.stream_identifier

        stream_id = self.frame.stream_identifier # to be shorten the description
        stream = handler.root_stream.find_child(stream_id)

        if not stream:
            stream = handler.root_stream.find_child(self.frame.dependent_stream).add_child(stream_id)

        stream.weight = self.frame.weight


    @staticmethod
    def FrameType():
        return FrameTypes.PRIORITY


class Respond2Header(RespondBase):
    async def respond(self, handler):
        """
        Opens a stream if the stream_identifier is not in the dict of stream.
        Appends the payload of the header to the current scope object.
        If the header frame indicates the end of stream, run the operating
        function, get the result and respond it to the client.
        """
        stream_id = self.frame.stream_identifier # to be shorten the description
        stream = handler.root_stream.find_child(stream_id)

        if not stream:
            # If stream is not found, that means a request to add a stream is received.
            stream = handler.root_stream.find_child(self.frame.stream_dependency).add_child(stream_id)
            stream.weight = self.frame.priority_weight

        stream.update_scope(headers=self.frame)
        
        if self.frame.end_stream:
            fn = handler.app(stream.scope)
            async def receive():
                return empty_event
            await fn(receive, handler.make_sender(stream_id))
            stream.frame = None


    @staticmethod
    def FrameType():
        return FrameTypes.HEADERS

class Respond2Data(RespondBase):

    @log(logger)
    async def respond(self, handler):
        """
        To parse the payload of a DATA frame, scope['content-type'] is required.
        If the frame indicates the end of stream, run the operating
        function, get the result and respond it to the client.
        """
        logger.debug(self.frame)
        stream_id = self.frame.stream_identifier # to be shorten the description
        stream = handler.find_stream(stream_id) # to be shorten the description

        stream.update_event(data=self.frame)
        
        logger.debug(f'is this frame the end of stream? {self.frame.end_stream}')

        if self.frame.end_stream:
            fn = handler.app(stream.scope)
            logger.debug(fn)

            async def receive():
                return stream.event

            await fn(receive, handler.make_sender(stream_id))
            stream.event = None


    @staticmethod
    def FrameType():
        return FrameTypes.DATA
