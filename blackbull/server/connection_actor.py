"""Connection Actor — Phase 6 Step 5."""
import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from ..actor import Actor, Message
from ..event_aggregator import EventAggregator
from .recipient import (AbstractReader, IncompleteReadError,
                        _HTTP2_STREAM_QUEUE_DEPTH, _WS_EVENT_QUEUE_DEPTH)
from .sender import AbstractWriter

logger = logging.getLogger(__name__)

_HTTP2_PREFACE_FIRST_LINE = b'PRI * HTTP/2.0\r\n'
_HTTP2_PREFACE_REMAINDER  = b'\r\nSM\r\n\r\n'


class ConnectionActor(Actor):
    """One per accepted TCP connection.

    Detects protocol, spawns the appropriate protocol Actor, and isolates
    failures so one bad connection cannot affect others.

    Supervisor strategy: isolate — ExceptionGroup from the TaskGroup is
    caught and emitted as a single on_error event; the connection is always
    closed in finally.
    """

    def __init__(
        self,
        reader: AbstractReader,
        writer: AbstractWriter,
        app: Callable[..., Awaitable[None]],
        aggregator: 'EventAggregator | None',
        *,
        peername: tuple[str, int] | None = None,
        sockname: tuple[str, int] | None = None,
        ssl: bool = False,
        stream_queue_depth: int = _HTTP2_STREAM_QUEUE_DEPTH,
        ws_queue_depth: int = _WS_EVENT_QUEUE_DEPTH,
    ) -> None:
        super().__init__()
        self._reader = reader
        self._writer = writer
        self._app = app
        self._aggregator = aggregator
        self._peername = peername
        self._sockname = sockname
        self._ssl = ssl
        self._stream_queue_depth = stream_queue_depth
        self._ws_queue_depth = ws_queue_depth

    async def run(self) -> None:
        if self._aggregator is not None:
            await self._aggregator.on_connection_accepted(self._peername)
        try:
            async with asyncio.TaskGroup() as tg:
                tg.create_task(self._dispatch())
        except* Exception as eg:
            if self._aggregator is not None:
                await self._aggregator.on_error({}, eg)
        finally:
            await self._writer.close()

    async def _dispatch(self) -> None:
        first_line = await self._reader.readuntil(b'\r\n')
        if first_line == _HTTP2_PREFACE_FIRST_LINE:
            remainder = await self._reader.readexactly(8)
            if first_line + remainder != _HTTP2_PREFACE_FIRST_LINE + _HTTP2_PREFACE_REMAINDER:
                raise ValueError(
                    f'Invalid HTTP/2 preface continuation: {remainder!r}')
            from .http2_actor import HTTP2Actor  # noqa: PLC0415
            actor = HTTP2Actor(
                self._reader, self._writer, self._app, self._aggregator,
                peername=self._peername, sockname=self._sockname,
                ssl=self._ssl,
                stream_queue_depth=self._stream_queue_depth,
            )
        else:
            from .http1_actor import HTTP1Actor  # noqa: PLC0415
            actor = HTTP1Actor(
                self._reader, self._writer, self._app, self._aggregator,
                request=first_line,
                peername=self._peername, sockname=self._sockname,
                ssl=self._ssl,
                ws_queue_depth=self._ws_queue_depth,
            )
        await actor.run()

    async def _handle(self, msg: Message) -> None:
        raise NotImplementedError
