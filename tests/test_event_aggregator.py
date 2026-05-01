import pytest
from unittest.mock import AsyncMock
from blackbull.event_aggregator import EventAggregator


@pytest.fixture
def dispatcher():
    d = AsyncMock()
    d.emit = AsyncMock()
    d.emit_intercept = AsyncMock()
    return d


@pytest.fixture
def aggregator(dispatcher):
    return EventAggregator(dispatcher)


@pytest.mark.asyncio
async def test_on_app_startup(aggregator, dispatcher) -> None:
    await aggregator.on_app_startup()
    dispatcher.emit.assert_called_once_with("app_startup", {})


@pytest.mark.asyncio
async def test_on_request_received(aggregator, dispatcher) -> None:
    scope = {"type": "http", "path": "/"}
    await aggregator.on_request_received(scope)
    dispatcher.emit.assert_called_once_with(
        "request_received", {"scope": scope}
    )


@pytest.mark.asyncio
async def test_on_before_handler(aggregator, dispatcher) -> None:
    scope, receive, send, call_next = {}, AsyncMock(), AsyncMock(), AsyncMock()
    await aggregator.on_before_handler(scope, receive, send, call_next)
    dispatcher.emit_intercept.assert_called_once_with(
        "before_handler", scope, receive, send, call_next
    )


@pytest.mark.asyncio
async def test_on_after_handler_success(aggregator, dispatcher) -> None:
    scope = {"type": "http"}
    await aggregator.on_after_handler(scope)
    dispatcher.emit.assert_called_once_with(
        "after_handler", {"scope": scope, "exception": None}
    )


@pytest.mark.asyncio
async def test_on_after_handler_with_exception(aggregator, dispatcher) -> None:
    scope = {"type": "http"}
    exc = ValueError("boom")
    await aggregator.on_after_handler(scope, exception=exc)
    dispatcher.emit.assert_called_once_with(
        "after_handler", {"scope": scope, "exception": exc}
    )


@pytest.mark.asyncio
async def test_on_error_with_exception_group(aggregator, dispatcher) -> None:
    scope = {"type": "http"}
    eg = ExceptionGroup("multi", [ValueError("a"), RuntimeError("b")])
    await aggregator.on_error(scope, eg)
    dispatcher.emit.assert_called_once_with(
        "error", {"scope": scope, "exception": eg}
    )


def test_all_level_b_events_covered() -> None:
    expected = {
        "on_app_startup", "on_app_shutdown",
        "on_request_received", "on_before_handler", "on_after_handler",
        "on_request_completed", "on_request_disconnected", "on_error",
        "on_websocket_connected", "on_websocket_message",
        "on_websocket_disconnected",
    }
    from blackbull.event_aggregator import EventAggregator
    actual = {name for name in dir(EventAggregator) if name.startswith("on_")}
    assert expected == actual
