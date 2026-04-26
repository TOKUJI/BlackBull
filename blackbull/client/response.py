"""Client-side responders: react to incoming HTTP/2 frames.

Each ``Responder`` subclass handles one ``FrameTypes`` value.  ``respond()``
delegates the protocol-level state mutation back to the owning
``HTTP2Client`` via underscore-prefixed callbacks (``_on_response_headers``,
``_on_response_data``, …) so the client owns its state and the responders
stay thin dispatchers.

The dispatch table is built once via ``__init_subclass__`` so
``ResponderFactory.create(frame)`` is O(1).
"""
from ..protocol.frame import FrameTypes, PingFrameFlags, SettingFrameFlags
from ..logger import get_logger_set

logger, _ = get_logger_set('client.response')


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
    """Drops frame types the client does not handle (PRIORITY, PRIORITY_UPDATE,
    PUSH_PROMISE, CONTINUATION until reassembly is added) without raising.

    Inherits from ``Responder`` but keeps ``FRAME_TYPE = None`` so
    ``__init_subclass__`` skips registration — this class is only ever
    created directly by ``ResponderFactory.create`` as the fallback.
    """

    async def respond(self, client) -> None:
        logger.debug('Unhandled frame type %r dropped', self.frame.FrameType())


class HeaderResponder(Responder):
    FRAME_TYPE = FrameTypes.HEADERS

    async def respond(self, client) -> None:
        client._on_response_headers(self.frame)


class DataResponder(Responder):
    FRAME_TYPE = FrameTypes.DATA

    async def respond(self, client) -> None:
        client._on_response_data(self.frame)


class PingResponder(Responder):
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
    FRAME_TYPE = FrameTypes.SETTINGS

    async def respond(self, client) -> None:
        if self.frame.flags & SettingFrameFlags.ACK:
            return  # ACK to our SETTINGS — nothing to do
        if getattr(self.frame, 'initial_window_size', None) is not None:
            client._on_initial_window_size(self.frame.initial_window_size)
        ack = client._factory.create(FrameTypes.SETTINGS, SettingFrameFlags.ACK, 0)
        await client._send_raw_frame(ack)


class WindowUpdateResponder(Responder):
    FRAME_TYPE = FrameTypes.WINDOW_UPDATE

    async def respond(self, client) -> None:
        client._on_window_update(self.frame)


class GoAwayResponder(Responder):
    FRAME_TYPE = FrameTypes.GOAWAY

    async def respond(self, client) -> None:
        client._on_goaway(self.frame)


class RstStreamResponder(Responder):
    FRAME_TYPE = FrameTypes.RST_STREAM

    async def respond(self, client) -> None:
        client._on_rst_stream(self.frame)
