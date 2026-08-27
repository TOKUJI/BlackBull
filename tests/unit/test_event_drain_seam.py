"""Awaiting a fire-and-forget observer, without a sleep.

`docs/guide/events.md` defines three hook kinds and two of them are already
assertable: `@app.intercept` and `@app.on(..., blocking=True)` are awaited
inline, so once a request returns they have run.  The third —
`@app.on(name)` — is detached on purpose, so a test that asserts its
side-effect races the request it followed.

The dispatcher has always tracked those tasks; it drained them only at
shutdown, and that drain *cancels* what overruns its timeout.  A test needs
the opposite: wait for quiescence, cancel nothing, and say whether it got
there.
"""
from __future__ import annotations

import asyncio

import pytest

from blackbull import BlackBull
from blackbull.event import Event
from blackbull.testing import native

pytestmark = pytest.mark.asyncio


def _app_recording_into(sink: list, *, delay: float = 0.0):
    app = BlackBull()

    @app.route(path='/')
    async def _root(conn):
        return 'ok'

    @app.on('request_completed')
    async def _observer(event):
        if delay:
            await asyncio.sleep(delay)
        sink.append(event.name)

    return app


class TestTheSeamExists:
    async def test_drain_is_reachable_from_the_app(self):
        app = BlackBull()
        assert hasattr(app, 'drain_events'), (
            'the detached-observer drain has no public seam')


class TestItRemovesTheRace:
    async def test_a_detached_observer_is_assertable_after_drain(self):
        """The whole point: no `asyncio.sleep` in the test.

        The observer sleeps longer than any incidental scheduling gap, so a
        passing assertion cannot be luck.
        """
        seen: list = []
        app = _app_recording_into(seen, delay=0.25)

        await native.get(app, '/')
        # Deliberately not asserting `seen == []` here: whether the task has
        # started is a scheduling detail, and pinning it would make this a
        # test of the event loop.
        await app.drain_events(timeout=5.0)

        assert seen == ['request_completed'], (
            f'observer had not completed after drain: {seen}')

    async def test_drain_reports_whether_it_reached_quiescence(self):
        """A drain that times out must say so rather than pass quietly."""
        seen: list = []
        app = _app_recording_into(seen, delay=5.0)

        await native.get(app, '/')
        drained = await app.drain_events(timeout=0.05)

        assert drained is False, 'a timed-out drain reported success'
        assert seen == [], 'the observer somehow finished inside 50 ms'

    async def test_drain_does_not_cancel_what_it_waits_for(self):
        """Unlike `aclose`, which cancels on timeout.

        A test helper that cancelled the work under test would make the
        thing it is meant to observe unobservable.
        """
        seen: list = []
        app = _app_recording_into(seen, delay=0.3)

        await native.get(app, '/')
        assert await app.drain_events(timeout=0.05) is False
        # The task must still be alive and must still finish on its own.
        assert await app.drain_events(timeout=5.0) is True
        assert seen == ['request_completed']

    async def test_drain_is_a_no_op_when_nothing_is_pending(self):
        app = BlackBull()

        @app.route(path='/')
        async def _root(conn):
            return 'ok'

        await native.get(app, '/')
        assert await app.drain_events(timeout=0.01) is True


class TestQuiescenceNotASnapshot:
    """An observer may emit, and what it spawns must also be waited for.

    Awaiting one snapshot of the pending set is the obvious implementation
    and the wrong one: the set can refill while it is being awaited.  This
    is also why `aclose` — which does exactly that — can return with work
    still outstanding at shutdown.
    """

    async def test_an_observer_that_emits_is_drained_too(self):
        seen: list = []
        app = BlackBull()

        @app.route(path='/')
        async def _root(conn):
            return 'ok'

        @app.on('request_completed')
        async def _first(event):
            await asyncio.sleep(0.05)
            seen.append('first')
            # Through the dispatcher: there is no `app.emit`, and an
            # AttributeError here would be swallowed by observer isolation
            # and read as "the drain worked".
            await app._dispatcher.emit(Event('second_hop', {}))

        @app.on('second_hop')
        async def _second(event):
            await asyncio.sleep(0.05)
            seen.append('second')

        await native.get(app, '/')
        drained = await app.drain_events(timeout=5.0)

        assert drained is True
        assert seen == ['first', 'second'], (
            f'the drain returned before the chained observer ran: {seen}')
