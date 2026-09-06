"""MQTT-private FIFO bounded by message count and retained wire-size charge.

The queue owns waiting messages, not the consumer's active message or the
producer's single admission candidate. Closing wakes waiters without requiring
room for a sentinel. No task is created for a blocked admission.
"""
from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable

from ..actor import Message


class MailboxClosed(RuntimeError):
    """The owner stopped accepting messages."""


class MailboxTooLarge(ValueError):
    """A single message cannot fit the byte budget, even in an empty queue."""


class Mailbox(asyncio.Queue[Message]):
    def __init__(self, maxsize: int, max_bytes: int,
                 size_of: Callable[[Message], int]) -> None:
        if maxsize <= 0 or max_bytes <= 0:
            raise ValueError('MQTT mailbox limits must be positive')
        super().__init__(maxsize=maxsize)
        self.max_bytes = max_bytes
        self.queued_bytes = 0
        self.closed = False
        self._size_of = size_of
        self._sizes: deque[int] = deque()
        self._space = asyncio.Event()
        self._available = asyncio.Event()

    def _put(self, item: Message) -> None:
        size = self._size_of(item)
        self._sizes.append(size)
        self.queued_bytes += size
        super()._put(item)
        self._available.set()

    def _get(self) -> Message:
        item = super()._get()
        self.queued_bytes -= self._sizes.popleft()
        self._space.set()
        return item

    def put_nowait(self, item: Message) -> None:
        if self.closed:
            raise MailboxClosed('MQTT mailbox is closed')
        size = self._size_of(item)
        if size > self.max_bytes:
            raise MailboxTooLarge('MQTT message exceeds mailbox byte budget')
        if self.queued_bytes + size > self.max_bytes:
            raise asyncio.QueueFull
        super().put_nowait(item)

    async def put(self, item: Message) -> None:
        while True:
            try:
                self.put_nowait(item)
                return
            except asyncio.QueueFull:
                # There is no await between the admission check and clear:
                # a consumer cannot free space in a lost-wakeup interval.
                self._space.clear()
                await self._space.wait()

    async def get(self) -> Message:
        while True:
            try:
                return self.get_nowait()
            except asyncio.QueueEmpty:
                if self.closed:
                    raise MailboxClosed('MQTT mailbox is drained') from None
                self._available.clear()
                await self._available.wait()

    def close(self, *, discard: bool = False) -> None:
        self.closed = True
        if discard:
            while not self.empty():
                self.get_nowait()
                self.task_done()
        self._space.set()
        self._available.set()
