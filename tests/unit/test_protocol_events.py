"""Protocol-agnostic Level B events (Sprint 50).

Uses a real EventDispatcher with *interceptor* handlers (awaited inline by
emit) so emission is deterministic without draining fire-and-forget tasks.
"""
import pytest

from blackbull.event import EventDispatcher
from blackbull.event_aggregator import EventAggregator


@pytest.fixture
def dispatcher():
    return EventDispatcher()


@pytest.fixture
def aggregator(dispatcher):
    return EventAggregator(dispatcher)


def _capture(dispatcher, event_name):
    seen = []
    dispatcher.intercept(event_name, lambda e: seen.append(e) or _noop())
    return seen


async def _noop():
    return None


@pytest.mark.asyncio
async def test_connection_accepted_carries_protocol(aggregator, dispatcher):
    seen = _capture(dispatcher, 'connection_accepted')
    await aggregator.on_connection_accepted(('1.2.3.4', 5555), protocol='echo')
    assert len(seen) == 1
    assert seen[0].detail == {'peername': ('1.2.3.4', 5555), 'protocol': 'echo'}


@pytest.mark.asyncio
async def test_connection_accepted_defaults_to_http(aggregator, dispatcher):
    seen = _capture(dispatcher, 'connection_accepted')
    await aggregator.on_connection_accepted(('1.2.3.4', 5555))
    assert seen[0].detail['protocol'] == 'http'


@pytest.mark.asyncio
async def test_connection_closed(aggregator, dispatcher):
    seen = _capture(dispatcher, 'connection_closed')
    await aggregator.on_connection_closed(('1.2.3.4', 5555), 'echo', 12.5)
    assert seen[0].detail == {
        'peername': ('1.2.3.4', 5555), 'protocol': 'echo', 'duration_ms': 12.5}


@pytest.mark.asyncio
async def test_message_received_and_sent(aggregator, dispatcher):
    rx = _capture(dispatcher, 'message_received')
    tx = _capture(dispatcher, 'message_sent')
    await aggregator.on_message_received('mqtt', 'PUBLISH', 42)
    await aggregator.on_message_sent('mqtt', 'PUBACK', 4)
    assert rx[0].detail == {'protocol': 'mqtt', 'message_type': 'PUBLISH', 'payload_size': 42}
    assert tx[0].detail == {'protocol': 'mqtt', 'message_type': 'PUBACK', 'payload_size': 4}


@pytest.mark.asyncio
async def test_no_emit_without_listeners(aggregator, dispatcher):
    # No interceptor/observer registered → has_listeners() short-circuits, so
    # emit is never called.  We assert this by spying on emit.
    calls = []
    orig_emit = dispatcher.emit

    async def _spy(event):
        calls.append(event)
        await orig_emit(event)

    dispatcher.emit = _spy
    await aggregator.on_connection_accepted(('1.2.3.4', 5555), protocol='echo')
    await aggregator.on_connection_closed(('1.2.3.4', 5555), 'echo', 1.0)
    await aggregator.on_message_received('echo', 'data', 1)
    await aggregator.on_message_sent('echo', 'data', 1)
    assert calls == []
