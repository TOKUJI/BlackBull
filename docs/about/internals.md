# Internals

For readers who want to know what BlackBull is doing under the
hood.  Application authors don't need any of this — `@app.route`
and the [Guide](../guide/routing.md) cover the user surface.
This page exists for contributors and for anyone curious about
how the protocol stack is built.

## Actor model

BlackBull's server core is organized as a small hierarchy of
actors — each one owns its state and communicates with the
others through queues, not shared variables.  No actor reaches
into another actor's internals; coordination is exclusively via
messages.

This isn't a third-party actor framework — every actor is a
plain `asyncio.Task` reading from an `asyncio.Queue`:

```python
class Actor:
    def __init__(self):
        self._inbox: asyncio.Queue[Message] = asyncio.Queue()

    async def run(self):
        async for msg in self._inbox_iter():
            await self._handle(msg)

    async def send(self, msg):
        await self._inbox.put(msg)
```

The benefit isn't sophistication — it's the discipline.  An
actor that can only communicate by sending messages is much
easier to reason about under burst load than one with arbitrary
back-channels into other components.

## Hierarchy

```
ASGIServer                        (one per process; owns the listening socket)
│
└── ConnectionActor               (one per accepted TCP connection)
      │
      ├── HTTP1Actor              (HTTP/1.1 driver)
      │     └── RequestActor      (one per HTTP/1.1 request, short-lived)
      │
      ├── HTTP2Actor              (HTTP/2 connection driver)
      │     └── StreamActor       (one per HTTP/2 stream)
      │           └── RequestActor   (per-stream ASGI call, short-lived)
      │
      ├── WebSocketActor          (after upgrade, replaces HTTP1/2Actor)
      │
      └── raw protocol handler    (non-HTTP bridge: the binding calls a
                                   long-lived (reader, writer, ctx) handler
                                   directly — no separate actor layer)
```

`ASGIServer` is the only non-actor in this hierarchy — it's a
plain `asyncio` server that owns the listening socket and
spawns a `ConnectionActor` task per accepted connection.
Everything below it is an `Actor` subclass.

A separate `EventAggregator` translates the low-level
inter-actor messages into the wire-level user-facing events
documented in [Events](../guide/events.md) —
`request_disconnected`, `error`, `websocket_message`,
`connection_accepted`, etc.  The request-lifecycle events
(`request_received`, `before_handler`, `after_handler`,
`request_completed`) are emitted by the application layer
(`BlackBull._dispatch`) instead, so they fire identically under
BlackBull's own server and under external ASGI hosts.

## Responsibilities

### `ASGIServer`

- Owns the listening socket.
- Accepts TCP connections; spawns a `ConnectionActor` task per
  connection.
- Supervisor strategy: **restart with backoff** — accept loop
  must stay alive even if individual binds fail.
- The `app_startup` and `app_shutdown` events are surfaced
  separately, by `BlackBull` itself as part of the ASGI
  lifespan handshake — not by the server (see
  [Events](../guide/events.md)).

### `ConnectionActor`

- Owns the reader / writer for one TCP connection, and the
  connection-lifecycle events: it fires `connection_accepted` on
  entry and `connection_closed` (with duration) on exit for
  **every** protocol — HTTP and non-HTTP alike.
- Detects the protocol *without any HTTP-specific wire knowledge*:
  it peeks a tiny protocol-agnostic discriminator prefix, asks each
  registered `ProtocolBinding` whether it `claims` it (ALPN first,
  then the cleartext chain), and replays the peeked bytes to the
  winning binding's `serve`.  Each binding does its own framing reads
  (the HTTP/2 preface, the HTTP/1.1 request line) — `ConnectionActor`
  holds no hardcoded byte counts, delimiters, or status strings.
- Supervisor strategy: **isolate** — one connection dying does
  not affect others.  A handler/actor exception from any protocol is
  caught here, emitted once as an `error` event, and the connection
  is closed.

### `HTTP1Actor`

- Drives the HTTP/1.1 request/response cycle.
- For each request: spawns a `RequestActor`, awaits its
  completion, then loops for keep-alive.
- On `Connection: Upgrade` (WebSocket): hands the reader /
  writer to a new `WebSocketActor` and exits.
- Supervisor strategy: **isolate** — an error in one request
  closes the connection only.

### `RequestActor`

- Owns one HTTP/1.1 request lifetime.
- Receives the parsed headers and body from `HTTP1Actor`.
- Calls the ASGI app, collects the response, writes it back
  via the connection's writer.
- Fires the `error` event when the app call raises.  (The
  request-lifecycle events are emitted by the application
  layer, not here.)

### `HTTP2Actor`

- Drives the HTTP/2 connection state machine: settings, flow
  control, GOAWAY.
- For each `HEADERS` frame with a new stream ID, spawns a
  `StreamActor`.
- Owns the connection-level send window; `StreamActor`s block
  on it when the window is exhausted.
- Supervisor strategy: **propagate** — a framing error on the
  connection is fatal.  `HTTP2Actor` sends GOAWAY and exits,
  causing `ConnectionActor` to close.

### `StreamActor`

- Owns one HTTP/2 stream.
- Assembles DATA frames into a request body, calls the ASGI
  app, writes response frames back.
- Supervisor strategy: **isolate** — stream errors send
  `RST_STREAM` and exit; the connection continues serving
  other streams.

### `WebSocketActor`

- Owns one WebSocket connection after the upgrade handshake.
- Runs the fragment-assembler internally — the ASGI app
  receives only complete messages (see
  [WebSockets — Fragmented messages](../guide/websockets.md#fragmented-messages)).
- Supervisor strategy: **isolate** — a protocol error closes
  this connection only.

### Non-HTTP protocol handlers (the Non-ASGI bridge)

- A non-HTTP protocol registers a `ProtocolBinding` whose `serve`
  calls a long-lived `(reader, writer, ctx)` handler directly —
  there is **no separate actor layer** between `ConnectionActor`
  and the handler.  `ConnectionActor` reaches it the same way it
  reaches HTTP: by a binding (port-bound, or matched by a first-byte
  sniff) for protocols that don't map to ASGI request/response:
  MQTT, raw TCP, and the like.
- Unlike the HTTP actors the handler does not run a request loop —
  it owns the connection until it decides to close (raw protocols
  are typically stateful and persistent).

### gRPC: no dedicated actor

Unary gRPC has **no Actor of its own**.  It is a dialect of HTTP/2, so it
rides the existing `HTTP2Actor` → `StreamActor` stack: each gRPC call is
just an HTTP/2 stream and therefore already gets per-stream actor
isolation for free.  The gRPC-specific logic lives at the **application**
layer — `BlackBull._dispatch` routes `content-type: application/grpc` to
`blackbull.grpc.serve_grpc`, which uses the ordinary ASGI
`(scope, receive, send)` interface (the `http.response.trailers` event
carries `grpc-status`).  `ConnectionActor` and `HTTP2Actor` stay entirely
gRPC-agnostic.  The full rationale — and where streaming RPCs may
reopen the question — is in the
[gRPC assessment](grpc-assessment.md#design-decision-grpc-rides-the-asgi-bridge-not-a-new-actor).
- Connection timing, error isolation, and the `connection_closed`
  event are provided uniformly by `ConnectionActor.run()` for every
  protocol (an earlier `RawProtocolActor` Layer-2 wrapper was folded
  into `ConnectionActor` so HTTP and non-HTTP share one lifecycle
  owner).
- The handler is where a protocol's own actors live.  MQTT, for
  example, runs a lifespan-owned `BrokerActor` and `TapActor`
  plus a per-connection `MQTT5Actor` beneath it — and
  these are the framework's **first genuine users of the `Actor`
  inbox** (the HTTP actors override `run()` and call each other
  directly).  See
  [MQTT broker design](mqtt-actor-design.md) for the deep dive
  and [Non-ASGI protocols](../guide/raw-protocols.md) for the
  user-facing API.

## Supervisor strategies — at a glance

| Actor | Strategy | Rationale |
|---|---|---|
| `ASGIServer` | Restart with backoff | Accept loop must stay alive |
| `ConnectionActor` | Isolate | One bad connection must not affect others |
| `HTTP1Actor` | Isolate | Error closes the connection cleanly |
| `RequestActor` | Isolate | Error fires `error` event; connection survives |
| `HTTP2Actor` | Propagate to `ConnectionActor` | Framing error is connection-fatal (GOAWAY) |
| `StreamActor` | Isolate (`RST_STREAM`) | Stream error is stream-fatal only |
| `WebSocketActor` | Isolate | Protocol error closes this WS connection only |
| raw protocol handler | Isolate (via `ConnectionActor`) | One raw connection's handler failure is bounded |

## Message types

All inter-actor messages are typed `dataclass` instances, not
dicts.  A base class carries a `sender` reference for reply
routing under the ask pattern:

```python
@dataclass
class Message:
    sender: Actor | None = None      # None for externally-originated

@dataclass
class StreamHeadersReceived(Message):
    stream_id: int
    headers: HeaderList
    end_stream: bool

@dataclass
class WindowUpdate(Message):         # ask-pattern reply
    stream_id: int
    increment: int
```

HTTP/2 message types live in `blackbull/server/http2_messages.py`;
HTTP/1.1 and WebSocket equivalents are colocated with their
respective actors.

## How exceptions propagate

| Scenario | Strategy | Reason |
|---|---|---|
| `RequestActor` handler raises | Isolate + re-emit as `error` event | Handler error must not kill the connection |
| `HTTP2Actor` framing error | Propagate to `ConnectionActor` | GOAWAY required; connection is unusable |
| `StreamActor` flow-control violation | Isolate (`RST_STREAM`) | Stream-fatal only; other streams keep going |
| raw protocol handler raises | Caught by `ConnectionActor`, re-emit as `error` event | Bridge handler error must not kill other connections |
| `ConnectionActor` unhandled | Log + close connection | One connection's failure is bounded |
| `ASGIServer` accept error | Backoff + retry | Server stays alive across transient errors |

## The pieces around the actor core

The actor hierarchy is the *control* side.  The *data* side is
the protocol stack:

- **HTTP/1.1 parser** — `blackbull/server/parser.py`.  Pure
  Python.
- **HTTP/2 frame layer** — `blackbull/protocol/` (frame types,
  flow-control windows, RFC 9218 `PRIORITY_UPDATE`).  Header
  compression delegates to the `hpack` library — the only
  third-party Python package in the protocol stack — wrapped by
  the BlackBull-owned `hpack_fastpath.py` for the common
  short-header path.
- **WebSocket codec** — `blackbull/server/ws_codec.py`
  (RFC 6455 framing) + `blackbull/server/websocket_actor.py`
  (fragment reassembly, RFC 7692 `permessage-deflate`).
- **Deadline subsystem** — per-process tick scanner tracking
  every connection's idle timer; arming and disarming a
  deadline are attribute writes + set operations rather than
  per-arm `loop.call_later` calls.
- **Sender / Recipient** — `blackbull/server/sender.py`,
  `blackbull/server/recipient.py`.  Buffer responses on the way
  out, parse incoming frames on the way in.  Cache headers
  (Date, common content-types) for hot-path savings.

For the wire-level behaviour of each layer, the
[RFC conformance suites](conformance.md) are the up-to-date
source of truth.

## Why pure-Python

`blackbull[speed]` adds `uvloop` as an optional dependency.
The HTTP/1.1 parser, HTTP/2 frame layer, and WebSocket codec
are all BlackBull's own code in pure Python.  The one exception
is HPACK header compression,
which delegates to the [`hpack`](https://pypi.org/project/hpack/)
library (pure Python, layered under the BlackBull-owned
`hpack_fastpath.py`); re-implementing a conformant HPACK
encoder/decoder is a sub-project of its own, and `hpack` is the
de-facto Python reference.  Two reasons we keep the rest in
pure Python:

- **Debuggability.**  An issue in HTTP/2 flow control or
  frame parsing can be stepped into with `pdb`.  The stack is
  the application's code, not an opaque C extension.  This
  applies to `hpack` too — it is itself pure Python, so HPACK
  bugs remain debuggable in-process.
- **Identity.**  BlackBull exists in part to demonstrate that
  CPython is fast enough for a pure-Python ASGI implementation
  when the framework itself stays out of the way.  Swapping in
  a C parser would make it a different project.

That said: stdlib C extensions (`_json`, `_hashlib`, `_ssl`)
are used freely — they ship with CPython and don't require a
separate build step.  "Pure Python" means **no third-party C
extensions in the protocol stack**, not "no C anywhere."

## Next

- [Conformance](conformance.md) — RFC test suites that exercise
  the stack end-to-end.
- [Events](../guide/events.md) — the user-facing event API,
  produced by `EventAggregator` from the low-level messages
  shown above.
- [HTTP/2](../guide/http2.md) — the user surface of the HTTP/2
  protocol features whose internals are summarized here.
