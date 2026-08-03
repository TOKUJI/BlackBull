# WebSockets

BlackBull serves WebSocket connections over HTTP/1.1 `Upgrade`
(RFC 6455) by default, and over HTTP/2 Extended CONNECT (RFC 8441)
as an opt-in.  `permessage-deflate` (RFC 7692) compression is
negotiated automatically.

## Registering a route

WebSocket routes use `scheme=Scheme.websocket`.  Declare a `WebSocket`
parameter and the framework hands you the connection as an object:

```python
from blackbull import BlackBull, WebSocket
from blackbull.utils import Scheme

app = BlackBull()

@app.route(path='/ws', scheme=Scheme.websocket)
async def ws_handler(ws: WebSocket):
    await ws.accept()
    async for message in ws:
        await ws.send(message)
```

That is the whole echo server.  The loop ends when the client goes away —
no sentinel event to test for, and no `try`/`except` around it.

`Sec-WebSocket-Version: 13` is validated automatically.

### The API

| Call | Does |
|---|---|
| `await ws.accept(subprotocol=None, headers=None)` | Completes the handshake.  Nothing may be sent before it. |
| `await ws.close(code=1000, reason=None)` | Closes.  Called *before* `accept()`, it rejects the connection instead.  Idempotent, so `finally: await ws.close()` is safe. |
| `await ws.send_text(str)` / `send_bytes(bytes)` | Sends one complete message. |
| `await ws.send_json(obj, binary=False)` | Serialises and sends. |
| `await ws.send(str \| bytes)` | Picks text or binary from the Python type. |
| `await ws.receive()` | One message: `str` for text, `bytes` for binary.  Raises `WebSocketDisconnect` when the peer closes. |
| `await ws.receive_text()` / `receive_bytes()` / `receive_json()` | Same, requiring a particular kind. |
| `async for message in ws` | Iterates messages; **ends** at disconnect rather than raising. |

Connection facts are on the object too — `ws.path`, `ws.headers`,
`ws.path_params`, `ws.query_string`, `ws.client`, `ws.subprotocols` — and
the full [`Connection`](requests-and-responses.md) is `ws.connection`.  State
is readable via `ws.accepted`, `ws.client_disconnected`, and `ws.close_code`.

The parameter is matched by annotation first, so `ws: WebSocket` works under
any name.  Un-annotated, the names `ws` and `websocket` are recognised.  You
can take the `Connection` alongside it:

```python
@app.route(path='/room/{name}', scheme=Scheme.websocket)
async def room(ws: WebSocket, conn: Connection):
    await ws.accept()
    await ws.send_text(f'welcome to {conn.path_params["name"]}')
```

A parameter that is none of the recognised kinds is a `TypeError` **at
registration**, not on the first connection.

### Injected parameters

A WebSocket handler declares what it needs the same way an HTTP handler does
— path params, query params, and `Depends` all resolve from the signature:

```python
@app.route(path='/rooms/{room}', scheme=Scheme.websocket)
async def chat(ws: WebSocket, room: str, since: int = 0,
               db=Depends(get_db)):
    await ws.accept()
    await ws.send_text(f'{room} from {since}')
```

| Declared | Resolves to |
|---|---|
| Name matches a `{param}` in the path | `ws.path_params[name]`, coerced to the annotation if given |
| Any other annotated `str`/`int`/`float`/`bool` (optionally `\| None`) | The query param of that name |
| `Depends(provider)` default | The provider's value, once per connection |

Two differences from the HTTP form are worth knowing:

**A query param must carry its annotation.** On an HTTP route a bare name is
taken as a `str` query param; on a WebSocket it is a `TypeError`. The reserved
names make bare parameters ambiguous — `async def chat(socket)` almost
certainly means the socket, not a query param called `socket` — so the
annotation is required and a typo fails at registration instead of rejecting
every connection at runtime.

**There is no body parameter.** A WebSocket has no request body, so the HTTP
`body` and dataclass-body forms have no WebSocket equivalent.

When a declared parameter cannot be bound — a required query param missing, a
value that will not coerce — the handshake is **refused with close code 1008**
(policy violation) and the handler never runs. The HTTP path answers the same
failure with `400`; a WebSocket has no response to put a status on.

#### Dependency lifetime — read this before injecting a database handle

A `Depends` on a WebSocket is resolved **once per connection** and released
when the handler exits. That is the correct scope for values — an
authenticated user, a parsed token, per-connection config — and the **wrong**
scope for scarce resources:

!!! warning "A socket holds its dependency for hours, not milliseconds"

    An HTTP request holds a pooled connection for the duration of the
    response.  A WebSocket holds it for the life of the socket.  A pool of 20
    therefore serves 20 *concurrent sockets*, and the 21st client blocks
    until someone disconnects.  Worse, a pinned connection sitting idle gets
    reaped underneath you — MySQL `wait_timeout`, PgBouncer, and AWS NAT
    idle timeouts all drop long-idle connections, so the handler wakes up
    holding a dead one.

Give the *pool* application scope and borrow from it per use instead:

```python
@app.on_startup
async def open_pool():
    app.state.pool = await asyncpg.create_pool(DSN)

@app.route(path='/rooms/{room}', scheme=Scheme.websocket)
async def chat(ws: WebSocket, room: str, user=Depends(current_user)):
    await ws.accept()
    async for message in ws:                       # user: per connection ✓
        async with app.state.pool.acquire() as db:  # db: per use ✓
            await db.execute('insert into messages …', room, message)
```

**Write cleanup in a `finally`.** Teardown runs whenever the handler exits —
clean close, `WebSocketDisconnect`, or an exception — but cleanup written
*after* a bare `yield` is skipped on the exception paths, because the
exception is thrown into the generator at the `yield`. This is ordinary
`@asynccontextmanager` behaviour and it bites harder here, since a socket ends
by exception far more often than a request does:

```python
async def get_conn():
    conn = await pool.acquire()
    try:
        yield conn
    finally:                    # runs on disconnect and on error alike
        await pool.release(conn)
```

### Rejecting a connection

Call `close()` without accepting.  The client's `connect()` fails outright,
rather than seeing a connection open and immediately shut:

```python
@app.route(path='/private', scheme=Scheme.websocket)
async def private(ws: WebSocket):
    if not authorized(ws.headers):
        await ws.close(4401, 'unauthorized')
        return
    await ws.accept()
    ...
```

### Catching the close code

`async for` swallows the disconnect because most loops do not care why the
peer left.  When you do, call `receive()` directly:

```python
from blackbull import WebSocketDisconnect

@app.route(path='/audited', scheme=Scheme.websocket)
async def audited(ws: WebSocket):
    await ws.accept()
    try:
        while True:
            await ws.send_text(await ws.receive_text())
    except WebSocketDisconnect as exc:
        logger.info('closed: %s %s', exc.code, exc.reason)
```

## The raw event form

The `(conn, receive, send)` triplet keeps working exactly as before, and is
**not deprecated** — it stays supported for at least a year past the release
that introduced the object (v0.63.0), and there is no plan to remove it.
Reach for it when you need to see the events themselves: writing middleware,
driving the handshake in an unusual order, or handling an event the object
does not model.

```python
@app.route(path='/ws-raw', scheme=Scheme.websocket)
async def ws_raw(conn, receive, send):
    await receive()                          # consume 'websocket.connect'
    await send({'type': 'websocket.accept'})
    while True:
        event = await receive()
        if event['type'] == 'websocket.disconnect':
            break
        text = event.get('text') or event.get('bytes', b'').decode()
        await send({'type': 'websocket.send', 'text': text})
```

The two forms are the same connection seen at different levels: the object's
methods emit exactly these events, so framing, fragmentation, and close
semantics are identical either way.  A route is classified once, at
registration, by whether its signature contains both `receive` and `send`.

## The `websocket` middleware

The built-in `blackbull.middleware.websocket` consumes the initial
`websocket.connect` event and sends `websocket.accept`, so the
inner handler can skip that boilerplate:

```python
from blackbull.middleware import websocket

@app.route(path='/chat', scheme=Scheme.websocket, middlewares=[websocket])
async def chat(scope, receive, send):
    # Connection already accepted; go straight to reading messages
    while True:
        event = await receive()
        if event['type'] == 'websocket.disconnect':
            break
        await send({'type': 'websocket.send', 'text': event.get('text', '')})
```

It works with the `WebSocket` object too — it records the completed
handshake on the connection, and the object adopts that state instead of
waiting for a `websocket.connect` that has already been consumed:

```python
@app.route(path='/chat', scheme=Scheme.websocket, middlewares=[websocket])
async def chat(ws: WebSocket):
    async for message in ws:        # already accepted
        await ws.send(message)
```

A bare `await ws.accept()` in that handler is tolerated as a no-op, so the
same body works whether or not the middleware is on the route.  Asking for
something the middleware cannot retroactively provide —
`await ws.accept('chat')`, or extra headers — raises instead, since the 101
has already gone out.  Likewise, if the handler closes the connection
itself, the middleware does not append a second close.

With the object form the middleware is largely redundant: `await
ws.accept()` is the one line it was removing.

!!! note "Writing middleware that touches the handshake"

    Middleware that takes the `websocket.connect` event off the receive
    channel must record it, or a downstream `WebSocket` object will read the
    client's first *message* expecting the handshake.  Which call depends on
    how far the middleware went:

    | Middleware did | Call | The object then |
    |---|---|---|
    | Read connect **and** sent `websocket.accept` | `mark_handshake_accepted(conn)` | Starts accepted; a bare `accept()` is a no-op |
    | Read connect only, left accepting to the handler | `mark_connect_consumed(conn)` | Doesn't wait for connect, still sends the accept |

    Both live in `blackbull.websocket`.  The second is the shape an auth
    middleware wants — pop connect so you keep the option of rejecting with
    a close code, then delegate; `examples/ChatServer/chatserver.py`'s
    `auth_mw` does exactly that.  The distinction is not cosmetic: marking a
    merely-consumed connection as *accepted* would make the handler skip its
    own `accept()`, leaving the client hanging on a handshake nobody
    completed.  Omit both and the object raises, naming each.

## Typed WebSocket events

These apply to the raw form; the `WebSocket` object hides the events
entirely, and constructs them itself against these same declarations.

The events are plain ASGI dicts on the wire, but their shapes are declared
as `TypedDict`s, so a type checker can narrow them on the `type` key:

```python
from blackbull import ASGIReceiveCallable, ASGISendCallable
from blackbull.asgi import ASGIEvent

async def chat(conn, receive: ASGIReceiveCallable, send: ASGISendCallable):
    while True:
        event = await receive()
        if event['type'] == ASGIEvent.WS_DISCONNECT:
            break
        if event['type'] == ASGIEvent.WS_RECEIVE:
            # Narrowed to WebSocketReceiveEvent: `text` and `bytes` are known
            # keys here, and both are Optional — BlackBull always sets both,
            # with one of them None.
            await send({'type': 'websocket.send', 'text': event.get('text') or ''})
```

`FragmentAssembler` has already reassembled fragments by this point, so a
`websocket.receive` event is always one complete message — which is why the
declared type has `text: str | None` and `bytes: bytes | None` rather than
modelling a partial frame.  The declarations change nothing at runtime.

## permessage-deflate (RFC 7692)

`permessage-deflate` compression is negotiated automatically when
the client offers it on the handshake.  The server replies with
`Sec-WebSocket-Extensions: permessage-deflate;
server_no_context_takeover; client_no_context_takeover` — the
no-context-takeover flags trade a small compression-ratio penalty
for bounded per-connection memory (each side resets its deflate
state between messages instead of keeping it for the whole
connection).

| Aspect | Behaviour |
|---|---|
| Default | On — matches modern browsers, Node `ws`, Python `websockets`, aiohttp. |
| Disable | `BB_WS_PERMESSAGE_DEFLATE=0`.  The handshake still succeeds; just no extension is negotiated. |
| Per-message-deflate strategy | Both `server_no_context_takeover` and `client_no_context_takeover` always advertised. |
| RSV1 bit | Set on compressed data frames per §7 of the RFC; clients without the negotiated extension that send RSV1 are rejected as protocol violations. |

## Transport: HTTP/1.1 Upgrade vs HTTP/2 Extended CONNECT

WebSocket is always available over the HTTP/1.1 `Upgrade`
handshake (RFC 6455 §4).  Over HTTP/2 it is **opt-in** via
Extended CONNECT (RFC 8441):

```bash
BB_H2_ENABLE_WEBSOCKET=1 python app.py --port 8443 --cert cert.pem --key key.pem
```

When enabled the server advertises
`SETTINGS_ENABLE_CONNECT_PROTOCOL=1` in its initial SETTINGS
frame.  An HTTP/2 peer may then open a WebSocket by sending
`:method = CONNECT`, `:protocol = websocket`, and the usual
`Sec-WebSocket-*` pseudo-headers on a single stream.  The
bidirectional DATA frames on that stream then carry WebSocket
frames.

This path is off by default because it has fewer conformance
tests than the HTTP/1.1 Upgrade path and few clients use it in
practice — Cloudflare's edge stack is the main consumer.
Browsers that negotiate HTTP/2 via ALPN normally still use
HTTP/1.1 for WebSocket, so most apps do not need to enable
RFC 8441.

## Subprotocol negotiation

Register the protocols the server supports before starting:

```python
app.available_ws_protocols = ['chat', 'superchat']
```

BlackBull picks the first protocol from the client's
`Sec-WebSocket-Protocol` offer that appears in this list and
returns it in the 101 handshake response.  If there is no match,
or if the client did not offer any protocol, no
`Sec-WebSocket-Protocol` header is sent and the connection
proceeds without a subprotocol.

The list accepts `str` or `bytes` values.  Common protocol names:

| Protocol | Use case |
|---|---|
| `graphql-ws` | Legacy GraphQL subscriptions (Apollo) |
| `graphql-transport-ws` | Modern GraphQL subscriptions |
| `stomp` / `v12.stomp` | STOMP messaging (RabbitMQ, ActiveMQ) |
| `mqtt` | MQTT over WebSocket (IoT) |
| `wamp` | Web Application Messaging Protocol |
| `ocpp1.6` / `ocpp2.0` | EV charging stations |

## Fragmented messages

WebSocket clients may split a single logical message across
multiple frames (RFC 6455 §5.4).  BlackBull reassembles fragments
transparently — the app always receives one `websocket.receive`
event containing the full payload, regardless of how many frames
the client used.

A fragmented sequence on the wire:

```
FIN=0, opcode=TEXT,  payload=b'hel'   ← opener
FIN=0, opcode=0x0,   payload=b'lo'    ← continuation
FIN=1, opcode=0x0,   payload=b''      ← final continuation
```

The app sees a single event:

```python
{'type': 'websocket.receive', 'text': 'hello', 'bytes': None}
```

— or, through the `WebSocket` object, one iteration of `async for` yielding
the `str` `'hello'`.

Control frames (ping, pong, close) may legally appear between
data fragments; BlackBull handles them immediately (responding
to pings with pong) and then continues reassembling the
fragmented message.

The following are protocol violations and raise
`ProtocolError`:

| Violation | RFC reference |
|---|---|
| CONTINUATION frame with no fragmentation in progress | §5.4 |
| New TEXT or BINARY frame while a fragment sequence is open | §5.4 |
| Control frame (ping/pong/close) with FIN=0 | §5.5 |

## Read-ahead and back-pressure

`BB_WS_QUEUE_DEPTH` selects how far ahead of your handler the
connection reads.  It defaults to `0`.

**`0` — inline (default).**  Frames are read in your handler's own
task, when it calls `receive()`.  There is no background reader task
and no per-message queue, which is what makes a WebSocket message cost
the same event-loop work as an HTTP/1.1 request.  Control frames are
still handled for you — a `ping` is answered and a `close` echoed per
RFC 6455 §5.5 — at the point your handler drives the next read.
RFC 6455 §5.5.2 explicitly permits a delayed `pong`.

**`N > 0` — read-ahead.**  A background task reads *ahead* of your
handler into a queue of depth `N`.  This costs an extra event-loop
round-trip per message and buys two things: control frames are
serviced even while your handler is busy between `receive()` calls,
and up to `N` messages buffer when the client outruns you.  When the
queue fills, the read loop blocks rather than buffering without bound.

Choose read-ahead when a handler does slow work between reads and you
need keepalive `ping`s answered during it.  A handler that loops
tightly on `receive()` — the common shape — wants the default.

!!! note "`websocket_message` forces read-ahead"

    The [`websocket_message`](events.md) event fires when the *server*
    reads a message, not when your handler consumes it, so a handler
    that never calls `receive()` still produces events.  That is only
    possible with a reader running ahead of the handler, so registering
    a `websocket_message` listener switches read-ahead on even at depth
    `0`.  Nothing else observes the difference between the two modes.

## Next

- [Routing](routing.md) — `@app.route` for HTTP routes and the
  rest of the routing surface.
- [Middleware](middleware.md) — the `websocket` middleware and
  other built-ins.
