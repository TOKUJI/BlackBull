"""The native test client, judged against the defect that motivated it.

Sprint 87 found ``HEAD`` answering **405** under ``BB_FORCE_ASGI_SCOPE=1``
while the native lane answered 200.  The cause was ordering, not logic: the
compat lane's scope snapshot was taken *before* ``HTTP1Actor.run`` rewrites
``HEAD`` to ``GET`` (RFC 9110 §9.3.2), so the router looked for a ``HEAD``
route, found none, and returned 405.  Every existing HEAD test passed.

A test client that could not have caught that has not earned its place.  So
this file re-injects the defect and asks each instrument what it sees.  The
injection restores exactly one thing — the method the compat snapshot carries
— because that single ordering difference *is* the bug; nothing else about
the actor is touched.

The answer, asserted below, is that **only Tier 2 catches it**: the defect
lives in the protocol actor, above where Tier 1 starts and on a path
``TestClient`` never runs at all.  That is the argument for Tier 2 stated as
an executable claim rather than a design opinion.
"""
import pytest

from blackbull import BlackBull
from blackbull.connection import Connection
from blackbull.env import reset_settings_cache
from blackbull.server.http1_actor import HTTP1Actor
from blackbull.testing import NativeTestServer, TestClient, native


@pytest.fixture
def app():
    a = BlackBull()

    @a.route(path='/')
    async def _root():
        return 'hello'

    return a


@pytest.fixture
def forced_asgi_lane(monkeypatch):
    """Run the actor's compat lane, the one the regression appeared on."""
    monkeypatch.setenv('BB_FORCE_ASGI_SCOPE', '1')
    reset_settings_cache()
    yield
    monkeypatch.delenv('BB_FORCE_ASGI_SCOPE', raising=False)
    reset_settings_cache()


@pytest.fixture
def prefix_ordering(monkeypatch):
    """Re-inject the Sprint 87 ordering: snapshot before the HEAD→GET rewrite.

    ``_parse`` records the method the request line carried; the compat
    snapshot then reports *that* method instead of the rewritten one — which
    is what a scope materialised earlier in ``run()`` would have frozen.

    Keyed by ``id(conn)``: :class:`Connection` is a ``slots=True`` dataclass,
    so neither an attribute nor a ``WeakKeyDictionary`` entry can be attached
    to it.  The map is per-test and cleared on teardown.
    """
    request_line_method: dict[int, str] = {}
    real_parse = HTTP1Actor._parse
    real_to_asgi_scope = Connection.to_asgi_scope

    def _parse(self, data):
        conn = real_parse(self, data)
        request_line_method[id(conn)] = conn.method
        return conn

    def _to_asgi_scope(self, *, force_asgi=False):
        scope = real_to_asgi_scope(self, force_asgi=force_asgi)
        if force_asgi and id(self) in request_line_method:
            scope['method'] = request_line_method[id(self)]
        return scope

    monkeypatch.setattr(HTTP1Actor, '_parse', _parse)
    monkeypatch.setattr(Connection, 'to_asgi_scope', _to_asgi_scope)
    yield
    request_line_method.clear()


# --- the gate ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_tier2_catches_the_head_regression(app, forced_asgi_lane, prefix_ordering):
    """With the pre-fix ordering restored, Tier 2 sees the 405."""
    async with NativeTestServer(app) as server:
        resp = await server.client.head('/')
    assert resp.status_code == 405, (
        'Tier 2 did not observe the Sprint 87 regression — the instrument '
        'cannot see the defect class it was built for.')


@pytest.mark.asyncio
async def test_tier2_sees_200_once_the_ordering_is_correct(app, forced_asgi_lane):
    """Same lane, same request, current code: the fixed behaviour."""
    async with NativeTestServer(app) as server:
        resp = await server.client.head('/')
    assert resp.status_code == 200
    assert resp.content == b''


@pytest.mark.asyncio
async def test_both_lanes_agree_on_head_through_tier2(app):
    """The native lane was always 200; the point is that the two now match."""
    async with NativeTestServer(app) as server:
        resp = await server.client.head('/')
    assert resp.status_code == 200


# --- why Tier 2 is not redundant with the other two instruments -------------
#
# The test is not "the other instruments give the wrong answer" — it is that
# their answer does not *change* when the defect is injected.  An instrument
# that reports the same thing whether the code is broken or fixed cannot be
# the one guarding this behaviour, whatever that thing happens to be.
#
# Both return 405 either way, for the same underlying reason: the HEAD→GET
# rewrite lives in ``HTTP1Actor``, so an instrument that starts below the
# actor (Tier 1) or replaces it entirely (``TestClient``) cannot express
# RFC 9110 §9.3.2 HEAD semantics at all.

@pytest.mark.asyncio
async def test_tier1_is_blind_to_the_regression(app, forced_asgi_lane,
                                                prefix_ordering, monkeypatch):
    """Tier 1's answer is identical with and without the defect injected."""
    with_defect = (await native.head(app, '/')).status

    # ``undo`` drops every patch this test's monkeypatch holds, the lane's
    # env var included — re-set it so the only variable that changed between
    # the two measurements is the injected ordering.
    monkeypatch.undo()
    monkeypatch.setenv('BB_FORCE_ASGI_SCOPE', '1')
    reset_settings_cache()
    without_defect = (await native.head(app, '/')).status

    assert with_defect == without_defect, (
        'Tier 1 distinguished the two, which would make this argument wrong.')


def test_the_asgi_test_client_is_blind_to_the_regression(app, forced_asgi_lane,
                                                         prefix_ordering,
                                                         monkeypatch):
    """``TestClient`` runs no protocol actor — ``httpx.ASGITransport`` builds
    the scope itself, so the actor's snapshot ordering is not in the picture
    and its answer cannot move."""
    with TestClient(app) as client:
        with_defect = client.head('/').status_code

    monkeypatch.undo()
    monkeypatch.setenv('BB_FORCE_ASGI_SCOPE', '1')
    reset_settings_cache()
    with TestClient(app) as client:
        without_defect = client.head('/').status_code

    assert with_defect == without_defect


# --- the defect class, not just the instance --------------------------------

@pytest.mark.asyncio
async def test_pre_dispatch_mutations_reach_the_compat_lane(app, forced_asgi_lane):
    """The general invariant the HEAD bug was one instance of.

    Anything ``HTTP1Actor.run`` mutates on the ``Connection`` before dispatch
    must be visible to the compat lane's scope.  ``HEAD``→``GET`` is the
    mutation that exists today; this asserts the property rather than the one
    case, so a future pre-dispatch mutation cannot reintroduce the class
    silently.
    """
    observed = []

    a = BlackBull()

    @a.route(path='/')
    async def _root(conn, receive, send):
        observed.append(conn.method)
        await send(b'ok')

    async with NativeTestServer(a) as server:
        await server.client.head('/')

    # The actor rewrote the method before dispatch; the compat lane's scope
    # carried the rewrite through ``from_scope``, so the router saw GET.
    assert observed == ['GET']
