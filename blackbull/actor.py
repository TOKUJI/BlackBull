"""The two base types every BlackBull actor is built from.

[`Actor`][blackbull.actor.Actor] owns a queue and a loop that drains it;
[`Message`][blackbull.actor.Message] is what goes in the queue.  Together they
are the whole mechanism: state lives inside one actor and is mutated only by
that actor's own loop, so any coordination between two of them is a message,
never a shared object or a lock.

To write one, subclass ``Actor``, override ``_handle``, and declare each
message a subclass of ``Message``.  A subclass is a plain dataclass, so its
fields are ordinary dataclass fields; ``sender`` is inherited and left out of
equality, which lets two messages with the same payload compare equal
regardless of who sent them.

An actor's inbox does not exist until first use and is created on the loop
that touches it, so an ``Actor`` may be constructed before the event loop is
running.

``docs/about/internals.md`` has the actor hierarchy the server assembles from
these — which actor owns a connection, a stream, a request.
"""
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
        """Enqueue *msg* to this Actor's inbox.

        Returns immediately for an unbounded inbox (the default).  When the
        Actor was constructed with ``inbox_maxsize > 0`` and the inbox is
        full, this awaits until a slot frees up — backpressure on the
        producer, per the bounded-inbox overflow policy.
        """
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
