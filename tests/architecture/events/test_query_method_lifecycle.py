"""Request-lifecycle events for HTTP QUERY requests (RFC 10008, Sprint 78).

The four lifecycle events (`request_received`, `before_handler`,
`after_handler`, `request_completed`) must fire exactly once per QUERY
request, same as any IANA method — QUERY dispatches through the raw-str
fallback in ``BlackBull._dispatch`` (no ``http.HTTPMethod`` member until
≥3.16), and that path must not skip or double-fire the events.
"""
import asyncio

import pytest

from blackbull import BlackBull
from blackbull.event import Event

from blackbull.server.http1_actor import HTTP1Actor

from .test_request_received_event import _FakeReader, _FakeWriter


_LIFECYCLE_EVENTS = ('request_received', 'before_handler',
                     'after_handler', 'request_completed')


async def _run_query_request(app, path: str = '/search',
                             body: bytes = b'select *') -> None:
    # HTTP1Actor's ``request=`` carries the header block only; the body is
    # read from the reader (mirrors the keep-alive loop's readuntil split).
    head = (f'QUERY {path} HTTP/1.1\r\n'
            f'Host: localhost:8000\r\n'
            f'Content-Type: text/plain\r\n'
            f'Content-Length: {len(body)}\r\n\r\n')
    actor = HTTP1Actor(
        _FakeReader(body), _FakeWriter(), app, None,
        request=head.encode(),
        peername=('127.0.0.1', 54321),
        sockname=('0.0.0.0', 8000),
    )
    await actor.run()


@pytest.mark.asyncio
async def test_lifecycle_events_fire_exactly_once_for_query():
    app = BlackBull()
    captured: dict[str, list[Event]] = {name: [] for name in _LIFECYCLE_EVENTS}
    done = asyncio.Event()

    for name in _LIFECYCLE_EVENTS:
        @app.on(name)
        async def observer(event: Event, _name=name):
            captured[_name].append(event)
            if _name == 'request_completed':
                done.set()

    @app.route(path='/search', methods=['QUERY'])
    async def search(scope, receive, send):
        await send({'type': 'http.response.start', 'status': 200, 'headers': []})
        await send({'type': 'http.response.body', 'body': b'ok', 'more_body': False})

    await _run_query_request(app)
    await asyncio.wait_for(done.wait(), timeout=2.0)
    await asyncio.sleep(0.2)        # settle window for late duplicates

    for name in _LIFECYCLE_EVENTS:
        assert len(captured[name]) == 1, (
            f'{name} fired {len(captured[name])} times for a QUERY request; '
            f'expected exactly 1')


@pytest.mark.asyncio
async def test_lifecycle_event_detail_reports_query_method():
    app = BlackBull()
    captured: list[Event] = []
    seen = asyncio.Event()

    @app.on('request_received')
    async def observer(event: Event):
        captured.append(event)
        seen.set()

    @app.route(path='/search', methods=['QUERY'])
    async def search(scope, receive, send):
        await send({'type': 'http.response.start', 'status': 200, 'headers': []})
        await send({'type': 'http.response.body', 'body': b'ok', 'more_body': False})

    await _run_query_request(app)
    await asyncio.wait_for(seen.wait(), timeout=2.0)
    await asyncio.sleep(0.2)
    assert len(captured) == 1

    d = captured[0].detail
    assert d['method'] == 'QUERY'
    assert d['path'] == '/search'
    assert d['scope'].method == 'QUERY'
