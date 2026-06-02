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
ServerActor                       (one per process)
│
└── ConnectionActor               (one per accepted TCP connection)
      │
      ├── HTTP1Actor              (HTTP/1.1 driver)
      │     └── RequestActor      (one per HTTP/1.1 request, short-lived)
      │
      ├── HTTP2Actor              (HTTP/2 connection driver)
      │     └── StreamActor       (one per HTTP/2 stream, short-lived)
      │
      └── WebSocketActor          (after upgrade, replaces HTTP1/2Actor)
```

A separate `EventAggregator` translates the low-level
inter-actor messages into the user-facing events documented in
[Events](../guide/events.md) — `request_received`,
`before_handler`, `request_completed`, `websocket_message`,
etc.

## Responsibilities

### `ServerActor`

- Owns the listening socket.
- Accepts TCP connections; spawns a `ConnectionActor` task per
  connection.
- Supervisor strategy: **restart with backoff** — accept loop
  must stay alive even if individual binds fail.
- Surfaces `app_startup` and `app_shutdown` (see
  [Events](../guide/events.md)).

### `ConnectionActor`

- Owns the reader / writer for one TCP connection.
- Detects the protocol: TLS handshake → check ALPN → HTTP/2
  preface check → spawn `HTTP2Actor` or `HTTP1Actor`.
- Supervisor strategy: **isolate** — one connection dying does
  not affect others.  Unhandled exceptions are logged, and the
  connection is closed.

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
- Drives the `before_handler`, `after_handler`,
  `request_completed`, and (on failure) error events.

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

## Supervisor strategies — at a glance

| Actor | Strategy | Rationale |
|---|---|---|
| `ServerActor` | Restart with backoff | Accept loop must stay alive |
| `ConnectionActor` | Isolate | One bad connection must not affect others |
| `HTTP1Actor` | Isolate | Error closes the connection cleanly |
| `RequestActor` | Isolate | Error fires `error` event; connection survives |
| `HTTP2Actor` | Propagate to `ConnectionActor` | Framing error is connection-fatal (GOAWAY) |
| `StreamActor` | Isolate (`RST_STREAM`) | Stream error is stream-fatal only |
| `WebSocketActor` | Isolate | Protocol error closes this WS connection only |

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
| `ConnectionActor` unhandled | Log + close connection | One connection's failure is bounded |
| `ServerActor` accept error | Backoff + retry | Server stays alive across transient errors |

## The pieces around the actor core

The actor hierarchy is the *control* side.  The *data* side is
the protocol stack:

- **HTTP/1.1 parser** — `blackbull/server/parser.py`.  Pure
  Python; no `httptools` dependency.
- **HTTP/2 frame layer** — `blackbull/protocol/` (frame types,
  flow-control windows, HPACK encoder/decoder + fastpath, RFC
  9218 `PRIORITY_UPDATE`).
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
The protocol stack itself stays pure Python — no `httptools`,
no `h2`-as-a-replacement.  Two reasons:

- **Debuggability.**  An issue in HPACK encoding or HTTP/2
  flow control can be stepped into with `pdb`.  The stack is
  the application's code, not an opaque C extension.
- **Identity.**  BlackBull exists in part to demonstrate that
  CPython is fast enough for a from-scratch ASGI implementation
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
