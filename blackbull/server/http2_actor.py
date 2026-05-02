from typing import Awaitable, Callable
import logging
import asyncio
from dataclasses import dataclass, field

from ..actor import Actor, Message
from .sender import AbstractWriter, SenderFactory
from .recipient import AbstractReader, HTTP2Recipient
from ..event_aggregator import EventAggregator
from .headers import HeaderList, Headers
from ..protocol.frame import FrameBase, FrameFactory, FrameTypes
from ..logger import log

logger = logging.getLogger(__name__)


# Example Level A messages
@dataclass
class ConnectionAccepted(Message):
    reader: AbstractReader|None = field(default=None, compare=False, repr=False)
    writer: AbstractWriter|None = field(default=None, compare=False, repr=False)
    peername: tuple[str, int]|None = field(default=None, compare=False, repr=False)

@dataclass
class StreamHeadersReceived(Message):
    stream_id: int|None = field(default=None, compare=False, repr=False)
    headers: HeaderList|None = field(default=None, compare=False, repr=False)
    end_stream: bool|None = field(default=None, compare=False, repr=False)

@dataclass
class WindowUpdate(Message):       # ask-pattern reply
    stream_id: int|None = field(default=None, compare=False, repr=False)
    increment: int|None = field(default=None, compare=False, repr=False)

@dataclass
class Header(Message):
    stream_id: int|None = field(default=None, compare=False, repr=False)
    headers: HeaderList|None = field(default=None, compare=False, repr=False)
    end_stream: bool|None = field(default=None, compare=False, repr=False)

@dataclass
class Data(Message):
    stream_id: int|None = field(default=None, compare=False, repr=False)
    data: bytes|None = field(default=None, compare=False, repr=False)
    end_stream: bool|None = field(default=None, compare=False, repr=False)

@dataclass
class SettingsReceived(Message):
    settings: dict[int, int]|None = field(default=None, compare=False, repr=False)


@dataclass
class Goaway(Message):
    last_stream_id: int|None = field(default=None, compare=False, repr=False)
    error_code: int|None = field(default=None, compare=False, repr=False)
    debug_data: bytes|None = field(default=None, compare=False, repr=False)


@dataclass
class WindowRequested(Message):
    stream_id: int|None = field(default=None, compare=False, repr=False)
    increment: int|None = field(default=None, compare=False, repr=False)

def _convert_Frame_to_Message(frame: FrameBase) -> Message:
    return Message()




class HTTP2Actor(Actor):
    """Drives the HTTP/2 connection state machine for one connection.

    Supervisor strategy: propagate — framing errors send GOAWAY and raise,
    surfacing to the caller.
    """

    def __init__(
        self,
        reader: AbstractReader,
        writer: AbstractWriter,
        app: Callable[..., Awaitable[None]],
        aggregator: EventAggregator,
        *,
        peername: tuple[str, int] | None = None,
        sockname: tuple[str, int] | None = None,
        ssl: bool = False,
    ) -> None:
        super().__init__()
        self._reader = reader
        self._writer = writer
        self._app = app
        self._aggregator = aggregator
        self._peername = peername
        self._sockname = sockname
        self._ssl = ssl

        self.factory = FrameFactory()
        self._control_sender = SenderFactory.http2(self._writer, self.factory, 0)


    async def run(self) -> None:
        # Read frames from reader in a loop; for each frame, send the
        # corresponding message to self._inbox so _handle() processes it.
        # Use asyncio.TaskGroup to run StreamActors concurrently.
        # On framing error: send GOAWAY, then raise (propagate strategy).
        
        # Operations for initial settings exchange (e.g. send SETTINGS frame) 
        # can be done here before the loop
        my_settings = self.factory.settings()
        await self.send_frame(my_settings)

        async with asyncio.TaskGroup() as tg:
            stream_ids: dict[int, Actor] = dict()

            while data := await self.receive():
                # Get a frame object by parsing the received data.
                frame = self.factory.load(data)
                stream_id = frame.stream_id

                logger.debug(f"Received frame of type {frame.FrameType()} on stream {stream_id}")

                msg = _convert_Frame_to_Message(frame)

                if stream_id not in stream_ids:
                    actor = StreamActor(
                        stream_id=stream_id,
                        headers=Headers([]),  # Placeholder, actual headers will be parsed in StreamActor
                        end_stream=False,  # Placeholder, actual value will be determined by frame flags
                        http2_actor=self,
                        app=self._app,
                        aggregator=self._aggregator,
                        peername=self._peername,
                        sockname=self._sockname,
                        ssl=self._ssl,
                    )
                    stream_ids[stream_id] = actor
                    _ = actor.send(msg)  # Send the initial message to the new StreamActor
                    tg.create_task(actor.run())

                else:   
                    # fire-and-forget, StreamActor processes messages sequentially
                    # so no risk of inbox congestion
                    _ = stream_ids[stream_id].send(msg)


    async def _run(self):
        # Send the settings at first.
        my_settings = self.factory.settings()
        await self.send_frame(my_settings)
        logger.info(my_settings)

        # Per-stream recipients: keyed by stream ID so each stream's receive
        # queue is isolated. A shared queue would let one stream's events leak
        # into another stream's handler (e.g. GET's empty event consumed by POST).
        recipients: dict[int, HTTP2Recipient] = {}

        async with asyncio.TaskGroup() as tg:
            self._task_group = tg
            await self._frame_loop(tg, recipients)
        # TaskGroup block: every per-stream task has completed by this point.
        self._task_group = None

    @log
    async def send_frame(self, frame: FrameBase):
        """Send a raw HTTP/2 frame via the control-plane sender."""
        await self._control_sender(frame)

    async def receive(self) -> bytes | None:
        """Read the stream, get some data from incoming frames, and parse it"""
        # to distinguish the type of incoming frame
        if (data := await self._reader.read(9)) == 0:
            logger.info('StreamReader got EOF')
            return None

        size = int.from_bytes(data[:3], 'big', signed=False)
        data += await self._reader.read(size)  # Add error handling for the case of insufficient data

        return data

    async def _handle(self, msg: Message) -> None:
        match type(msg):
            case Header(): await self._on_headers(msg)
            case Data():    await self._on_data(msg)
            case WindowUpdate():   await self._on_window_update(msg)
            case SettingsReceived():  await self._on_settings(msg)
            case Goaway():    await self._on_goaway(msg)
            case WindowRequested():  await self._on_window_request(msg)
            case _:              pass  # unknown frame type: ignore per RFC

class StreamActor(Actor):
    """Owns one HTTP/2 stream.

    Assembles DATA frames, delegates to RequestActor for ASGI dispatch,
    writes response frames via HTTP2Actor (ask-pattern for flow control).
    Supervisor strategy: isolate — RST_STREAM on unhandled error.
    """

    def __init__(
        self,
        stream_id: int,
        headers: Headers,
        end_stream: bool,
        http2_actor: HTTP2Actor,          # for send-window ask-pattern
        app: Callable[..., Awaitable[None]],
        aggregator: EventAggregator,
        *,
        peername: tuple[str, int] | None = None,
        sockname: tuple[str, int] | None = None,
        ssl: bool = False,
    ) -> None:
        super().__init__()
        self._stream_id = stream_id
        self._headers = headers
        self._end_stream = end_stream
        self._http2_actor = http2_actor
        self._app = app
        self._aggregator = aggregator
        self._peername = peername
        self._sockname = sockname
        self._ssl = ssl

    async def run(self) -> None:
        # 1. If not end_stream: receive DATA frames via inbox until END_STREAM
        # 2. Build scope dict (type="http", method, path, headers, …)
        # 3. Build receive / send callables that route through HTTP2Actor
        #    (send blocks on window update — ask-pattern)
        # 4. Delegate to RequestActor(scope, receive, send, app, aggregator)
        # 5. On BaseException: send RST_STREAM via HTTP2Actor, do not re-raise
        ...