"""PRIORITY must not let a peer grow server state.

RFC 9113 §6.3 lets a peer send PRIORITY for a stream in *any* state,
including idle — prioritising a stream before opening it is the whole
point of the frame.  BlackBull created a priority-tree node for each such
frame and never removed one: ``Stream.remove_child`` exists and has no
caller anywhere in the tree.  Fourteen bytes on the wire bought a node
that lived for the connection, and PRIORITY is not one of the frame types
Sprint 104's rate meters cover.

RFC 9113 §5.3 deprecated that prioritisation scheme and BlackBull does not
implement it — ``Stream.weight`` and ``Stream.parent`` are written by the
responder and read by nothing.  So the state was not merely unbounded, it
was never used, and the fix is to stop recording it rather than to cap it.
What must survive is the frame's *validation*: §5.3.1 makes a stream
depending on itself a stream error, and h2spec tests exactly that.

PRIORITY_UPDATE (RFC 9218 §7.1) reaches the same ``add_child`` on purpose
— a hint may legitimately arrive before HEADERS — so it keeps its
pre-created node, under a cap §7 explicitly permits.
"""
from __future__ import annotations

import asyncio

import pytest

from blackbull.protocol.frame_types import ErrorCodes, FrameTypes
from blackbull.server.http2_actor import HTTP2Actor
from blackbull.server.sender import AsyncioWriter

pytestmark = pytest.mark.asyncio


def _frame(type_byte: FrameTypes, flags: int = 0, stream_id: int = 0,
           payload: bytes = b'') -> bytes:
    return (len(payload).to_bytes(3, 'big') + type_byte + bytes([flags])
            + stream_id.to_bytes(4, 'big') + payload)


def _priority(stream_id: int, *, depends_on: int = 0, weight: int = 16,
              exclusive: bool = False) -> bytes:
    dep = depends_on | (0x80000000 if exclusive else 0)
    return _frame(FrameTypes.PRIORITY, 0, stream_id,
                  dep.to_bytes(4, 'big') + bytes([weight - 1]))


def _priority_update(prioritized: int, field: bytes = b'u=3') -> bytes:
    return _frame(FrameTypes.PRIORITY_UPDATE, 0, 0,
                  prioritized.to_bytes(4, 'big') + field)


class _Wire:
    def __init__(self, frames: list[bytes]):
        self._queue: asyncio.Queue = asyncio.Queue()
        for f in frames:
            self._queue.put_nowait(f)
        self.closed = False
        self.written = bytearray()

    async def receive(self) -> bytes:
        if self.closed:
            return b''
        return await self._queue.get()

    def write(self, data: bytes) -> None:
        self.written += data

    def writelines(self, parts) -> None:
        for p in parts:
            self.written += p

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        pass

    def get_extra_info(self, *a, **kw):
        return None


async def _drive(frames: list[bytes]):
    """Run the actor over *frames* and hand back the actor and what it wrote."""
    wire = _Wire([_frame(FrameTypes.SETTINGS, 0, 0, b'')] + frames)

    async def _app(*a, **kw):  # pragma: no cover - no request is dispatched
        pass

    actor = HTTP2Actor(None, AsyncioWriter(wire), _app, aggregator=None)
    actor.receive = wire.receive
    sent: list = []
    real_send = actor.send_frame

    async def _recording_send(frame):
        sent.append(frame)
        return await real_send(frame)

    actor.send_frame = _recording_send
    task = asyncio.ensure_future(actor.run())
    for _ in range(400):
        await asyncio.sleep(0)
        if wire._queue.empty():
            break
    await asyncio.sleep(0.02)
    task.cancel()
    return actor, sent, wire


class TestPriorityFloodHoldsNoState:
    async def test_a_thousand_idle_priorities_leave_no_nodes(self):
        n = 1000
        # Descending ids: nothing about ordering should admit them either.
        actor, _sent, _wire = await _drive(
            [_priority(2 * (n - i) + 1) for i in range(n)])

        assert len(actor.root_stream.children) == 0, (
            f'{len(actor.root_stream.children)} priority-tree nodes survived '
            f'{n} PRIORITY frames on idle streams — 14 wire bytes each')

    async def test_the_connection_is_not_torn_down_for_it(self):
        """The frame is legal (§6.3).  Bounding it must not answer GOAWAY."""
        actor, sent, wire = await _drive([_priority(2 * i + 1)
                                          for i in range(50)])
        assert not any(f.FrameType() == FrameTypes.GOAWAY for f in sent)
        assert not wire.closed

    async def test_an_exclusive_flood_costs_no_tree_walk(self):
        """The exclusive branch walked every child to rewrite a dead field.

        With the tree growing one node per frame, that walk made a PRIORITY
        flood quadratic as well as unbounded.
        """
        actor, _sent, _wire = await _drive(
            [_priority(2 * i + 1, depends_on=1, exclusive=True)
             for i in range(200)])
        assert len(actor.root_stream.children) == 0


class TestPriorityValidationSurvives:
    async def test_a_stream_depending_on_itself_is_a_stream_error(self):
        """RFC 9113 §5.3.1 — h2spec asserts this; it must not regress."""
        _actor, sent, _wire = await _drive([_priority(3, depends_on=3)])

        rsts = [f for f in sent if f.FrameType() == FrameTypes.RST_STREAM]
        assert rsts, 'a stream depending on itself was accepted'
        assert rsts[0].stream_id == 3
        assert rsts[0].error_code == ErrorCodes.PROTOCOL_ERROR

    async def test_a_normal_priority_is_answered_with_nothing(self):
        _actor, sent, wire = await _drive([_priority(3, depends_on=0)])
        assert not any(f.FrameType() in (FrameTypes.RST_STREAM,
                                         FrameTypes.GOAWAY) for f in sent)
        assert not wire.closed


class TestPriorityUpdatePreCreationIsCapped:
    async def test_pre_created_hint_nodes_are_bounded(self, caplog):
        """RFC 9218 §7 permits limiting how many buffered hints are kept."""
        actor, _sent, _wire = await _drive([])
        cap = actor.max_concurrent_streams

        _actor2, _s, _w = await _drive(
            [_priority_update(2 * i + 1) for i in range(cap + 50)])

        held = len(_actor2.root_stream.children)
        assert held <= cap, (
            f'{held} PRIORITY_UPDATE hint nodes held with a cap of {cap}')

    async def test_a_hint_still_reaches_the_stream_it_names(self):
        """The cap must not cost the feature: a hint before HEADERS is kept."""
        _actor, _sent, _wire = await _drive([_priority_update(3, b'u=1')])
        stream = _actor.find_stream(3)
        assert stream is not None, 'the pre-created hint node is gone'
        assert stream.priority_hint is not None
