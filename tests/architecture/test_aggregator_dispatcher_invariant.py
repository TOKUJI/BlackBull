"""On the served path, an aggregator exists exactly when a dispatcher does.

This invariant is load-bearing for the whole `aggregator=None` question.  If it
holds, then `aggregator is None` on a served connection implies the app has no
`_dispatcher`, which in turn means `HTTP1Actor._dispatch_request`'s legacy
branch never constructs its disconnect wrapper in production — the branch is
reachable only by direct construction in tests.

It is asserted here rather than reasoned about in a proposal because the two
halves live in different files and neither mentions the other: `server.py`
derives the aggregator from the dispatcher (once, in `__init__`), and
`http1_actor.py` keys its legacy branch off the dispatcher independently.  Nothing but this test stops a
future edit to either side from quietly making the dead branch live again —
with a second, divergent disconnect-wrapper implementation behind it.
"""
from __future__ import annotations

import pytest

from blackbull import BlackBull
from blackbull.event_aggregator import EventAggregator
from blackbull.server.server import ASGIServer


async def _foreign_app(scope, receive, send):
    """An ASGI app that is not a BlackBull instance — no `_dispatcher`."""


def test_a_blackbull_app_gets_both():
    server = ASGIServer(BlackBull())

    assert server._cached_dispatcher is not None
    assert isinstance(server._cached_aggregator, EventAggregator)


def test_a_foreign_app_gets_neither():
    server = ASGIServer(_foreign_app)

    assert server._cached_dispatcher is None
    assert server._cached_aggregator is None


@pytest.mark.parametrize('app', [BlackBull(), _foreign_app],
                         ids=['blackbull', 'foreign'])
def test_the_two_are_never_out_of_step(app):
    """The invariant itself, stated as the biconditional the dead-branch
    argument depends on."""
    server = ASGIServer(app)

    assert ((server._cached_aggregator is None)
            == (server._cached_dispatcher is None))


def test_the_aggregator_is_built_from_that_very_dispatcher():
    """Not merely "both present" — the aggregator must wrap the app's own
    dispatcher, or events would be published where nobody subscribed."""
    app = BlackBull()
    server = ASGIServer(app)

    assert server._cached_aggregator._dispatcher is app._dispatcher
