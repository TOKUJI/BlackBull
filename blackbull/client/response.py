from ..frame import FrameTypes
from ..logger import get_logger_set, log
logger, _ = get_logger_set('client.response')


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
                                     self.frame.payload)
        await handler.send_frame(res)
        return handler

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
        res = handler.factory.create(FrameTypes.SETTINGS,
                                     0x1,
                                     self.frame.stream_identifier,
                                     )
        await handler.send_frame(res)
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


class Respond2Data(RespondBase):
    @log(logger)
    async def respond(self, handler):
        """
        To parse the payload of a DATA frame, scope['content-type'] is required.
        If the frame indicates the end of stream, run the operating
        function, get the result and respond it to the client.
        """
        logger.debug(self.frame)
        stream_id = self.frame.stream_identifier  # to be shorten the description

        if stream := handler.find_stream(stream_id):
            pass
        else:
            stream = handler.create_stream(stream_id)

        stream.update_event(data=self.frame)

        if self.frame.end_stream:
            return stream.scope, stream.event

    @staticmethod
    def FrameType():
        return FrameTypes.DATA


class Respond2Header(RespondBase):
    async def respond(self, handler):
        """
        Open a stream if the stream_identifier is not in the dict of stream.
        Append the payload of the header to the current scope object.
        If the header frame indicates the end of stream, run the operating
        function, get the result and respond it to the client.
        """
        stream_id = self.frame.stream_identifier  # to be shorten the description
        stream = handler.find_stream(stream_id)
        if not stream:
            stream = handler.create_stream(stream_id)

        logger.debug(stream.scope)
        stream.update_scope(headers=self.frame)
        logger.debug(stream.scope)

        if self.frame.end_stream:
            await handler.app(stream.scope, receive, send)

            # async def receive():
            #     return empty_event
            # await fn(receive, handler.make_sender(stream_id))
            handler.streams[stream_id].frame = None

    @staticmethod
    def FrameType():
        return FrameTypes.HEADERS
