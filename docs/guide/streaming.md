# Streaming responses

BlackBull streams responses by yielding chunks from an async iterator.
The framework handles transport framing (HTTP/1.1 chunked encoding or
HTTP/2 DATA frames) and the backpressure model.  This page covers
both halves: the surface for writing a streaming handler, and the
model behind the curtain so you know what your code is actually
doing.

## The simplified shape: async generator

Return an async generator from a simplified handler and BlackBull
wraps it in a [`StreamingResponse`](#streamingresponse) for you:

```python
import asyncio
from blackbull import BlackBull

app = BlackBull()

@app.route(path='/feed')
async def feed():
    for i in range(10):
        yield f'line {i}\n'.encode()
        await asyncio.sleep(0.1)
```

Each `yield` becomes one body chunk.  `bytes` is preferred; `str`
is auto-encoded as UTF-8 for convenience.  No `await send(...)`
required, no `Response` object required.

The handler is still bounded by the normal request-timeout knobs —
`BB_REQUEST_TIMEOUT` and `BB_WRITE_TIMEOUT` apply to streaming
handlers the same way they apply to any other.

## `StreamingResponse`

For finer control, construct a `StreamingResponse` explicitly:

```python
from blackbull import StreamingResponse

@app.route(path='/feed')
async def feed():
    async def lines():
        for i in range(10):
            yield f'line {i}\n'
    return StreamingResponse(lines(), media_type='text/plain')
```

Constructor arguments:

| Argument | Default | Meaning |
|---|---|---|
| `content` | — | Async iterator producing `bytes` or `str` chunks. |
| `status` | `200` | HTTP status code on the response start. |
| `headers` | `[]` | Extra response headers as `[(bytes, bytes), ...]`. |
| `media_type` | `'text/plain'` | Content-Type header (overridable via *headers*). |

Pass it to `await send(...)` from a full-ASGI handler, or just
`return` it from a simplified handler.

## Server-Sent Events: `EventSourceResponse`

For browser-side `EventSource` clients, use the SSE convenience
class.  It formats each yielded item per the WHATWG SSE grammar,
forces `Content-Type: text/event-stream`, and sets
`Cache-Control: no-cache`:

```python
from blackbull import EventSourceResponse

@app.route(path='/sse')
async def sse():
    async def events():
        yield {'event': 'token', 'data': 'hello'}
        yield {'event': 'token', 'data': 'world'}
        yield {'event': 'done',  'data': ''}
    return EventSourceResponse(events())
```

Yielded items accept three shapes:

| Yield | Wire emission |
|---|---|
| `str` | `data: <text>\n\n` |
| `bytes` | `data: <text-utf8>\n\n` |
| `Mapping` | One field line per recognised key, blank-line terminator |

Recognised mapping keys are `data`, `event`, `id`, and `retry`.
`data` may be a string with embedded newlines (each line emits its
own `data:` field per the spec; the browser rejoins them with
`\n`).  A non-string `data` is JSON-serialised before the wire.
Unknown keys are ignored.

Override the default headers by passing your own `headers=[...]`
— a caller-supplied `cache-control` or `content-type` takes
precedence.

## The backpressure model

Streaming handlers fire one `await send({'type': 'http.response.body', ...})`
per chunk.  Both HTTP/1.1 and HTTP/2 paths apply transport-level
backpressure inside that `await`, so a slow client never causes
unbounded buffer growth in BlackBull.

### HTTP/1.1

`AsyncioWriter.write()` calls `asyncio.StreamWriter.drain()` on
every chunk
([`blackbull/server/sender.py`](https://github.com/TOKUJI/BlackBull/blob/master/blackbull/server/sender.py)).
`drain()` blocks while the kernel send buffer is full of bytes the
peer hasn't acknowledged, so the handler naturally stalls until
the peer pulls bytes off the socket.  `BB_WRITE_TIMEOUT` (default
30 s) bounds how long a single drain can wait — if a peer reads
1 byte/sec, the connection is closed before the handler hangs
indefinitely.

### HTTP/2

`HTTP2Sender._write_data()` enforces per-stream and per-connection
flow control per RFC 9113 §6.9.  Before writing each DATA frame
the sender waits on an `asyncio.Event` until either window has
credit:

```python
while (self.connection_window_size <= 0 or
       self.stream_window_size[self._stream_id] <= 0):
    if self._window_open is None:
        self._window_open = asyncio.Event()
    self._window_open.clear()
    await self._window_open.wait()
```

The handler's next `yield` only fires after the previous chunk's
`await send()` returns — and that return only happens after the
peer has granted enough WINDOW_UPDATE credit to put the chunk on
the wire.  No background queue, no buffering, no per-stream
unbounded growth.  `BB_REQUEST_TIMEOUT` bounds total per-stream
runtime if you want a hard ceiling on streams that produce
forever.

The unit test [`test_http2_sender_blocks_when_window_closed`](https://github.com/TOKUJI/BlackBull/blob/master/tests/unit/test_sse.py)
proves this experimentally: forcing both windows to zero before
the write starts produces zero wire bytes; signalling the
`_window_open` event releases the write.

## When SSE, when WebSocket, when chunked HTTP

| Pattern | Strengths | Weaknesses |
|---|---|---|
| **SSE** (`EventSourceResponse`) | Server → client only; auto-reconnect in browsers via `EventSource`; works through HTTP proxies; cleanly multiplexed over HTTP/2 streams. | One-way only.  Text-only payloads on the wire (binary needs base64). |
| **WebSocket** | Bidirectional; binary-native; lowest per-message framing overhead after the upgrade. | Most HTTP middleboxes treat WS as opaque; harder to debug; framework state on both sides. |
| **Chunked HTTP** (`StreamingResponse`) | One-way, request-scoped; uses normal HTTP semantics; trivially cacheable / proxied. | No event names, no client reconnect.  Use when you just want a long-running download, not a live event stream. |

Default pick: **SSE** when the client is a browser tab consuming
incremental updates, **WebSocket** when the client also pushes,
and **plain `StreamingResponse`** for non-browser HTTP clients
(LLM token streams over `httpx.stream`, log tail downloads, etc.).

## Caveats

- **Compression interaction**: the `Compression` middleware buffers
  the full body before deciding whether to compress; pair it with
  a route-specific exclusion if you want SSE delivered unbuffered.
- **`Content-Length`**: streaming responses do not set
  `content-length`; the transport advertises chunked encoding
  (HTTP/1.1) or stream framing (HTTP/2).  Clients that require a
  known length should buffer client-side.
- **Cancellation**: when the client disconnects, the handler's
  next `await send(...)` raises and the generator's `finally`
  blocks run.  Use a `try / finally` around any resource that
  outlives a single yield (DB cursors, subscriptions, …).
