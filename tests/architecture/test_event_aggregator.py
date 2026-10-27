import pytest
from unittest.mock import AsyncMock
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
async def test_on_error_with_exception_group(aggregator, dispatcher) -> None:
    scope = {"type": "http"}
    eg = ExceptionGroup("multi", [ValueError("a"), RuntimeError("b")])
    await aggregator.on_error(scope, eg)
    dispatcher.emit.assert_called_once_with(
        Event("error", {"conn": scope, "exception": eg})
    )


def test_all_level_b_events_covered() -> None:
    expected = {
        "on_request_disconnected", "on_error",
        "on_websocket_connected", "on_websocket_message",
        "on_websocket_disconnected",
        "on_connection_accepted", "on_connection_closed",
        "on_message_received", "on_message_sent",
    }
    from blackbull.event_aggregator import EventAggregator
    actual = {name for name in dir(EventAggregator) if name.startswith("on_")}
    assert expected == actual


# ---------------------------------------------------------------------------
# The hot-path listener guards
# ---------------------------------------------------------------------------

_LISTENER_CHECKS = [
    ("has_websocket_message_listeners", "websocket_message"),
    ("has_request_completed_listeners", "request_completed"),
    ("has_request_disconnected_listeners", "request_disconnected"),
]


@pytest.mark.parametrize("method,event", _LISTENER_CHECKS)
def test_a_listener_guard_follows_registration(method, event) -> None:
    d = EventDispatcher()
    agg = EventAggregator(d)
    assert getattr(agg, method)() is False
    d.on('connection_accepted', AsyncMock())     # another event does not count
    assert getattr(agg, method)() is False
    d.on(event, AsyncMock())                     # a later registration does
    assert getattr(agg, method)() is True
