import asyncio
import pytest
from dataclasses import dataclass
from blackbull.actor import Actor, Message


@dataclass
class Ping(Message):
    value: int = 0


class CollectorActor(Actor):
    """Concrete Actor that appends every received message to a list."""

    def __init__(self) -> None:
        super().__init__()
        self.received: list[Message] = []

    async def _handle(self, msg: Message) -> None:
        self.received.append(msg)


@pytest.mark.asyncio
async def test_actor_receives_single_message() -> None:
    actor = CollectorActor()
    task = asyncio.create_task(actor.run())
    await actor.send(Ping(value=42))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(actor.received) == 1
    assert isinstance(actor.received[0], Ping)
    assert actor.received[0].value == 42


@pytest.mark.asyncio
async def test_actor_preserves_message_order() -> None:
    actor = CollectorActor()
    task = asyncio.create_task(actor.run())
    for i in range(5):
        await actor.send(Ping(value=i))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert [m.value for m in actor.received] == list(range(5))


@pytest.mark.asyncio
async def test_base_actor_handle_raises_not_implemented() -> None:
    actor = Actor()
    task = asyncio.create_task(actor.run())
    await actor.send(Message())
    with pytest.raises(NotImplementedError):
        await asyncio.wait_for(task, timeout=1.0)


def test_message_sender_excluded_from_equality() -> None:
    actor = CollectorActor()
    a = Ping(value=1, sender=actor)
    b = Ping(value=1, sender=None)
    assert a == b


@pytest.mark.asyncio
async def test_send_is_nonblocking() -> None:
    actor = CollectorActor()
    # Do not start run() — inbox accumulates without a consumer
    for i in range(100):
        await actor.send(Ping(value=i))
    assert actor._inbox.qsize() == 100
