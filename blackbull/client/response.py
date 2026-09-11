"""Client-side responders: react to incoming HTTP/2 frames.

Each ``Responder`` subclass handles one ``FrameTypes`` value and mutates no
protocol state of its own — it hands the frame back to the owning
``HTTP2Client``, which is what keeps that state in one place.
"""
from ..protocol.frame_types import FrameTypes, PingFrameFlags, SettingFrameFlags
import logging

logger = logging.getLogger(__name__)


class ResponderFactory:
    """Looks up the ``Responder`` for an incoming frame type and instantiates it."""

    @staticmethod
    def create(frame) -> 'Responder':
        cls = Responder._registry.get(frame.FrameType())
        if cls is None:
            return _NullResponder(frame)
        return cls(frame)


class Responder:
    """Base class for client-side reactions to an incoming HTTP/2 frame.

    Subclasses set ``FRAME_TYPE`` and implement ``respond(client)``.
    ``__init_subclass__`` registers each concrete subclass keyed on its
    ``FRAME_TYPE`` so ``ResponderFactory.create`` can dispatch by type.
    """

    FRAME_TYPE: 'FrameTypes | None' = None
    _registry: 'dict[FrameTypes, type[Responder]]' = {}

    def __init__(self, frame):
        self.frame = frame

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        if cls.FRAME_TYPE is None:
            return
        if cls.FRAME_TYPE in cls._registry:
            raise ValueError(f'Duplicate FRAME_TYPE: {cls.FRAME_TYPE}')
        cls._registry[cls.FRAME_TYPE] = cls

    async def respond(self, client) -> None:
        raise NotImplementedError


class _NullResponder(Responder):
    """Drops frame types the client does not act on (PRIORITY,
    PRIORITY_UPDATE) without raising.

    Inherits from ``Responder`` but keeps ``FRAME_TYPE = None`` so
    ``__init_subclass__`` skips registration — this class is only ever
    created directly by ``ResponderFactory.create`` as the fallback.
    """

    async def respond(self, client) -> None:
        logger.debug('Unhandled frame type %r dropped', self.frame.FrameType())


class HeaderResponder(Responder):
    """HEADERS — the response head (or its trailers) for one stream."""

    FRAME_TYPE = FrameTypes.HEADERS

    async def respond(self, client) -> None:
        await client._on_response_headers(self.frame)


class DataResponder(Responder):
    """DATA — a body chunk, which the client also credits back to the
    flow-control window even when the stream is no longer tracked."""

    FRAME_TYPE = FrameTypes.DATA

    async def respond(self, client) -> None:
        await client._on_response_data(self.frame)


class PingResponder(Responder):
    """PING — answered with an ACK echoing the payload, unless it is itself
    the ACK to a ping this client sent."""

    FRAME_TYPE = FrameTypes.PING

    async def respond(self, client) -> None:
        if self.frame.flags & PingFrameFlags.ACK:
            return  # ACK to one of our own pings — nothing to do
        ack = client._factory.create(
            FrameTypes.PING, PingFrameFlags.ACK,
            self.frame.stream_id, data=self.frame.payload,
        )
        await client._send_raw_frame(ack)


class SettingsResponder(Responder):
    """SETTINGS — applies the peer's parameters and acknowledges them; an ACK
    from the peer instead completes this client's own settings exchange."""

    FRAME_TYPE = FrameTypes.SETTINGS

    async def respond(self, client) -> None:
        if self.frame.flags & SettingFrameFlags.ACK:
            client._on_settings_ack()
            return
        if getattr(self.frame, 'initial_window_size', None) is not None:
            client._on_initial_window_size(self.frame.initial_window_size)
        ack = client._factory.create(FrameTypes.SETTINGS, SettingFrameFlags.ACK, 0)
        await client._send_raw_frame(ack)


class PushPromiseResponder(Responder):
    """PUSH_PROMISE — dropped, but its field block arrives here decoded.

    RFC 9113 §4.3 requires the decode even for a frame to be discarded.  A
    responder never sees CONTINUATION: the client folds it into the frame
    that opened the block.
    """

    FRAME_TYPE = FrameTypes.PUSH_PROMISE

    async def respond(self, client) -> None:
        await client._on_push_promise(self.frame)


class WindowUpdateResponder(Responder):
    """WINDOW_UPDATE — flow-control credit, released to the connection or to
    one stream's sender depending on the frame's stream id."""

    FRAME_TYPE = FrameTypes.WINDOW_UPDATE

    async def respond(self, client) -> None:
        client._on_window_update(self.frame)


class GoAwayResponder(Responder):
    """GOAWAY — the peer is closing the connection; pending streams above the
    last-processed id it names are failed, since it never handled them."""

    FRAME_TYPE = FrameTypes.GOAWAY

    async def respond(self, client) -> None:
        client._on_goaway(self.frame)


class RstStreamResponder(Responder):
    """RST_STREAM — one stream was terminated by the peer; its waiter is
    failed while the connection carries on."""

    FRAME_TYPE = FrameTypes.RST_STREAM

    async def respond(self, client) -> None:
        client._on_rst_stream(self.frame)
