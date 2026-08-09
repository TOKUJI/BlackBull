"""The per-connection serve task starts eagerly, inside ``connection_made``.

``asyncio.create_task`` queues the coroutine's first step for the *next* loop
iteration.  For a connection that is one hop between the accept and the first
read — pure latency on a churn workload, where every request pays it.  Starting
the task eagerly runs the prologue (deadline object, protocol-detection order,
the first ``peek``) synchronously and parks at the same place it would have
parked anyway.

What must stay true regardless: a prologue that raises cannot escape into the
transport's ``connection_made`` callback, because asyncio has nowhere to put
that exception but a log line, and the connection would hang half-open.
"""
from __future__ import annotations

import asyncio

import pytest

from blackbull.server.server import ASGIServer

pytestmark = pytest.mark.asyncio


class _FakeTransport:
    def __init__(self) -> None:
        self.reading = True
        self.closed = False

    def pause_reading(self): self.reading = False
    def resume_reading(self): self.reading = True
    def write(self, data): pass
    def close(self): self.closed = True
    def is_closing(self): return self.closed
    def get_extra_info(self, name, default=None): return default


async def _noop_app(conn, receive, send):  # pragma: no cover - never dispatched
    pass


def _protocol(monkeypatch, serve_connection):
    """A ``_ServedConnection`` whose ``_serve_connection`` is *serve_connection*."""
    server = ASGIServer(_noop_app)
    monkeypatch.setattr(server, '_serve_connection', serve_connection)
    return server.connection_protocol_factory()()


async def test_serve_body_runs_before_connection_made_returns(monkeypatch):
    """The prologue is not waiting on a loop turn."""
    reached = []
    park = asyncio.Event()

    async def _serve_connection(reader, writer, **kwargs):
        reached.append('prologue')
        await park.wait()

    proto = _protocol(monkeypatch, _serve_connection)
    proto.connection_made(_FakeTransport())

    # No await between connection_made() and here — under the queued form
    # this list is still empty at this point.
    assert reached == ['prologue']

    park.set()
    await proto._serve_task


async def test_the_task_still_parks_where_it_always_did(monkeypatch):
    """Eager only means "started"; a coroutine that suspends is still a live
    task the connection holds, not a completed one."""
    park = asyncio.Event()

    async def _serve_connection(reader, writer, **kwargs):
        await park.wait()

    proto = _protocol(monkeypatch, _serve_connection)
    proto.connection_made(_FakeTransport())

    assert not proto._serve_task.done()
    park.set()
    await proto._serve_task
    assert proto._serve_task.done()


async def test_a_prologue_that_raises_does_not_escape_connection_made(monkeypatch):
    """Eager start surfaces pre-first-await exceptions at the *call site*.
    That call site is a transport callback, so it has to be caught here."""
    async def _serve_connection(reader, writer, **kwargs):
        raise RuntimeError('prologue blew up')

    proto = _protocol(monkeypatch, _serve_connection)
    proto.connection_made(_FakeTransport())  # must not raise

    with pytest.raises(RuntimeError, match='prologue blew up'):
        await proto._serve_task


async def test_a_failed_prologue_is_logged_not_swallowed(monkeypatch, caplog):
    """The done-callback still runs for an already-completed eager task —
    otherwise a connection failure vanishes."""
    async def _serve_connection(reader, writer, **kwargs):
        raise RuntimeError('prologue blew up')

    proto = _protocol(monkeypatch, _serve_connection)
    with caplog.at_level('ERROR', logger='blackbull.server.server'):
        proto.connection_made(_FakeTransport())
        # The callback is scheduled with call_soon even when the task is
        # already done; one loop turn is enough to see it.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
    assert 'connection task failed' in caplog.text
