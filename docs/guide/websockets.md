# WebSockets

BlackBull serves WebSocket connections over HTTP/1.1 `Upgrade`
(RFC 6455) by default, and over HTTP/2 Extended CONNECT (RFC 8441)
as an opt-in.  `permessage-deflate` (RFC 7692) compression is
negotiated automatically.

## Registering a route

WebSocket routes use `scheme=Scheme.websocket`.  The handler
always receives the full `(scope, receive, send)` triplet —
the simplified handler form does **not** apply:

```python
from blackbull import BlackBull
from blackbull.utils import Scheme

app = BlackBull()

@app.route(path='/ws', scheme=Scheme.websocket)
async def ws_handler(scope, receive, send):
    await receive()                          # consume 'websocket.connect'
    await send({'type': 'websocket.accept'})
    while True:
        event = await receive()
        if event['type'] == 'websocket.disconnect':
            break
        text = event.get('text') or event.get('bytes', b'').decode()
        await send({'type': 'websocket.send', 'text': text})
```

`Sec-WebSocket-Version: 13` is validated automatically.

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

## Queue depth and back-pressure

Each WebSocket connection has an inbound event queue; the depth
defaults to 256 and is configurable via `BB_WS_QUEUE_DEPTH`.
When the queue fills (your handler is slower than the client),
new frames block the per-connection read loop rather than
unbounded buffering in memory.

## Next

- [Routing](routing.md) — `@app.route` for HTTP routes and the
  rest of the routing surface.
- [Middleware](middleware.md) — the `websocket` middleware and
  other built-ins.
