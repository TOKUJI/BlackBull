from functools import partial
import traceback

from ..protocol.stream import Stream
from ..protocol.frame import FrameTypes, SettingFrameFlags
from ..logger import get_logger_set, log

logger, _ = get_logger_set(__name__)


class RespondFactory:

    @staticmethod
    def create(frame):
        try:
            klass = RespondBase._registry[frame.FrameType()]
        except KeyError:
            raise ValueError(f"Unsupported FrameType: {frame.FrameType()}")
        return klass(frame)


class RespondBase:
    """
    Abstract base class of responsing classes. You can find every subclass
    (i.e. responsing classes) by accessing __subclasses__().
    """
    FRAME_TYPE = None
    _registry = {}

    def __init__(self, frame):
        self.frame = frame

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)

        # If FRAME_TYPE is None, we won't register it, as it's meant to be an abstract base class
        if cls.FRAME_TYPE is None:
            return

        # Check for duplicate FRAME_TYPE to avoid overwriting existing entries in the registry
        if cls.FRAME_TYPE in cls._registry:
            raise ValueError(f"Duplicate FRAME_TYPE: {cls.FRAME_TYPE}")

        # Register the subclass in the registry based on its FRAME_TYPE
        # This allows us to create instances of the correct subclass based on the frame type
        cls._registry[cls.FRAME_TYPE] = cls

    async def respond(self, handler):
        raise NotImplementedError()

    @classmethod
    def FrameType(cls):
        if cls.FRAME_TYPE is None:
            raise NotImplementedError()
        return cls.FRAME_TYPE


class Respond2Ping(RespondBase):
    async def respond(self, handler):
        res = handler.factory.create(
            FrameTypes.PING,
            SettingFrameFlags.ACK,
            self.frame.stream_id,
            data=self.frame.payload,
            )
        await handler.send_frame(res)

    FRAME_TYPE = FrameTypes.PING


class Respond2WindowUpdate(RespondBase):
    async def respond(self, handler):
        increment = self.frame.window_size
        if self.frame.stream_id == 0:
            # Connection-level: credit all cached stream senders.
            for sender in handler._senders.values():
                sender.connection_window_size += increment
                sender._window_open.set()
        else:
            sender = handler.make_sender(self.frame.stream_id)
            sender.window_update(increment)

    FRAME_TYPE = FrameTypes.WINDOW_UPDATE


class Respond2Settings(RespondBase):
    async def respond(self, handler):
        if self.frame.flags == SettingFrameFlags.INIT:
            if hasattr(self.frame, 'initial_window_size') and self.frame.initial_window_size is not None:
                for sender in handler._senders.values():
                    sender.apply_settings(self.frame.initial_window_size)
            if hasattr(self.frame, 'header_table_size'):
                # TODO: update header_table_size
                pass
            await handler.send_frame(handler.factory.settings(ack=True))

        elif self.frame.flags == SettingFrameFlags.ACK:
            logger.debug('Got ACK. Do nothing.')

    FRAME_TYPE = FrameTypes.SETTINGS


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

    FRAME_TYPE = FrameTypes.PRIORITY


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

    FRAME_TYPE = FrameTypes.RST_STREAM

