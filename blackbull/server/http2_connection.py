"""The HTTP/2 connection core (RFC 9113) that the server and the client share.

[`HTTP2Connection`][] admits frames by stream state, assembles field blocks,
answers SETTINGS, PING, WINDOW_UPDATE, PRIORITY, RST_STREAM and GOAWAY, and
decides the error scope.  A role subclasses it and supplies what a stream
means to it; the rules that differ by role are in the role policy section.
"""
import asyncio
import contextlib
import logging

from hpack import HPACKError

from ..env import get_settings
from ..protocol.frame_types import (DEFAULT_INITIAL_WINDOW_SIZE, DEFAULT_MAX_FRAME_SIZE,
                                    ErrorCodes, FrameFormatError, FrameTypes,
                                    HeaderFrameFlags, PingFrameFlags, SettingFrameFlags, peer_max_frame_size)
from ..protocol.stream import Stream, StreamState
from .cap_log import log_cap_hit
from .rate_window import RateWindow
from .sender import ConnectionWindow

logger = logging.getLogger(__name__)

#: Frames to be dropped after admission said so.
SKIP = object()

# How a stream closed, for §5.1's late frames.
_ENDED, _RESET_SENT, _RESET_RECEIVED = 0, 1, 2

_MAX_FLOW_WINDOW = 2**31 - 1        # §6.9.1, §6.5.2
_CLOSED_STREAMS_CAP = 1024

# §4.2: oversize is a connection error for these (and anything on stream 0).
_FRAME_SIZE_CONNECTION_ERROR_TYPES = frozenset({
    FrameTypes.HEADERS, FrameTypes.CONTINUATION, FrameTypes.PUSH_PROMISE,
    FrameTypes.SETTINGS})
# §6.1-6.4, §6.6, §6.10: never on stream 0.
_STREAM_ONLY_FRAME_TYPES = frozenset({
    FrameTypes.DATA, FrameTypes.HEADERS, FrameTypes.PRIORITY,
    FrameTypes.RST_STREAM, FrameTypes.PUSH_PROMISE, FrameTypes.CONTINUATION})
# §6.5, §6.7, §6.8, §6.9 with stream 0 handled per type.
_CONNECTION_ONLY_FRAME_TYPES = frozenset({
    FrameTypes.SETTINGS, FrameTypes.PING, FrameTypes.GOAWAY})
# §6.3, §6.5, §6.8, §6.9: fixed or bounded payload lengths.
_LENGTH_RULED_TYPES = frozenset({
    FrameTypes.PRIORITY, FrameTypes.WINDOW_UPDATE, FrameTypes.SETTINGS, FrameTypes.GOAWAY})


class HTTP2Connection:
    """One endpoint of an HTTP/2 connection, either role.

    A role sets ``_PEER_PARITY`` (the parity of the stream ids the peer opens)
    and implements the hooks: ``_send_control``, ``_reset``, ``_after_goaway``,
    ``_retire_all``, ``_peer_reset``, ``_peer_goaway``, ``_open_peer_stream``,
    ``_on_push_promise`` and ``_field_block_refused``.
    """

    #: 1 when the peer opens odd streams (the server's view), 0 otherwise.
    _PEER_PARITY = 1

    def _init_connection(self, factory, *, field_block_max: int,
                         field_block_cap: str) -> None:
        self.factory = factory
        self.root_stream = Stream(0, None, 1)
        self._senders: dict = {}
        self._conn_window = ConnectionWindow(DEFAULT_INITIAL_WINDOW_SIZE)
        self._peer_initial_window_size = DEFAULT_INITIAL_WINDOW_SIZE
        self._peer_enable_push = True
        # §5.1 late frames: stream id -> how it closed.  Bounded; an evicted
        # id stays CLOSED, as ended, through the high-water mark of its parity.
        self._closed_streams: dict[int, int] = {}
        self._closed_peer_high_water = 0
        self._closed_local_high_water = 0
        self._last_peer_stream_id = 0
        self._goaway_sent = False
        # §6.10: the one field block the peer may have open.
        self._open_block = None
        self._open_block_stream = None
        self._block_opened_at: float | None = None
        self._field_block_max = field_block_max
        self._field_block_cap = field_block_cap
        cfg = get_settings()
        rate, window = cfg.frame_rate_limit, cfg.frame_rate_window
        self._rst_meter = RateWindow(rate, window)          # CVE-2023-44487
        self._ping_meter = RateWindow(rate, window)         # CVE-2019-9512
        self._settings_meter = RateWindow(rate, window)     # CVE-2019-9515
        self._empty_frame_meter = RateWindow(rate, window)  # CVE-2019-9518

    # --- error scope (§5.4) ------------------------------------------------

    async def _connection_error(self, error_code: ErrorCodes, reason: str = '') -> None:
        """Send GOAWAY once and end the connection (§5.4.1)."""
        if self._goaway_sent:
            return
        logger.warning('HTTP/2 connection error %s: %s', error_code.name, reason)
        # Revoke every output owner before the GOAWAY write can suspend.
        self._goaway_sent = True
        self._retire_all()
        with contextlib.suppress(Exception):
            await self._send_control(
                self.factory.goaway(self._last_peer_stream_id, error_code))
        await self._after_goaway(reason)

    async def _undecodable(self, exc: Exception) -> None:
        """A frame or field block that cannot be read ends the connection (§4.3)."""
        if isinstance(exc, FrameFormatError):
            await self._connection_error(exc.error_code, str(exc))
        else:
            await self._connection_error(
                ErrorCodes.COMPRESSION_ERROR, f'could not decode the field block: {exc}')

    async def _meter(self, window: RateWindow, what: str) -> bool:
        """Count one frame; True when its budget is spent and the connection ends."""
        if not window.hit():
            return False
        log_cap_hit('frame_rate', requested=window.count, limit=window.limit,
                    protocol='http2')
        await self._connection_error(
            ErrorCodes.ENHANCE_YOUR_CALM,
            f'{what} rate limit exceeded ({window.count} in {window.window}s)')
        return True

    # --- closed streams (§5.1) ----------------------------------------------

    def _mark_closed(self, stream_id: int, via_rst: bool, *, by_peer: bool = False,
                     advances_watermark: bool = True) -> None:
        if not advances_watermark:
            # Our reset of a stream that never opened is no reason to ignore it later.
            self._closed_streams[stream_id] = _RESET_RECEIVED if by_peer else _ENDED
        else:
            # by_peer implies via_rst: _ENDED, _RESET_SENT or _RESET_RECEIVED.
            self._closed_streams[stream_id] = via_rst + by_peer
            if stream_id % 2 == self._PEER_PARITY:
                if stream_id > self._closed_peer_high_water:
                    self._closed_peer_high_water = stream_id
            elif stream_id > self._closed_local_high_water:
                self._closed_local_high_water = stream_id
        if len(self._closed_streams) > _CLOSED_STREAMS_CAP:
            del self._closed_streams[next(iter(self._closed_streams))]

    def _is_closed_stream(self, stream_id: int) -> tuple[bool, int | None]:
        """Closed membership, and how it closed when retained."""
        exact = self._closed_streams.get(stream_id)
        if exact is not None:
            return True, exact
        watermark = (self._closed_peer_high_water if stream_id % 2 == self._PEER_PARITY
                     else self._closed_local_high_water)
        return 0 < stream_id <= watermark, None

    # --- frame admission ----------------------------------------------------

    async def _load(self, data: bytes):
        """The frame in *data*, or ``None`` after ending the connection over it."""
        try:
            return self.factory.load(data)
        except Exception as exc:
            await self._unloadable(exc)
            return None

    async def _unloadable(self, exc: Exception) -> None:
        """End the connection over a frame ``factory.load`` raised *exc* for."""
        if isinstance(exc, (HPACKError, FrameFormatError)):
            self._field_block_refused(exc)
            await self._undecodable(exc)
        else:
            # Our bug, not the peer's: say so rather than blame its encoding.
            logger.error('HTTP/2 frame parse failed', exc_info=exc)
            await self._connection_error(ErrorCodes.INTERNAL_ERROR,
                                         f'failed to parse a frame: {exc!r}')

    async def _admit(self, frame):
        """The stream *frame* belongs to, ``None`` for a connection frame or an
        idle-stream PRIORITY, or [`SKIP`][] once the frame is answered."""
        frame_type = frame.FrameType()
        open_block = self._open_block
        if open_block is not None and frame_type is not FrameTypes.CONTINUATION:
            # §6.10: a block is atomic on the wire, unknown types included.
            name = frame_type.name if frame_type is not None else 'unknown'
            await self._connection_error(ErrorCodes.PROTOCOL_ERROR,
                                         f'expected CONTINUATION, got {name}')
            return SKIP
        if frame_type is None:
            return SKIP     # §5.5
        if frame_type is FrameTypes.RST_STREAM:
            if await self._meter(self._rst_meter, 'RST_STREAM'):
                return SKIP
        elif frame_type is FrameTypes.PING:
            if await self._meter(self._ping_meter, 'PING'):
                return SKIP
        elif frame_type is FrameTypes.SETTINGS:
            if await self._meter(self._settings_meter, 'SETTINGS'):
                return SKIP
        length = frame.length
        stream_id = frame.stream_id
        if length == 0 and frame_type in (FrameTypes.CONTINUATION, FrameTypes.DATA):
            if await self._meter(self._empty_frame_meter, f'empty {frame_type.name}'):
                return SKIP
        if length > DEFAULT_MAX_FRAME_SIZE:
            if frame_type in _FRAME_SIZE_CONNECTION_ERROR_TYPES or stream_id == 0:
                await self._connection_error(
                    ErrorCodes.FRAME_SIZE_ERROR,
                    f'{frame_type.name} length {length} > {DEFAULT_MAX_FRAME_SIZE}')
            else:
                await self._reset(stream_id, ErrorCodes.FRAME_SIZE_ERROR)
            return SKIP
        if stream_id == 0:
            if frame_type in _STREAM_ONLY_FRAME_TYPES:
                await self._connection_error(ErrorCodes.PROTOCOL_ERROR,
                                             f'{frame_type.name} with stream_id 0')
                return SKIP
        elif frame_type in _CONNECTION_ONLY_FRAME_TYPES:
            await self._connection_error(
                ErrorCodes.PROTOCOL_ERROR, f'{frame_type.name} with stream_id {stream_id}')
            return SKIP
        if frame_type is FrameTypes.CONTINUATION:
            if open_block is None:
                await self._connection_error(
                    ErrorCodes.PROTOCOL_ERROR,
                    'unexpected CONTINUATION without preceding HEADERS')
                return SKIP
            if stream_id != open_block.stream_id:
                await self._connection_error(
                    ErrorCodes.PROTOCOL_ERROR,
                    f'CONTINUATION stream {stream_id} does not match the open '
                    f'field block on stream {open_block.stream_id}')
                return SKIP
            return self._open_block_stream
        if ((frame_type in _LENGTH_RULED_TYPES
                or frame_type is FrameTypes.HEADERS and frame.flags & HeaderFrameFlags.PRIORITY)
                and not await self._check_length(frame, frame_type)):
            return SKIP
        if stream_id == 0:
            return None
        # §5.1: the stream by its state.
        if frame_type is FrameTypes.PUSH_PROMISE and not self._accepts_push_promise():
            await self._connection_error(
                ErrorCodes.PROTOCOL_ERROR, f'PUSH_PROMISE from a peer that may not push, on stream {stream_id}')
            return SKIP
        stream = self.root_stream.children.get(stream_id)
        if stream is None:
            is_closed, how = self._is_closed_stream(stream_id)
            if is_closed:
                return await self._late_frame(frame, frame_type, how or _ENDED)
            if frame_type is FrameTypes.HEADERS:
                return await self._open_peer_stream(frame)
            if frame_type is FrameTypes.PRIORITY:
                return None     # §6.3: legal on an idle stream, and creates nothing
            await self._connection_error(
                ErrorCodes.PROTOCOL_ERROR, f'frame {frame_type.name} on idle stream {stream_id}')
            return SKIP
        state = stream.state
        if state is StreamState.CLOSED:
            return await self._late_frame(frame, frame_type, _ENDED)
        if state is StreamState.IDLE:
            if frame_type is FrameTypes.HEADERS:
                # A PRIORITY_UPDATE may have reserved the node; HEADERS opens it.
                return await self._open_peer_stream(frame, stream)
            if frame_type is FrameTypes.PRIORITY or frame_type is FrameTypes.PUSH_PROMISE:
                return stream
            await self._connection_error(
                ErrorCodes.PROTOCOL_ERROR, f'frame {frame_type.name} on idle stream {stream_id}')
            return SKIP
        if state is StreamState.HALF_CLOSED_REMOTE and frame_type not in (
                FrameTypes.PRIORITY, FrameTypes.WINDOW_UPDATE, FrameTypes.RST_STREAM,
                FrameTypes.HEADERS):
            # A field section is refused after its HPACK updates, by the role.
            await self._reset(stream_id, ErrorCodes.STREAM_CLOSED,
                              additional_credit=self._flow_length(frame, frame_type))
            return SKIP
        return stream

    async def _check_length(self, frame, frame_type) -> bool:
        """The payload lengths of §4.2, §6.3, §6.5, §6.8 and §6.9."""
        length = frame.length
        if frame_type is FrameTypes.PRIORITY:
            if length != 5:
                await self._reset(frame.stream_id, ErrorCodes.FRAME_SIZE_ERROR)
                return False
        elif frame_type is FrameTypes.WINDOW_UPDATE:
            if length != 4:
                await self._connection_error(ErrorCodes.FRAME_SIZE_ERROR,
                                             f'WINDOW_UPDATE length={length}')
                return False
        elif frame_type is FrameTypes.SETTINGS:
            if frame.flags & SettingFrameFlags.ACK:
                if length:
                    await self._connection_error(ErrorCodes.FRAME_SIZE_ERROR,
                                                 f'SETTINGS ACK with length={length}')
                    return False
            elif length % 6:
                await self._connection_error(
                    ErrorCodes.FRAME_SIZE_ERROR, f'SETTINGS length not multiple of 6: {length}')
                return False
        elif frame_type is FrameTypes.HEADERS and frame.flags & HeaderFrameFlags.PRIORITY:
            # §4.2: too short for its priority fields; a field block, so the connection.
            if length < 5 + bool(frame.flags & HeaderFrameFlags.PADDED):
                await self._connection_error(
                    ErrorCodes.FRAME_SIZE_ERROR,
                    f'HEADERS length {length} cannot hold its priority fields')
                return False
        elif frame_type is FrameTypes.GOAWAY and length < 8:
            await self._connection_error(ErrorCodes.FRAME_SIZE_ERROR,
                                         f'GOAWAY length={length}')
            return False
        return True

    async def _late_frame(self, frame, frame_type, how: int):
        """§5.1: a frame on a CLOSED stream, by how it closed."""
        if frame_type is FrameTypes.PRIORITY:
            return None
        if frame_type is FrameTypes.RST_STREAM:
            return SKIP         # §5.4.2: never answered in kind
        if how == _RESET_SENT:
            # Frames the peer sent before our RST_STREAM reached it are
            # ignored; DATA still bought connection credit, and a field block
            # is still decoded for the connection-wide table (§4.3).
            if frame_type is FrameTypes.HEADERS and not frame.end_headers:
                await self._open(frame, None)
            credit = self._flow_length(frame, frame_type)
            if credit:
                await self._return_connection_credit(credit)
            return SKIP
        if frame_type is FrameTypes.HEADERS:
            await self._connection_error(
                ErrorCodes.STREAM_CLOSED, f'HEADERS on closed stream {frame.stream_id}')
            return SKIP
        if frame_type is FrameTypes.WINDOW_UPDATE and how == _ENDED:
            return SKIP         # docs/about/rfc9113-implementation.md §5.1
        # DATA still bought connection credit; the stream's own is gone.
        await self._reset(frame.stream_id, ErrorCodes.STREAM_CLOSED,
                          additional_credit=self._flow_length(frame, frame_type))
        return SKIP

    @staticmethod
    def _flow_length(frame, frame_type) -> int:
        return frame.length if frame_type is FrameTypes.DATA else 0

    # --- field blocks (§4.3, §6.10) -------------------------------------------

    async def _assemble(self, frame, stream):
        """The decoded field block *frame* completes, or ``None`` while open or
        refused.  *frame* is a CONTINUATION or an opening frame without
        END_HEADERS."""
        open_block = self._open_block
        if open_block is None:
            await self._open(frame, stream)
            return None
        open_block.raw_block += frame.payload
        if await self._bound_block(open_block) is None:
            return None
        if not frame.end_headers:
            return None
        self._open_block = None
        self._open_block_stream = None
        self._block_opened_at = None
        try:
            open_block.parse_payload()
        except (HPACKError, FrameFormatError) as exc:
            # §4.3: hpack may have applied part of the block already.
            self._field_block_refused(exc)
            await self._undecodable(exc)
            return None
        return open_block

    async def _open(self, frame, stream) -> None:
        """Open the field block *frame* starts, on *stream* or on none."""
        self._open_block = frame
        self._open_block_stream = stream
        self._block_opened_at = asyncio.get_running_loop().time()
        self._on_block_opened()
        await self._bound_block(frame)

    async def _bound_block(self, block):
        """Cap the undecoded block by the head total: excess load, so
        ENHANCE_YOUR_CALM (§10.5), before the decoder sees it."""
        limit = self._field_block_max
        seen = len(block.raw_block)
        if limit and seen > limit:
            log_cap_hit(self._field_block_cap, requested=seen, limit=limit,
                        protocol='http2')
            await self._connection_error(
                ErrorCodes.ENHANCE_YOUR_CALM,
                f'undecoded field block exceeded {self._field_block_cap}={limit}')
            return None
        return block

    # --- control frames -------------------------------------------------------

    async def on_ping(self, frame) -> None:
        """§6.7: answer with the ACK; an ACK is the answer to ours."""
        if frame.flags & PingFrameFlags.ACK:
            return
        await self._send_control(self.factory.create(
            FrameTypes.PING, PingFrameFlags.ACK, 0, data=frame.payload))

    async def on_settings(self, frame) -> None:
        """§6.5: validate every parameter, then apply them and ACK."""
        if frame.flags & SettingFrameFlags.ACK:
            self._settings_acknowledged()
            return
        enable_push = getattr(frame, 'enable_push', None)
        if enable_push is not None and enable_push not in self._ENABLE_PUSH_VALUES:
            await self._connection_error(ErrorCodes.PROTOCOL_ERROR,
                                         f'SETTINGS_ENABLE_PUSH invalid value {enable_push}')
            return
        window = getattr(frame, 'initial_window_size', None)
        if window is not None and window > _MAX_FLOW_WINDOW:
            await self._connection_error(ErrorCodes.FLOW_CONTROL_ERROR,
                                         f'SETTINGS_INITIAL_WINDOW_SIZE overflow: {window}')
            return
        try:
            max_frame = peer_max_frame_size(frame.settings)
        except FrameFormatError as error:
            await self._connection_error(error.error_code, str(error))
            return
        if enable_push is not None:
            self._peer_enable_push = bool(enable_push)
        if window is not None:
            self._set_peer_initial_window(window)
        if max_frame is not None:
            self.factory.peer_max_frame_size = max_frame
        self.factory.apply_peer_header_table_size(frame.settings)
        await self._send_control(self.factory.settings(ack=True))

    def _set_peer_initial_window(self, window: int) -> None:
        """§6.9.2: open streams move by the delta, possibly below zero."""
        delta = window - self._peer_initial_window_size
        self._peer_initial_window_size = window
        if delta:
            for sender in self._senders.values():
                sender.adjust_initial_window(delta)

    async def on_window_update(self, frame) -> None:
        """§6.9: credit the connection or one stream; zero and overflow are
        errors at the level they arrived on."""
        increment = frame.window_size
        stream_id = frame.stream_id
        if stream_id == 0:
            if increment == 0:
                await self._connection_error(ErrorCodes.PROTOCOL_ERROR,
                                             'WINDOW_UPDATE increment 0 on connection')
                return
            window = self._conn_window.size + increment
            if window > _MAX_FLOW_WINDOW:
                await self._connection_error(ErrorCodes.FLOW_CONTROL_ERROR,
                                             f'connection flow window overflow: {window}')
                return
            self._conn_window.size = window
            for sender in self._senders.values():
                sender.wake_window()
            return
        if increment == 0:
            await self._reset(stream_id, ErrorCodes.PROTOCOL_ERROR)
            return
        sender = self._stream_sender(stream_id)
        if sender is None:
            # Nothing left to send, but the window is still the peer's to overflow.
            stream = self.root_stream.children.get(stream_id)
            if stream is None:
                return
            window = getattr(stream, 'window_size', self._peer_initial_window_size) + increment
            if window > _MAX_FLOW_WINDOW:
                await self._reset(stream_id, ErrorCodes.FLOW_CONTROL_ERROR)
                return
            stream.window_size = window
            return
        if sender.stream_window_size + increment > _MAX_FLOW_WINDOW:
            await self._reset(stream_id, ErrorCodes.FLOW_CONTROL_ERROR)
            return
        sender.window_update(increment)

    async def on_priority(self, frame) -> None:
        """§5.3.1: a stream cannot depend on itself."""
        if frame.dependent_stream == frame.stream_id:
            await self._reset(frame.stream_id, ErrorCodes.PROTOCOL_ERROR)

    async def on_rst_stream(self, frame, stream) -> None:
        """§6.4: an RST_STREAM on an idle stream is a connection error."""
        if stream is None or stream.state is StreamState.IDLE:
            await self._connection_error(ErrorCodes.PROTOCOL_ERROR,
                                         f'RST_STREAM on idle stream {frame.stream_id}')
            return
        self._peer_reset(frame.stream_id, frame.error_code)

    # --- role policy ------------------------------------------------------------
    # The server's view; the client overrides where RFC 9113 makes it differ.

    #: §6.5.2: a client may announce either; a server MUST NOT announce 1.
    _ENABLE_PUSH_VALUES = (0, 1)

    def _accepts_push_promise(self) -> bool:
        """§8.4: only a server pushes."""
        return False

    def _settings_acknowledged(self) -> None:
        pass

    def _on_block_opened(self) -> None:
        pass

    def _field_block_refused(self, exc: Exception) -> None:
        pass

    async def _return_connection_credit(self, n: int) -> None:
        raise NotImplementedError

    def _stream_sender(self, stream_id: int):
        raise NotImplementedError
