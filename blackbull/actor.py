import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field


@dataclass
class Message:
    """Base class for all Level A Actor messages.

    Subclasses are plain dataclasses — add fields with standard dataclass syntax.
    The ``sender`` field is excluded from equality comparison so that message
    identity is determined by payload, not by which Actor sent it.
    """

    sender: "Actor | None" = field(default=None, compare=False, repr=False)


class Actor:
    """Base class for all BlackBull Actors.

    Each Actor owns an ``asyncio.Queue`` inbox and is expected to run as an
    ``asyncio.Task`` started by its Supervisor.  Actors communicate
    exclusively via :meth:`send`; they never share mutable state.

    Subclasses must override :meth:`_handle`.

    Pass ``inbox_maxsize`` to bound the inbox (``0`` — the default — is
    unbounded, matching :class:`asyncio.Queue`).  A bounded inbox lets an actor
    apply back-pressure or, with :meth:`asyncio.Queue.put_nowait`, an explicit
    overflow policy.
    """

    def __init__(self, *, inbox_maxsize: int = 0) -> None:
        self.__inbox: asyncio.Queue[Message] | None = None
        self.__inbox_maxsize = inbox_maxsize

    @property
    def _inbox(self) -> asyncio.Queue[Message]:
        if self.__inbox is None:
            self.__inbox = asyncio.Queue(maxsize=self.__inbox_maxsize)
        return self.__inbox

    async def run(self) -> None:
        """Consume the inbox until the task is cancelled."""
        async for msg in self._inbox_iter():
            await self._handle(msg)

    async def send(self, msg: Message) -> None:
        """Enqueue *msg* to this Actor's inbox (non-blocking)."""
        await self._inbox.put(msg)

    async def _inbox_iter(self) -> AsyncIterator[Message]:
        while True:
            yield await self._inbox.get()

    async def _handle(self, msg: Message) -> None:
        """Dispatch a received message.

        Raises:
            NotImplementedError: Always — subclasses must override this method.
        """
        raise NotImplementedError(
            f"{type(self).__name__}._handle is not implemented "
            f"for message type {type(msg).__name__}"
        )
