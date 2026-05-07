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
        "on_connection_accepted",
    }
    from blackbull.event_aggregator import EventAggregator
    actual = {name for name in dir(EventAggregator) if name.startswith("on_")}
    assert expected == actual
