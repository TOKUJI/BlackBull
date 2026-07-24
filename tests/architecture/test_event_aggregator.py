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
async def test_on_error_with_exception_group(aggregator, dispatcher) -> None:
    scope = {"type": "http"}
    eg = ExceptionGroup("multi", [ValueError("a"), RuntimeError("b")])
    await aggregator.on_error(scope, eg)
    dispatcher.emit.assert_called_once_with(
        Event("error", {"conn": scope, "exception": eg})
    )


def test_all_level_b_events_covered() -> None:
    expected = {
        "on_app_startup", "on_app_shutdown",
        "on_request_disconnected", "on_error",
        "on_websocket_connected", "on_websocket_message",
        "on_websocket_disconnected",
        "on_connection_accepted", "on_connection_closed",
        "on_message_received", "on_message_sent",
    }
    from blackbull.event_aggregator import EventAggregator
    actual = {name for name in dir(EventAggregator) if name.startswith("on_")}
    assert expected == actual


def test_dispatcher_generation_bumps_on_registration() -> None:
    d = EventDispatcher()
    g0 = d.generation
    d.on('before_handler', AsyncMock())
    g1 = d.generation
    d.intercept('error', AsyncMock())
    g2 = d.generation
    assert g0 != g1 != g2 and len({g0, g1, g2}) == 3


# ---------------------------------------------------------------------------
# has_websocket_message_listeners — the WebSocket receive hot-path guard
# ---------------------------------------------------------------------------

def test_has_websocket_message_listeners_false_when_empty() -> None:
    agg = EventAggregator(EventDispatcher())
    assert agg.has_websocket_message_listeners() is False


def test_has_websocket_message_listeners_true_when_registered() -> None:
    d = EventDispatcher()
    d.on('websocket_message', AsyncMock())
    assert EventAggregator(d).has_websocket_message_listeners() is True


def test_has_websocket_message_listeners_ignores_other_events() -> None:
    """A listener on some *other* event must not flip the WS-receive guard."""
    d = EventDispatcher()
    d.on('request_received', AsyncMock())
    assert EventAggregator(d).has_websocket_message_listeners() is False


def test_has_websocket_message_listeners_invalidates_late_registration() -> None:
    d = EventDispatcher()
    agg = EventAggregator(d)
    assert agg.has_websocket_message_listeners() is False  # caches False
    d.on('websocket_message', AsyncMock())                 # bumps generation
    assert agg.has_websocket_message_listeners() is True    # recomputed


def test_has_websocket_message_listeners_is_cached_between_calls() -> None:
    d = EventDispatcher()
    agg = EventAggregator(d)
    agg.has_websocket_message_listeners()                  # prime the cache
    d.has_listeners = Mock(side_effect=AssertionError(
        'has_listeners must not be called again while generation is unchanged'))
    assert agg.has_websocket_message_listeners() is False  # served from cache
