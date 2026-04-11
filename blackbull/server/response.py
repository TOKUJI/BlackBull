from functools import partial
import traceback

from ..stream import Stream
from ..frame import FrameTypes, SettingFlags
from ..logger import get_logger_set, log

logger, _ = get_logger_set(__name__)


class RespondFactory:
    @classmethod
    def create(cls, frame):
        factory = {klass.FrameType(): klass for klass in RespondBase.__subclasses__()}
        logger.debug(f'Factory mapping: {factory}')
        if frame.FrameType() not in factory:
            logger.warning(f'Unsupported frame type: {frame.FrameType()}')
            return None
        return factory[frame.FrameType()](frame)


class RespondBase:
    """
    Abstract base class of responsing classes. You can find every subclass
    (i.e. responsing classes) by accessing __subclasses__().
    """
    def __init__(self, frame):
        self.frame = frame

    async def respond(self, handler):
        raise NotImplementedError()

    @classmethod
    def FrameType(cls):
        raise NotImplementedError()


class Respond2Ping(RespondBase):
    async def respond(self, handler):
        res = handler.factory.create(
            FrameTypes.PING,
            SettingFlags.ACK,
            self.frame.stream_id,
            data=self.frame.payload,
            )
        await handler.send_frame(res)

    @classmethod
    def FrameType(cls):
        return FrameTypes.PING


class Respond2WindowUpdate(RespondBase):
    async def respond(self, handler):
        if self.frame.stream_id == 0:
            handler.client_window_size = self.frame.window_size
        else:
            handler.client_stream_window_size[self.frame.stream_id] = self.frame.window_size

    @classmethod
    def FrameType(cls):
        return FrameTypes.WINDOW_UPDATE


class Respond2Settings(RespondBase):
    async def respond(self, handler):
        if self.frame.flags == SettingFlags.INIT:
            if hasattr(self.frame, 'initial_window_size'):
                handler.initial_window_size = self.frame.initial_window_size
            if hasattr(self.frame, 'header_table_size'):
                # TODO: update header_table_size
                pass
            res = handler.factory.create(FrameTypes.SETTINGS,
                                         SettingFlags.ACK,
                                         self.frame.stream_id)
            await handler.send_frame(res)

        elif self.frame.flags == SettingFlags.ACK:
            logger.debug('Got ACK. Do nothing.')

    @classmethod
    def FrameType(cls):
        return FrameTypes.SETTINGS


class Respond2Priority(RespondBase):
    async def respond(self, handler):
        if self.frame.exclusion:
            for x in handler.root_stream.get_children():
                if x.parent == self.frame.dependent_stream:
                    x.parent = self.frame.stream_id

        stream_id = self.frame.stream_id  # to be shorten the description
        stream = handler.root_stream.find_child(stream_id)

        if not stream:
            stream = handler.root_stream.find_child(self.frame.dependent_stream).add_child(stream_id)

        stream.weight = self.frame.weight

    @classmethod
    def FrameType(cls):
        return FrameTypes.PRIORITY


class Respond2RstStream(RespondBase):

    @log(logger)
    async def respond(self, handler):
        """
        To terminate current stream and record the reason in the log
        """
        logger.debug(self.frame)
        stream_id = self.frame.stream_id  # to be shorten the description
        stream = handler.find_stream(stream_id)  # to be shorten the description

        logger.warning(f'stream_id = {stream_id}, {self.frame.error_code}')
        stream.close()

    @classmethod
    def FrameType(cls):
        return FrameTypes.RST_STREAM

