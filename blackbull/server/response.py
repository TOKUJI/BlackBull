from functools import partial
import traceback

from ..protocol.stream import Stream, StreamState
from ..protocol.frame_types import (
    ErrorCodes, FrameTypes, PingFrameFlags, SettingFrameFlags,
)
import logging
from ..logger import log

logger = logging.getLogger(__name__)


class ResponderFactory:

    @staticmethod
    def create(frame):
        try:
            klass = Responder._registry[frame.FrameType()]
        except KeyError:
            raise ValueError(f"Unsupported FrameType: {frame.FrameType()}")
        return klass(frame)


class Responder:
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


class PingResponder(Responder):
    async def respond(self, handler):
        # RFC 9113 §6.7 — PING with non-zero stream identifier is a connection
        # error of type PROTOCOL_ERROR.
        if self.frame.stream_id != 0:
            await handler._connection_error(
                ErrorCodes.PROTOCOL_ERROR,
                f'PING with stream_id={self.frame.stream_id}')
            return
        # RFC 9113 §6.7 — a PING with ACK set is a response to our PING;
        # we MUST NOT respond with another ACK.  Just drop it.
        if self.frame.flags & PingFrameFlags.ACK:
            logger.debug('PING ACK received — no response sent')
            return
        res = handler.factory.create(
            FrameTypes.PING,
            PingFrameFlags.ACK,
            self.frame.stream_id,
            data=self.frame.payload,
            )
        await handler.send_frame(res)

    FRAME_TYPE = FrameTypes.PING


class WindowUpdateResponder(Responder):
    # RFC 9113 §6.9.1 — flow-control windows are 31-bit; values above this
    # cause a FLOW_CONTROL_ERROR.
    _MAX_FLOW_WINDOW = 2**31 - 1

    async def respond(self, handler):
        # RFC 9113 §6.9 — a WINDOW_UPDATE frame with a length other than 4
        # octets is a connection error of type FRAME_SIZE_ERROR.
        if self.frame.length != 4:
            await handler._connection_error(
                ErrorCodes.FRAME_SIZE_ERROR,
                f'WINDOW_UPDATE length={self.frame.length}')
            return

        increment = self.frame.window_size
        # RFC 9113 §6.9 — increment of 0 is a PROTOCOL_ERROR.  On stream 0 it
        # is a connection error; on a stream it is a stream error.
        if increment == 0:
            if self.frame.stream_id == 0:
                await handler._connection_error(
                    ErrorCodes.PROTOCOL_ERROR,
                    'WINDOW_UPDATE increment 0 on connection')
            else:
                await handler.send_frame(handler.factory.rst_stream(
                    self.frame.stream_id, ErrorCodes.PROTOCOL_ERROR))
            return

        if self.frame.stream_id == 0:
            # Connection-level: credit all cached stream senders and update the
            # running total so future senders start with the current budget.
            new_window = getattr(handler, '_connection_window_size', 65535) + increment
            # RFC 9113 §6.9.1 — exceeding 2^31-1 is a connection-level
            # FLOW_CONTROL_ERROR with GOAWAY.
            if new_window > self._MAX_FLOW_WINDOW:
                await handler._connection_error(
                    ErrorCodes.FLOW_CONTROL_ERROR,
                    f'connection flow window overflow: {new_window}')
                return
            handler._connection_window_size = new_window
            for sender in handler._senders.values():
                sender.connection_window_size += increment
                sender.wake_window()
        else:
            sender = handler.make_sender(self.frame.stream_id)
            sid = self.frame.stream_id
            current = sender.stream_window_size.get(sid, 65535)
            if current + increment > self._MAX_FLOW_WINDOW:
                # RFC 9113 §6.9.1 — stream-level flow window overflow is a
                # stream error of type FLOW_CONTROL_ERROR.
                await handler.send_frame(handler.factory.rst_stream(
                    sid, ErrorCodes.FLOW_CONTROL_ERROR))
                return
            sender.window_update(increment)

    FRAME_TYPE = FrameTypes.WINDOW_UPDATE


class SettingsResponder(Responder):
    async def respond(self, handler):
        if self.frame.flags == SettingFrameFlags.INIT:
            iws = getattr(self.frame, 'initial_window_size', None)
            mfs = getattr(self.frame, 'max_frame_size', None)
            # Store peer's announced window so future stream senders start correctly.
            if iws is not None:
                handler._peer_initial_window_size = iws
            if iws is not None or mfs is not None:
                for sender in handler._senders.values():
                    sender.apply_settings(initial_window_size=iws, max_frame_size=mfs)
            # NOTE: per RFC 7540 §6.5.2, peer's SETTINGS_HEADER_TABLE_SIZE
            # constrains OUR encoder's table, not OUR decoder's. Updating the
            # decoder here would (and did) trip hpack's InvalidTableSizeError
            # because the peer's encoder never asked us to resize.
            await handler.send_frame(handler.factory.settings(ack=True))

        elif self.frame.flags == SettingFrameFlags.ACK:
            logger.debug('Got ACK. Do nothing.')

    FRAME_TYPE = FrameTypes.SETTINGS


class PriorityResponder(Responder):
    async def respond(self, handler):
        # RFC 9113 §5.3.1 — a stream cannot depend on itself; this is a
        # stream error of type PROTOCOL_ERROR.
        if self.frame.dependent_stream == self.frame.stream_id:
            await handler.send_frame(handler.factory.rst_stream(
                self.frame.stream_id, ErrorCodes.PROTOCOL_ERROR))
            return

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


class PriorityUpdateResponder(Responder):
    """RFC 9218 §7.1 — receive PRIORITY_UPDATE, log the hint, do not schedule."""
    FRAME_TYPE = FrameTypes.PRIORITY_UPDATE

    async def respond(self, handler) -> None:
        hint = self.frame.parsed_priority
        logger.debug(
            'PRIORITY_UPDATE stream=%d priority=%r parsed=%r',
            self.frame.prioritized_stream_id,
            self.frame.priority_field,
            hint,
        )
        stream = handler.find_stream(self.frame.prioritized_stream_id)
        if stream is None:
            # PRIORITY_UPDATE arrived before HEADERS — pre-create the stream
            # so the hint is available when HEADERS arrives later.
            handler.root_stream.add_child(self.frame.prioritized_stream_id)
            stream = handler.find_stream(self.frame.prioritized_stream_id)
        if stream is not None:
            stream.priority_hint = hint
            if stream.scope is not None:
                stream.scope['http2_priority'] = hint


class RstStreamResponder(Responder):

    @log
    async def respond(self, handler):
        """Terminate the named stream and log the reason.

        RFC 9113 §6.4 validation:
          - RST_STREAM with stream_id 0 is a connection PROTOCOL_ERROR.
          - RST_STREAM on a stream in the IDLE state is a connection
            PROTOCOL_ERROR.
        """
        stream_id = self.frame.stream_id
        if stream_id == 0:
            await handler._connection_error(
                ErrorCodes.PROTOCOL_ERROR,
                'RST_STREAM with stream_id 0')
            return

        stream = handler.find_stream(stream_id)
        # A stream that we have never seen HEADERS/PUSH_PROMISE on is in
        # the IDLE state (or doesn't exist in our tree yet — same thing).
        if stream is None or stream.state == StreamState.IDLE:
            await handler._connection_error(
                ErrorCodes.PROTOCOL_ERROR,
                f'RST_STREAM on idle stream {stream_id}')
            return

        logger.warning('stream_id=%d %s', stream_id, self.frame.error_code)
        # Keep the stream node in the tree but mark it closed-via-RST.  Later
        # frames from the peer on this identifier are detected by the
        # frame-loop state check and trigger STREAM_CLOSED (vs PROTOCOL_ERROR
        # for streams closed via END_STREAM).
        stream.on_rst_received()

    FRAME_TYPE = FrameTypes.RST_STREAM

