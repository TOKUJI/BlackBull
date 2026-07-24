from ..protocol.stream import StreamState
from ..connection import Connection
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
            # Connection-level: credit the ONE shared connection window (bug
            # 1.2), then wake every blocked stream sender so they re-check.
            conn_window = handler._conn_window
            new_window = conn_window.size + increment
            # RFC 9113 §6.9.1 — exceeding 2^31-1 is a connection-level
            # FLOW_CONTROL_ERROR with GOAWAY.
            if new_window > self._MAX_FLOW_WINDOW:
                await handler._connection_error(
                    ErrorCodes.FLOW_CONTROL_ERROR,
                    f'connection flow window overflow: {new_window}')
                return
            conn_window.size = new_window
            for sender in handler._senders.values():
                sender.wake_window()
        else:
            sender = handler.make_sender(self.frame.stream_id)
            sid = self.frame.stream_id
            current = sender.stream_window_size
            if current + increment > self._MAX_FLOW_WINDOW:
                # RFC 9113 §6.9.1 — stream-level flow window overflow is a
                # stream error of type FLOW_CONTROL_ERROR.
                await handler.send_frame(handler.factory.rst_stream(
                    sid, ErrorCodes.FLOW_CONTROL_ERROR))
                return
            sender.window_update(increment)

    FRAME_TYPE = FrameTypes.WINDOW_UPDATE


class SettingsResponder(Responder):
    # RFC 9113 §6.5.2 — SETTINGS parameter ranges.
    _MAX_FLOW_WINDOW = 2**31 - 1           # SETTINGS_INITIAL_WINDOW_SIZE
    _MIN_MAX_FRAME_SIZE = 16384            # SETTINGS_MAX_FRAME_SIZE lower bound
    _MAX_MAX_FRAME_SIZE = 16777215         # SETTINGS_MAX_FRAME_SIZE upper bound (2^24-1)

    async def respond(self, handler):
        # RFC 9113 §6.5 — SETTINGS MUST be sent on stream 0.
        if self.frame.stream_id != 0:
            await handler._connection_error(
                ErrorCodes.PROTOCOL_ERROR,
                f'SETTINGS with stream_id={self.frame.stream_id}')
            return

        if self.frame.flags & SettingFrameFlags.ACK:
            # RFC 9113 §6.5 — SETTINGS ACK MUST have a length of 0.
            if self.frame.length != 0:
                await handler._connection_error(
                    ErrorCodes.FRAME_SIZE_ERROR,
                    f'SETTINGS ACK with length={self.frame.length}')
                return
            logger.debug('Got ACK. Do nothing.')
            return

        # INIT branch
        # RFC 9113 §6.5 — length MUST be a multiple of 6 octets.
        if self.frame.length % 6 != 0:
            await handler._connection_error(
                ErrorCodes.FRAME_SIZE_ERROR,
                f'SETTINGS length not multiple of 6: {self.frame.length}')
            return

        # RFC 9113 §6.5.2 — parameter range validation.
        ep = getattr(self.frame, 'enable_push', None)
        if ep is not None and ep not in (0, 1):
            await handler._connection_error(
                ErrorCodes.PROTOCOL_ERROR,
                f'SETTINGS_ENABLE_PUSH invalid value {ep}')
            return

        iws = getattr(self.frame, 'initial_window_size', None)
        if iws is not None and iws > self._MAX_FLOW_WINDOW:
            await handler._connection_error(
                ErrorCodes.FLOW_CONTROL_ERROR,
                f'SETTINGS_INITIAL_WINDOW_SIZE overflow: {iws}')
            return

        mfs = getattr(self.frame, 'max_frame_size', None)
        if mfs is not None and (mfs < self._MIN_MAX_FRAME_SIZE or mfs > self._MAX_MAX_FRAME_SIZE):
            await handler._connection_error(
                ErrorCodes.PROTOCOL_ERROR,
                f'SETTINGS_MAX_FRAME_SIZE out of range: {mfs}')
            return

        # RFC 9113 §6.9.2 — when SETTINGS_INITIAL_WINDOW_SIZE changes mid-
        # connection, adjust every active stream's send-window by the delta
        # (new − old).  This can drive an already-drained window negative,
        # which is required behaviour: subsequent WINDOW_UPDATE credits add
        # to the negative value before any further DATA may be sent.  New
        # streams (created after this SETTINGS) start at the new IWS, so we
        # update the handler-level value after applying the delta.
        if iws is not None:
            old_iws = handler._peer_initial_window_size
            delta = iws - old_iws
            handler._peer_initial_window_size = iws
            if delta != 0:
                for sender in handler._senders.values():
                    sender.adjust_initial_window(delta)
        if mfs is not None:
            for sender in handler._senders.values():
                sender.apply_settings(max_frame_size=mfs)
        # NOTE: per RFC 7540 §6.5.2, peer's SETTINGS_HEADER_TABLE_SIZE
        # constrains OUR encoder's table, not OUR decoder's. Updating the
        # decoder here would (and did) trip hpack's InvalidTableSizeError
        # because the peer's encoder never asked us to resize.
        await handler.send_frame(handler.factory.settings(ack=True))

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
            # RFC 9113 §5.3.1 — a dependency on an unknown (or pruned-as-closed)
            # stream is treated as a dependency on stream 0.  Previously
            # find_child returned None and .add_child() raised AttributeError,
            # unwinding the frame loop and killing every stream on the
            # connection without a GOAWAY (bug 1.10).
            parent = handler.root_stream.find_child(self.frame.dependent_stream)
            if parent is None:
                parent = handler.root_stream
            stream = parent.add_child(stream_id)

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
            # Reflect a late PRIORITY_UPDATE onto an already-dispatched request.
            # Native path: the Connection's H/2 priority extension. Compat lane:
            # the ASGI scope dict (incl. the ``http2_priority`` deprecation alias).
            target = stream.conn
            if isinstance(target, Connection):
                ext = target.extensions
                if ext is not None:
                    ext['http.response.priority'] = hint
            elif target is not None:
                target['http2_priority'] = hint
                ext = target.get('extensions')
                if ext is not None:
                    ext['http.response.priority'] = hint


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
        # Cancel the running handler for this stream.  Without this a
        # server-streaming handler abandoned by the client blocks forever in the
        # sender's flow-control wait (no further WINDOW_UPDATE will ever arrive),
        # permanently holding a max_concurrent_streams slot — a high-churn
        # streaming client leaks slots until new streams are REFUSED_STREAM'd.
        # Cancellation cleanly interrupts the flow-control wait; the handler's
        # finally/aclose still runs, and the done-callback frees the slot.
        task = handler._stream_tasks.get(stream_id)
        if task is not None and not task.done():
            task.cancel()
        # Prune the stream node and record it as closed-via-RST.  Later frames
        # from the peer on this identifier are detected by the frame-loop's
        # _closed_streams check and trigger STREAM_CLOSED.
        handler.root_stream.children.pop(stream_id, None)
        handler._mark_closed(stream_id, via_rst=True)
        handler._senders.pop(stream_id, None)
        released = handler._recipients.pop(stream_id, None)
        if released is not None:
            # Consume-based crediting: DATA the cancelled handler never read
            # was debited from the shared connection window — replay the
            # balance to stream 0 so later streams aren't starved.
            handler._release_recipient_credit(released)

    FRAME_TYPE = FrameTypes.RST_STREAM

