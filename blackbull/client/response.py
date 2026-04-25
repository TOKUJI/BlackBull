from ..protocol.frame import FrameTypes, SettingFrameFlags
from ..logger import get_logger_set, log
logger, _ = get_logger_set('client.response')


class ResponderFactory:
    @staticmethod
    def create(frame):
        factory = {klass.FrameType(): klass for klass in Responder.__subclasses__()}
        return factory[frame.FrameType()](frame)


class Responder:
    def __init__(self, frame):
        self.frame = frame

    async def respond(self, handler):
        raise NotImplementedError()

    @staticmethod
    def FrameType():
        raise NotImplementedError()


class PingResponder(Responder):
    async def respond(self, handler):
        res = handler.factory.create(FrameTypes.PING,
                                     SettingFrameFlags.ACK,
                                     self.frame.stream_id,
                                     self.frame.payload)
        await handler.send_frame(res)
        return handler

    @staticmethod
    def FrameType():
        return FrameTypes.PING


class WindowUpdateResponder(Responder):
    async def respond(self, handler):
        if self.frame.stream_id == 0:
            handler.connection_window_size = self.frame.window_size
        else:
            handler.stream_window_size[self.frame.stream_id] = self.frame.window_size

    @staticmethod
    def FrameType():
        return FrameTypes.WINDOW_UPDATE


class SettingsResponder(Responder):
    async def respond(self, handler):
        res = handler.factory.create(FrameTypes.SETTINGS,
                                     SettingFrameFlags.ACK,
                                     self.frame.stream_id,
                                     )
        await handler.send_frame(res)
        if self.frame.flags == SettingFrameFlags.INIT:
            if hasattr(self.frame, 'initial_window_size'):
                handler.initial_window_size = self.frame.initial_window_size
            if hasattr(self.frame, 'header_table_size'):
                # TODO: update header_table_size
                pass
            res = handler.factory.create(FrameTypes.SETTINGS,
                                         SettingFrameFlags.ACK,
                                         self.frame.stream_id)
            await handler.send_frame(res)

        elif self.frame.flags == SettingFrameFlags.ACK:
            logger.debug('Got ACK. Do nothing.')

    @staticmethod
    def FrameType():
        return FrameTypes.SETTINGS


class DataResponder(Responder):
    @log(logger)
    async def respond(self, handler):
        """
        To parse the payload of a DATA frame, scope['content-type'] is required.
        If the frame indicates the end of stream, run the operating
        function, get the result and respond it to the client.
        """
        logger.debug(self.frame)
        stream_id = self.frame.stream_id  # to be shorten the description

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


class HeaderResponder(Responder):
    async def respond(self, handler):
        """
        Open a stream if the stream_id is not in the dict of stream.
        Append the payload of the header to the current scope object.
        If the header frame indicates the end of stream, run the operating
        function, get the result and respond it to the client.
        """
        stream_id = self.frame.stream_id  # to be shorten the description
        stream = handler.find_stream(stream_id)
        if not stream:
            stream = handler.create_stream(stream_id)

        logger.debug(stream.scope)
        stream.update_scope(headers=self.frame)
        logger.debug(stream.scope)

        if self.frame.end_stream:
            async def receive():  # TODO: implement properly
                return {}

            async def send(event):  # TODO: implement properly
                pass

            await handler.app(stream.scope, receive, send)

            handler.streams[stream_id].frame = None

    @staticmethod
    def FrameType():
        return FrameTypes.HEADERS
