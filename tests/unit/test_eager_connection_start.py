"""The per-connection serve task starts eagerly, inside ``connection_made``.

``asyncio.create_task`` queues the coroutine's first step for the *next* loop
iteration.  For a connection that is one hop between the accept and the first
read — pure latency on a churn workload, where every request pays it.  Starting
the task eagerly runs the prologue (deadline object, protocol-detection order,
the first ``peek``) synchronously and parks at the same place it would have
parked anyway.

``eager_start`` arrived in 3.12 and the supported floor is 3.11, so there are
two shapes to hold to, not one.  Both are asserted here — the fallback is a
branch that ships, and an unasserted branch is one nobody has run.  The eager
arm is exercised wherever the interpreter allows it; the queued arm is
exercised everywhere, by forcing the capability flag off.

What must stay true under either: a prologue that raises cannot escape into the
transport's ``connection_made`` callback, because asyncio has nowhere to put
that exception but a log line, and the connection would hang half-open.
"""
from __future__ import annotations

import asyncio
import sys

import pytest

import blackbull.server.server as server_module
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


async def test_the_capability_flag_tracks_the_interpreter():
    """Guards the branch selector itself: a flag stuck off would make every
    eager assertion below silently untested."""
    assert server_module._EAGER_TASKS == (sys.version_info >= (3, 12))


@pytest.mark.skipif(not server_module._EAGER_TASKS,
                    reason='eager_start requires Python 3.12+')
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


async def test_the_pre_3_12_fallback_still_serves_the_connection(monkeypatch):
    """The floor is 3.11, where the hop cannot be skipped.  The connection is
    served anyway — one loop turn later, which is the old behaviour exactly."""
    monkeypatch.setattr(server_module, '_EAGER_TASKS', False)
    reached = []
    park = asyncio.Event()

    async def _serve_connection(reader, writer, **kwargs):
        reached.append('prologue')
        await park.wait()

    proto = _protocol(monkeypatch, _serve_connection)
    proto.connection_made(_FakeTransport())

    assert reached == [], 'the queued form runs nothing before its loop turn'
    await asyncio.sleep(0)
    assert reached == ['prologue']

    park.set()
    await proto._serve_task


@pytest.fixture(params=['eager', 'queued'])
def either_spawn_form(request, monkeypatch):
    """The invariants below belong to the connection, not to eager start, so
    they are asserted under both spawn forms."""
    if request.param == 'eager':
        if not server_module._EAGER_TASKS:
            pytest.skip('eager_start requires Python 3.12+')
    else:
        monkeypatch.setattr(server_module, '_EAGER_TASKS', False)
    return request.param


async def test_the_task_still_parks_where_it_always_did(monkeypatch,
                                                        either_spawn_form):
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


async def test_a_prologue_that_raises_does_not_escape_connection_made(
        monkeypatch, either_spawn_form):
    """A pre-first-await exception must reach the task, not the transport
    callback that built it — asyncio has nowhere to put the latter but a log
    line, and the connection would hang half-open."""
    async def _serve_connection(reader, writer, **kwargs):
        raise RuntimeError('prologue blew up')

    proto = _protocol(monkeypatch, _serve_connection)
    proto.connection_made(_FakeTransport())  # must not raise

    with pytest.raises(RuntimeError, match='prologue blew up'):
        await proto._serve_task


async def test_a_failed_prologue_is_logged_not_swallowed(monkeypatch, caplog,
                                                         either_spawn_form):
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
