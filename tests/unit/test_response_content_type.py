from copy import deepcopy

import pytest

from blackbull import EventSourceResponse, JSONResponse, RedirectResponse, Response, StreamingResponse


async def _chunks():
    yield b'hello'


def _response(kind, headers):
    if kind == 'response':
        return Response('hello', content_type='application/factory', headers=headers)
    if kind == 'json':
        return JSONResponse({'ok': True}, headers=headers)
    if kind == 'redirect':
        return RedirectResponse('/target', headers=headers)
    if kind == 'stream':
        return StreamingResponse(_chunks(), media_type='application/factory', headers=headers)
    return EventSourceResponse(_chunks(), headers=headers)


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['response', 'json', 'redirect', 'stream', 'sse'])
@pytest.mark.parametrize('headers', [
    {b'content-type': b'application/custom'},
    {'Content-Type': 'application/custom'},
    [(b'x-before', b'1'), ('cOnTeNt-TyPe', 'application/custom'),
     (b'set-cookie', b'a=1'), (b'set-cookie', b'b=2')],
    [(bytearray(b'Content-Type'), bytearray(b'application/custom'))],
])
async def test_explicit_content_type_overrides_factory_type(kind, headers):
    original = deepcopy(headers)
    events = []

    async def send(event):
        events.extend(event.to_asgi())

    await _response(kind, headers)(None, None, send)
    start = next(e for e in events if e['type'] == 'http.response.start')
    assert [v for k, v in start['headers'] if k == b'content-type'] == [b'application/custom']
    assert headers == original
    if isinstance(headers, list) and len(headers) == 4:
        assert [v for k, v in start['headers'] if k == b'set-cookie'] == [b'a=1', b'b=2']


@pytest.mark.asyncio
@pytest.mark.parametrize('kind,expected', [
    ('response', b'application/factory'), ('json', b'application/json'),
    ('redirect', b'text/html; charset=utf-8'), ('stream', b'application/factory'),
    ('sse', b'text/event-stream'),
])
async def test_factory_type_is_used_when_not_explicit(kind, expected):
    events = []

    async def send(event):
        events.extend(event.to_asgi())

    await _response(kind, [(b'x-other', b'1')])(None, None, send)
    start = next(e for e in events if e['type'] == 'http.response.start')
    assert [v for k, v in start['headers'] if k == b'content-type'] == [expected]


@pytest.mark.parametrize('kind', ['response', 'json', 'redirect', 'stream', 'sse'])
@pytest.mark.parametrize('second', [b'application/custom', b'text/plain'])
def test_multiple_explicit_content_types_are_rejected(kind, second):
    with pytest.raises(ValueError, match='Content-Type'):
        _response(kind, [(b'Content-Type', b'application/custom'), (b'content-type', second)])


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['response', 'json', 'redirect', 'stream', 'sse'])
async def test_empty_explicit_content_type_does_not_add_factory_type(kind):
    events = []

    async def send(event):
        events.extend(event.to_asgi())

    await _response(kind, [(b'content-type', b'')])(None, None, send)
    start = next(e for e in events if e['type'] == 'http.response.start')
    assert [v for k, v in start['headers'] if k == b'content-type'] == [b'']
