import pytest
from unittest.mock import AsyncMock, Mock
from blackbull.event import Event, EventDispatcher
from blackbull.event_aggregator import EventAggregator


@pytest.fixture
def dispatcher():
    d = AsyncMock(spec_set=EventDispatcher)
    d.emit = AsyncMock()
    # d.emit_intercept = AsyncMock()
    return d


@pytest.fixture
def aggregator(dispatcher):
    return EventAggregator(dispatcher)


@pytest.mark.asyncio
async def test_on_app_startup(aggregator, dispatcher) -> None:
    await aggregator.on_app_startup()
    dispatcher.emit.assert_called_once_with(Event("app_startup", {}))


@pytest.mark.asyncio
async def test_on_request_received(aggregator, dispatcher) -> None:
    scope = {"type": "http", "method": "GET", "path": "/foo",
             "http_version": "1.1", "headers": [], "client": ("1.2.3.4", 9000)}
    await aggregator.on_request_received(scope)
    detail = dispatcher.emit.call_args[0][0].detail
    assert detail['scope'] is scope
    assert detail['method'] == 'GET'
    assert detail['path'] == '/foo'
    assert detail['client_ip'] == '1.2.3.4'
    assert detail['http_version'] == '1.1'


@pytest.mark.asyncio
async def test_on_before_handler(aggregator, dispatcher) -> None:
    scope = {"method": "POST", "path": "/submit"}
    receive, send, call_next = AsyncMock(), AsyncMock(), AsyncMock()
    await aggregator.on_before_handler(scope, receive, send, call_next)
    detail = dispatcher.emit.call_args[0][0].detail
    assert detail['scope'] is scope
    assert detail['method'] == 'POST'
    assert detail['path'] == '/submit'
    call_next.assert_awaited_once_with(scope, receive, send)


@pytest.mark.asyncio
async def test_on_after_handler_success(aggregator, dispatcher) -> None:
    scope = {"type": "http"}
    await aggregator.on_after_handler(scope)
    dispatcher.emit.assert_called_once_with(
        Event("after_handler", {"scope": scope, "exception": None})
    )


@pytest.mark.asyncio
async def test_on_after_handler_with_exception(aggregator, dispatcher) -> None:
    scope = {"type": "http"}
    exc = ValueError("boom")
    await aggregator.on_after_handler(scope, exception=exc)
    dispatcher.emit.assert_called_once_with(
        Event("after_handler", {"scope": scope, "exception": exc})
    )


@pytest.mark.asyncio
async def test_on_error_with_exception_group(aggregator, dispatcher) -> None:
    scope = {"type": "http"}
    eg = ExceptionGroup("multi", [ValueError("a"), RuntimeError("b")])
    await aggregator.on_error(scope, eg)
    dispatcher.emit.assert_called_once_with(
        Event("error", {"scope": scope, "exception": eg})
    )


def test_all_level_b_events_covered() -> None:
    expected = {
        "on_app_startup", "on_app_shutdown",
        "on_request_received", "on_before_handler", "on_after_handler",
        "on_request_completed", "on_request_disconnected", "on_error",
        "on_websocket_connected", "on_websocket_message",
        "on_websocket_disconnected",
        "on_connection_accepted", "on_connection_closed",
        "on_message_received", "on_message_sent",
    }
    from blackbull.event_aggregator import EventAggregator
    actual = {name for name in dir(EventAggregator) if name.startswith("on_")}
    assert expected == actual


# ---------------------------------------------------------------------------
# has_any_request_listeners — the RequestActor fast-path guard
# ---------------------------------------------------------------------------

# Every event a request may emit between RequestActor.run() entry and exit.
# The fast path is only safe to take when NONE of these has a listener.
_REQUEST_PATH_EVENTS = {
    'request_received', 'before_handler', 'after_handler',
    'request_completed', 'request_disconnected', 'error',
}


def test_request_events_set_matches_emitted_events() -> None:
    """The fast-path guard must check exactly the events the request path can
    emit — no more, no fewer.  Adding a new request event without listing it
    here (the way ``error`` was originally missed) would silently bypass its
    listeners on the fast path."""
    assert set(EventAggregator._REQUEST_EVENTS) == _REQUEST_PATH_EVENTS


def test_has_any_request_listeners_false_when_empty() -> None:
    agg = EventAggregator(EventDispatcher())
    assert agg.has_any_request_listeners() is False


@pytest.mark.parametrize('event', sorted(_REQUEST_PATH_EVENTS))
def test_has_any_request_listeners_true_for_each_request_event(event) -> None:
    """Registering a listener for ANY request event (observer or interceptor)
    must disable the fast path.  Parametrised over ``error`` too — the bug was
    that an ``error``-only listener was bypassed."""
    d_obs = EventDispatcher()
    d_obs.on(event, AsyncMock())
    assert EventAggregator(d_obs).has_any_request_listeners() is True

    d_int = EventDispatcher()
    d_int.intercept(event, AsyncMock())
    assert EventAggregator(d_int).has_any_request_listeners() is True


@pytest.mark.parametrize('event', ['app_startup', 'websocket_message',
                                   'connection_accepted', 'message_received'])
def test_has_any_request_listeners_ignores_non_request_events(event) -> None:
    """A listener on a non-request event must NOT disable the fast path."""
    d = EventDispatcher()
    d.on(event, AsyncMock())
    assert EventAggregator(d).has_any_request_listeners() is False


# ---------------------------------------------------------------------------
# Caching — the per-request guard must not recompute the 6-event scan every call
# ---------------------------------------------------------------------------

def test_has_any_request_listeners_invalidates_when_listener_added_late() -> None:
    """A listener registered AFTER the first (cached) call must still flip the
    result — the cache is keyed on the dispatcher's listener generation."""
    d = EventDispatcher()
    agg = EventAggregator(d)
    assert agg.has_any_request_listeners() is False   # caches False
    d.on('before_handler', AsyncMock())               # bumps generation
    assert agg.has_any_request_listeners() is True     # recomputed, not stale


def test_has_any_request_listeners_is_cached_between_calls() -> None:
    """Repeat calls with no registration change must not re-scan the dispatcher."""
    d = EventDispatcher()
    agg = EventAggregator(d)
    agg.has_any_request_listeners()                   # prime the cache
    d.has_listeners = Mock(side_effect=AssertionError(
        'has_listeners must not be called again while the generation is unchanged'))
    assert agg.has_any_request_listeners() is False   # served from cache, no scan


def test_dispatcher_generation_bumps_on_registration() -> None:
    d = EventDispatcher()
    g0 = d.generation
    d.on('before_handler', AsyncMock())
    g1 = d.generation
    d.intercept('error', AsyncMock())
    g2 = d.generation
    assert g0 != g1 != g2 and len({g0, g1, g2}) == 3
