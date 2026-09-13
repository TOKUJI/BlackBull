"""Server-Sent Events / streaming surface.

Three things to gate:

1. ``_format_sse_event`` produces wire bytes that match the WHATWG SSE
   grammar (data:/event:/id:/retry: fields, double-newline terminator,
   multi-line ``data`` splits).
2. ``EventSourceResponse`` constructs the right ASGI events: a
   ``http.response.start`` with ``text/event-stream`` + ``cache-control:
   no-cache``, then one body event per encoded SSE record, then a final
   empty body with ``more_body=False``.
3. The simplified-handler adapter recognises ``async def
   stream(): yield ...`` shape and wraps the async generator in a
   :class:`StreamingResponse` automatically.
"""
from __future__ import annotations

import json

import pytest

from blackbull import EventSourceResponse, StreamingResponse
from blackbull.response import _format_sse_event
from blackbull.native import NativeResponse


# ----------------------------------------------------------------------
# _format_sse_event — pure encoder
# ----------------------------------------------------------------------

def test_sse_format_str_emits_data_lines():
    assert _format_sse_event('hello') == b'data: hello\n\n'


def test_sse_format_bytes_emits_data_lines():
    assert _format_sse_event(b'hello') == b'data: hello\n\n'


def test_sse_format_mapping_emits_named_fields():
    out = _format_sse_event({
        'event': 'token',
        'id': '42',
        'retry': 3000,
        'data': 'hello',
    })
    # Spec order is unimportant for parsers but stable order keeps tests
    # readable.  The encoder emits event/id/retry/data in that order.
    assert out == b'event: token\nid: 42\nretry: 3000\ndata: hello\n\n'


def test_sse_format_multi_line_data_splits_lines():
    """WHATWG §9.2.6 — each ``\\n`` in the data string emits its own
    ``data:`` field; the client rejoins with ``\\n`` on receive."""
    out = _format_sse_event({'data': 'line1\nline2\nline3'})
    assert out == b'data: line1\ndata: line2\ndata: line3\n\n'


def test_sse_format_dict_data_is_json_serialised():
    """A non-string ``data`` value JSON-encodes for the wire."""
    out = _format_sse_event({'event': 'progress', 'data': {'pct': 42}})
    body = out.decode('utf-8')
    assert body.startswith('event: progress\n')
    assert 'data: ' in body
    data_line = next(ln for ln in body.split('\n') if ln.startswith('data:'))
    assert json.loads(data_line[len('data: '):]) == {'pct': 42}


def test_sse_format_omits_missing_fields():
    """Only the keys that are present should appear on the wire."""
    out = _format_sse_event({'data': 'ok'})
    assert out == b'data: ok\n\n'


def test_sse_format_id_coerced_to_string():
    out = _format_sse_event({'id': 7, 'data': 'x'})
    assert out == b'id: 7\ndata: x\n\n'


def test_sse_format_rejects_unsupported_type():
    with pytest.raises(TypeError):
        _format_sse_event(object())


# ----------------------------------------------------------------------
# EventSourceResponse — ASGI event stream
# ----------------------------------------------------------------------

class _AsgiCapture:
    """Collect every event passed to send() so we can assert on the stream shape."""
    def __init__(self):
        self.events: list[dict] = []

    async def __call__(self, event) -> None:
        # Responses emit native now; these assertions describe the wire, so
        # record the ASGI events each object stands for.
        if isinstance(event, NativeResponse):
            self.events.extend(event.to_asgi())
        else:
            self.events.append(event)


def _reference_sse_events(payload: bytes) -> list[dict[str, str]]:
    """Parse enough of WHATWG §9.2.6 to observe logical event boundaries."""
    text = payload.decode('utf-8')
    lines = text.replace('\r\n', '\n').replace('\r', '\n').split('\n')
    data = ''
    event_type = ''
    last_id = ''
    reconnect = ''
    events = []
    for line in lines:
        if line == '':
            if data:
                events.append({
                    'type': event_type or 'message',
                    'data': data[:-1],
                    'id': last_id,
                    'retry': reconnect,
                })
            data = ''
            event_type = ''
            continue
        if line.startswith(':'):
            continue
        field, separator, value = line.partition(':')
        if separator and value.startswith(' '):
            value = value[1:]
        if field == 'data':
            data += value + '\n'
        elif field == 'event':
            event_type = value
        elif field == 'id' and '\x00' not in value:
            last_id = value
        elif field == 'retry' and value.isdecimal():
            reconnect = value
    return events


async def _public_sse_wire(*items) -> bytes:
    async def src():
        for item in items:
            yield item

    cap = _AsgiCapture()
    await EventSourceResponse(src())(
        conn={'type': 'http'}, receive=None, send=cap)
    return b''.join(
        event.get('body', b'') for event in cap.events
        if event.get('type') == 'http.response.body'
    )


@pytest.mark.asyncio
async def test_event_source_response_emits_text_event_stream_content_type():
    async def src():
        yield 'hi'

    cap = _AsgiCapture()
    await EventSourceResponse(src())(conn={'type': 'http'},
                                     receive=None, send=cap)

    start = cap.events[0]
    assert start['type'] == 'http.response.start'
    assert start['status'] == 200
    headers = dict(start['headers'])
    assert headers[b'content-type'] == b'text/event-stream'
    assert headers[b'cache-control'] == b'no-cache'


@pytest.mark.asyncio
async def test_event_source_response_streams_one_body_event_per_yield():
    async def src():
        yield 'a'
        yield 'b'
        yield 'c'

    cap = _AsgiCapture()
    await EventSourceResponse(src())(conn={'type': 'http'},
                                     receive=None, send=cap)

    # 1 start + 3 body chunks + 1 final empty body = 5 events.
    assert len(cap.events) == 5
    body_chunks = [e for e in cap.events
                   if e.get('type') == 'http.response.body']
    assert body_chunks[0]['body'] == b'data: a\n\n'
    assert body_chunks[1]['body'] == b'data: b\n\n'
    assert body_chunks[2]['body'] == b'data: c\n\n'
    # Final chunk: empty + more_body=False.
    assert body_chunks[-1]['body'] == b''
    assert body_chunks[-1].get('more_body') is False


@pytest.mark.asyncio
async def test_event_source_response_explicit_headers_win():
    """A caller-supplied content-type / cache-control overrides the
    defaults — the existing StreamingResponse merge semantics carry over."""
    async def src():
        yield 'x'

    cap = _AsgiCapture()
    await EventSourceResponse(
        src(),
        headers=[(b'cache-control', b'public, max-age=10')],
    )(conn={'type': 'http'}, receive=None, send=cap)

    headers = dict(cap.events[0]['headers'])
    assert headers[b'cache-control'] == b'public, max-age=10'


@pytest.mark.asyncio
@pytest.mark.parametrize('line_ending', ['\n', '\r', '\r\n'])
async def test_event_source_response_preserves_all_sse_data_line_endings(
        line_ending):
    wire = await _public_sse_wire(f'first{line_ending}second')

    assert _reference_sse_events(wire) == [{
        'type': 'message', 'data': 'first\nsecond', 'id': '', 'retry': '',
    }]


@pytest.mark.asyncio
async def test_event_source_response_preserves_consecutive_and_trailing_lines():
    wire = await _public_sse_wire('first\r\r\n\nlast\r')

    assert _reference_sse_events(wire) == [{
        'type': 'message',
        'data': 'first\n\n\nlast\n',
        'id': '',
        'retry': '',
    }]


@pytest.mark.asyncio
@pytest.mark.parametrize(('field', 'value'), [
    ('event', 'intended\nevent: changed'),
    ('event', 'intended\revent: changed'),
    ('id', 'intended\nid: changed'),
    ('id', 'intended\rid: changed'),
    ('id', 'intended\x00changed'),
])
async def test_event_source_response_rejects_metadata_boundary_octets(
        field, value):
    with pytest.raises(ValueError, match=f'SSE {field}'):
        await _public_sse_wire({field: value, 'data': 'payload'})


@pytest.mark.asyncio
async def test_event_source_response_preserves_valid_metadata_and_unicode_data():
    wire = await _public_sse_wire({
        'event': 'token',
        'id': 'snowman-☃',
        'retry': 3000,
        'data': 'hello ☃',
    })

    assert _reference_sse_events(wire) == [{
        'type': 'token',
        'data': 'hello ☃',
        'id': 'snowman-☃',
        'retry': '3000',
    }]


@pytest.mark.asyncio
async def test_event_source_response_bytes_share_text_boundary_semantics():
    wire = await _public_sse_wire(b'first\rsecond')

    assert _reference_sse_events(wire) == [{
        'type': 'message', 'data': 'first\nsecond', 'id': '', 'retry': '',
    }]


@pytest.mark.asyncio
@pytest.mark.parametrize('data', [b'first\rsecond', bytearray(b'first\rsecond')])
async def test_event_source_mapping_bytes_share_text_boundary_semantics(data):
    wire = await _public_sse_wire({'data': data})

    assert _reference_sse_events(wire) == [{
        'type': 'message', 'data': 'first\nsecond', 'id': '', 'retry': '',
    }]


@pytest.mark.asyncio
async def test_event_source_response_rejects_invalid_utf8_bytes():
    with pytest.raises(UnicodeDecodeError):
        await _public_sse_wire(b'\xff')


# ----------------------------------------------------------------------
# Simplified-handler adapter — async generator return
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_simplified_handler_async_generator_wraps_in_streaming_response():
    """A handler shaped ``async def stream(): yield ...`` should drive
    a StreamingResponse without the caller doing the wrap themselves."""
    from blackbull.router import _adapt_handler

    async def stream():
        yield b'a'
        yield b'b'

    adapted = _adapt_handler(stream, '/x')
    cap = _AsgiCapture()
    await adapted({'type': 'http', 'path_params': {}}, None, cap)

    types = [e['type'] for e in cap.events]
    assert types[0] == 'http.response.start'
    assert types[-1] == 'http.response.body'
    # Body chunks 'a' and 'b' + empty close.
    bodies = [e['body'] for e in cap.events if e['type'] == 'http.response.body']
    assert b'a' in bodies and b'b' in bodies
    assert cap.events[-1].get('more_body') is False


@pytest.mark.asyncio
async def test_simplified_handler_returning_streaming_response_passes_through():
    """When the handler returns a StreamingResponse explicitly, the
    adapter must call it instead of wrapping its instance as a Response
    (which would crash on the unexpected type)."""
    from blackbull.router import _adapt_handler

    async def src():
        yield b'token'

    async def handler():
        return StreamingResponse(src(), media_type='text/plain')

    adapted = _adapt_handler(handler, '/x')
    cap = _AsgiCapture()
    await adapted({'type': 'http', 'path_params': {}}, None, cap)

    assert cap.events[0]['type'] == 'http.response.start'
    body_events = [e for e in cap.events if e['type'] == 'http.response.body']
    assert any(e.get('body') == b'token' for e in body_events)


# ----------------------------------------------------------------------
# HTTP/2 backpressure — _write_data blocks on credit
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_http2_sender_blocks_when_window_closed():
    """When the peer's flow-control window is exhausted, ``_write_data``
    must wait on ``_window_open`` instead of pushing more bytes onto the
    wire.  This is what makes ``yield``-driven streams honour
    backpressure: each ``await send(http.response.body)`` flows through
    here and naturally throttles to the credit the peer has granted.
    """
    import asyncio
    from blackbull.server.sender import HTTP2Sender, AbstractWriter
    from blackbull.protocol.frame import FrameFactory

    class _RecordingWriter(AbstractWriter):
        def __init__(self):
            self.chunks = bytearray()
        async def write(self, data: bytes) -> None:
            self.chunks += data
        async def writelines(self, parts) -> None:
            for p in parts:
                self.chunks += p
        async def close(self) -> None:
            pass

    writer = _RecordingWriter()
    sender = HTTP2Sender(writer, FrameFactory(), stream_id=1)
    # Force the peer's window closed BEFORE the write starts.
    sender.connection_window_size = 0
    sender.stream_window_size = 0

    write_done = asyncio.Event()

    async def do_write():
        await sender._write_data(b'x' * 16, end_stream=True)
        write_done.set()

    task = asyncio.create_task(do_write())
    try:
        # Give the loop a couple of ticks so the writer enters its
        # credit-wait loop without producing any DATA bytes.
        for _ in range(5):
            await asyncio.sleep(0)
        assert not write_done.is_set(), 'expected _write_data to block on credit'
        assert writer.chunks == b'', 'expected no DATA bytes before credit grant'

        # Reopen the window and signal the waiter — the write should now
        # run to completion and put the full payload on the wire.
        sender.connection_window_size = 100
        sender.stream_window_size = 100
        assert sender._window_open is not None
        sender._window_open.set()
        await asyncio.wait_for(write_done.wait(), timeout=1.0)
        assert b'xxxx' in bytes(writer.chunks)
    finally:
        # Awaiting the task drains its result and surfaces any exception
        # the writer raised — without this, an exception inside do_write
        # would be lost and CodeQL flags ``task`` as unused.
        await task


@pytest.mark.asyncio
async def test_simplified_handler_returning_event_source_response_passes_through():
    """The EventSourceResponse subclass must take the StreamingResponse
    branch, not the dispatch-by-isinstance Response branch."""
    from blackbull.router import _adapt_handler

    async def src():
        yield {'event': 'ping', 'data': 'pong'}

    async def handler():
        return EventSourceResponse(src())

    adapted = _adapt_handler(handler, '/x')
    cap = _AsgiCapture()
    await adapted({'type': 'http', 'path_params': {}}, None, cap)

    headers = dict(cap.events[0]['headers'])
    assert headers[b'content-type'] == b'text/event-stream'
    bodies = [e['body'] for e in cap.events
              if e['type'] == 'http.response.body' and e.get('body')]
    assert any(b'event: ping' in b for b in bodies)
    assert any(b'data: pong' in b for b in bodies)
