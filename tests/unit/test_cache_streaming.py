"""A streamed response must reach the client once, in order, and uncached.

`middleware/cache.py` holds the header arm until the body completes so it
can attach an ETag and store the pair.  A streaming response has no
completion to wait for, so the middleware switches to pass-through — and
the switch was half made: it flushed what it held without clearing it, and
the terminal chunk flushed the same buffer again.

The assertions are on the events the client receives, because that is what
the defect corrupts.  A test on the middleware's own state would have
passed throughout: `streaming` is assigned in three places and read in
none.

Helpers mirror `test_cache_middleware.py` so the two files describe the
same seam the same way.
"""
from __future__ import annotations

import pytest

from blackbull.middleware.cache import Cache

pytestmark = pytest.mark.asyncio


def _scope(method: str = 'GET', path: str = '/', query: bytes = b'',
           headers: list[tuple[bytes, bytes]] | None = None):
    from blackbull.connection import Connection
    return Connection.from_scope({
        'type': 'http', 'method': method, 'path': path,
        'query_string': query, 'headers': list(headers or []),
    })


async def _run(mw, scope, call_next):
    sent: list = []

    async def send(event):
        from blackbull.native import NativeResponse
        if isinstance(event, NativeResponse):
            sent.extend(event.to_asgi())
        else:
            sent.append(event)

    await mw(scope, None, send, call_next)
    return sent


def _body_of(events) -> bytes:
    return b''.join(e.get('body', b'') for e in events
                    if isinstance(e, dict)
                    and e.get('type') == 'http.response.body')


def _starts_in(events) -> int:
    return sum(1 for e in events if isinstance(e, dict)
               and e.get('type') == 'http.response.start')


def _streaming_handler(chunks):
    counter = {'n': 0}

    async def call_next(scope, receive, send):
        counter['n'] += 1
        await send({'type': 'http.response.start', 'status': 200,
                    'headers': [(b'content-type', b'text/plain')]})
        for i, c in enumerate(chunks):
            await send({'type': 'http.response.body', 'body': c,
                        'more_body': i < len(chunks) - 1})

    return call_next, counter


class TestAStreamedBodyIsSentOnce:
    async def test_the_body_is_not_duplicated(self):
        call_next, _ = _streaming_handler([b'aa', b'bb', b'cc'])

        events = await _run(Cache(), _scope(path='/s'), call_next)

        assert _body_of(events) == b'aabbcc', (
            f'client received {_body_of(events)!r} — the response was sent '
            f'more than once, or chunks were withheld and replayed')

    async def test_the_response_head_is_sent_once(self):
        """Two `http.response.start` events is a protocol violation."""
        call_next, _ = _streaming_handler([b'aa', b'bb'])

        events = await _run(Cache(), _scope(path='/s'), call_next)

        assert _starts_in(events) == 1, (
            f'{_starts_in(events)} response-start events reached the client')

    async def test_chunk_order_survives(self):
        call_next, _ = _streaming_handler([b'1', b'2', b'3', b'4', b'5'])

        events = await _run(Cache(), _scope(path='/s'), call_next)

        assert _body_of(events) == b'12345'

    async def test_a_single_chunk_response_is_unaffected(self):
        """The non-streaming path keeps its behaviour."""
        call_next, _ = _streaming_handler([b'only'])

        events = await _run(Cache(), _scope(path='/s'), call_next)

        assert _body_of(events) == b'only'
        assert _starts_in(events) == 1


class TestAStreamedBodyIsNotCached:
    """The docstring says streaming is not cached; make that true.

    Buffering the whole stream to store it defeats streaming, and a later
    request would be served the buffered copy as if it were complete.
    """

    async def test_a_second_request_runs_the_handler_again(self):
        mw = Cache()
        call_next, counter = _streaming_handler([b'x', b'y'])

        first = await _run(mw, _scope(path='/s'), call_next)
        second = await _run(mw, _scope(path='/s'), call_next)

        assert _body_of(first) == b'xy'
        assert _body_of(second) == b'xy'
        assert counter['n'] == 2, (
            'the streamed response was cached and replayed')

    async def test_a_complete_response_is_still_cached(self):
        """The guard must not disable caching wholesale."""
        mw = Cache()
        call_next, counter = _streaming_handler([b'cached'])

        a = await _run(mw, _scope(path='/one'), call_next)
        b = await _run(mw, _scope(path='/one'), call_next)

        assert _body_of(a) == b'cached'
        assert _body_of(b) == b'cached'
        assert counter['n'] == 1, 'a complete response should have been cached'
