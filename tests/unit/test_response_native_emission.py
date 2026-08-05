"""`Response` and `StreamingResponse` emit native.

Both are BlackBull-owned serialisers on BlackBull's own send path, and both
emitted `http.response.*` dicts that `wrap_native_send` converted straight back
— the last response-dict round trip in the framework.

`Response` gets the bigger win: a complete response is **one object, one send**
(header and body together), where the dict form always cost two.
"""
import pytest

from blackbull.native import NativeResponse
from blackbull.response import (JSONResponse, RedirectResponse, Response,
                                StreamingResponse, _emit_response)


def _collector():
    seen = []

    async def send(event):
        seen.append(event)
    return seen, send


@pytest.mark.asyncio
async def test_response_is_one_object_one_send():
    seen, send = _collector()

    await Response('hello')(None, None, send)

    assert len(seen) == 1, (
        f'a complete response should be one send, got {len(seen)}')
    event = seen[0]
    assert isinstance(event, NativeResponse)
    assert event.status == 200
    assert event.body == b'hello'
    assert event.header is not None
    assert event.header.get(b'content-type') == b'text/html; charset=utf-8'
    assert event.more_body is False


@pytest.mark.asyncio
async def test_json_and_redirect_ride_the_same_path():
    seen, send = _collector()
    await JSONResponse({'a': 1})(None, None, send)
    await RedirectResponse('/there')(None, None, send)

    assert all(isinstance(e, NativeResponse) for e in seen)
    assert seen[0].body == b'{"a": 1}'
    assert seen[0].header.get(b'content-type') == b'application/json'
    assert seen[1].header.get(b'location') == b'/there'


@pytest.mark.asyncio
async def test_emit_response_helper_is_native():
    """The shared emitter — also the app's send(body, status, headers) form
    and the default error handler."""
    seen, send = _collector()

    await _emit_response(send, b'x', 404, [(b'a', b'1')])

    assert len(seen) == 1
    assert isinstance(seen[0], NativeResponse)
    assert seen[0].status == 404
    assert seen[0].body == b'x'


@pytest.mark.asyncio
async def test_streaming_response_is_header_then_chunks():
    async def gen():
        yield 'one'
        yield b'two'
        yield ''            # empty chunks are skipped, as before

    seen, send = _collector()
    await StreamingResponse(gen(), media_type='text/plain')(None, None, send)

    assert all(isinstance(e, NativeResponse) for e in seen), (
        f'streaming emitted {[type(e).__name__ for e in seen]}')
    header, *body = seen
    assert header.header is not None and header.body is None
    assert header.header.get(b'content-type') == b'text/plain'
    assert [e.body for e in body] == [b'one', b'two', b'']
    assert [e.more_body for e in body] == [True, True, False]


@pytest.mark.asyncio
async def test_streaming_keeps_a_caller_supplied_content_type():
    async def gen():
        yield 'x'

    seen, send = _collector()
    await StreamingResponse(gen(),
                            headers=[(b'content-type', b'text/event-stream')]
                            )(None, None, send)

    assert seen[0].header.get(b'content-type') == b'text/event-stream'
    assert len([h for h, _ in seen[0].header
                if h.lower() == b'content-type']) == 1, 'content-type duplicated'
