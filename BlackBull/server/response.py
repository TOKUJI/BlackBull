from functools import partial

from ..frame import FrameTypes, Stream
from ..logger import get_logger_set
logger, log = get_logger_set('server.response')

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
            for x in handler.streams:
                if x.parent == self.frame.dependent_stream:
                    x.parent = self.frame.stream_identifier

        if self.frame.stream_identifier not in handler.streams:
            handler.streams[self.frame.stream_identifier] = Stream(self.frame.stream_identifier,
                                          parent=self.frame.dependent_stream,
                                          weight=self.frame.weight,
                                          )
        else:
            handler.streams[self.frame.stream_identifier].weight = self.frame.weight


    @staticmethod
    def FrameType():
        return FrameTypes.PRIORITY


class Respond2Header(RespondBase):
    async def respond(self, handler):
        """
        Open a stream if the stream_identifier is not in the dict of stream.
        Append the payload of the header to the current scope object.
        If the header frame indicates the end of stream, run the operating
        function, get the result and respond it to the client.
        """
        stream_id = self.frame.stream_identifier # to be shorten the description

        if stream_id not in handler.streams:
            handler.streams[stream_id] = \
                Stream(stream_id,
                       parent=self.frame.stream_dependency,
                       weight=self.frame.priority_weight,
                       )
        logger.debug(handler.streams[stream_id].scope)

        handler.streams[stream_id].scope = \
            await handler.make_scope(
                headers=self.frame,
                scope=handler.streams[stream_id].scope
                )
        logger.debug(handler.streams[stream_id].scope)
        
        if self.frame.end_stream:
            fn = handler.app(handler.streams[stream_id].scope)
            async def receive():
                return empty_event
            await fn(receive, handler.make_sender(stream_id))
            handler.streams[stream_id].frame = None


    @staticmethod
    def FrameType():
        return FrameTypes.HEADERS

class Respond2Data(RespondBase):

    async def respond(self, handler):
        """
        Append the payload of the header to the current scope object.
        To parse the payload of a DATA frame, scope['content-type'] is required.
        If the frame indicates the end of stream, run the operating
        function, get the result and respond it to the client.
        """
        stream_id = self.frame.stream_identifier # to be shorten the description

        # TODO: Make event here, not scope.
        logger.debug(handler.streams[stream_id].event)
        handler.streams[stream_id].event = \
            await handler.make_event(
                data=self.frame,
                event=handler.streams[stream_id].event
                )
        logger.debug(handler.streams[stream_id].event)
        
        if self.frame.end_stream:
            fn = handler.app(handler.streams[stream_id].scope)
            async def receive():
                return handler.streams[stream_id].event
            # receive = partial(handler.make_event, data=self.frame)
            await fn(receive, handler.make_sender(stream_id))
            handler.streams[stream_id].event = None


    @staticmethod
    def FrameType():
        return FrameTypes.DATA
