# BlackBull — Level A Actor Model Design

**Status**: Draft — Phase 5 / M4  
**Prerequisite completed**: Phase 3 (examples migration), Phase 4 (docs)

---

## Goal

Refactor BlackBull's internal components into an Actor-model architecture (Level A),
where each Actor owns its state and communicates exclusively via message queues.
Level B application-facing events remain unchanged.

---

## Primitives

BlackBull does not depend on an external Actor framework.
Each Actor is implemented as:

~~~python
class Actor:
    def __init__(self):
        self._inbox: asyncio.Queue[Message] = asyncio.Queue()

    async def run(self) -> None:
        """Main loop — consume inbox forever."""
        async for msg in self._inbox_iter():
            await self._handle(msg)

    async def send(self, msg: Message) -> None:
        await self._inbox.put(msg)

    async def _inbox_iter(self):
        while True:
            yield await self._inbox.get()
~~~

- Each Actor runs as an `asyncio.Task` started by its Supervisor.
- Actors never share mutable state — all coordination is via `send()`.
- The ask-pattern (request/reply) is used only when a caller must await a result;
  otherwise fire-and-forget `send()` is preferred.

---

## Actor Hierarchy

~~~
ServerActor  (Supervisor — one per process)
│
└── ConnectionActor  (one per accepted TCP connection)
      │
      ├── HTTP1Actor           (HTTP/1.1 protocol driver)
      │     └── RequestActor   (one per HTTP/1.1 request, short-lived)
      │
      ├── HTTP2Actor           (HTTP/2 connection driver)
      │     └── StreamActor    (one per HTTP/2 stream, short-lived)
      │
      └── WebSocketActor       (spawned after upgrade, replaces HTTP1/2Actor)
~~~

`EventAggregator` is a **non-Actor** component that sits between Level A and
Level B. It subscribes to Level A messages by reference and emits Level B events
via the existing `EventDispatcher`. It has no inbox and no lifecycle of its own.

---

## Actor Responsibilities

### ServerActor
- Owns the listening socket.
- Accepts TCP connections; for each, spawns a `ConnectionActor` task.
- Supervisor strategy: **restart** on failure (bind/accept errors are retried
  with backoff; the server does not crash on one bad connection).
- Emits: `app_startup`, `app_shutdown` (Level B, via `EventAggregator`).

### ConnectionActor
- Owns the `AbstractReader` / `AbstractWriter` for one TCP connection.
- Detects protocol: TLS → HTTP/2 preface check → spawns `HTTP2Actor` or `HTTP1Actor`.
- Supervisor strategy: **isolate** — one connection dying does not affect others.
  `ExceptionGroup` from the child `TaskGroup` is caught here; unhandled exceptions
  are logged and the connection is closed.
- Emits (Level A): `connection_accepted`, `tls_handshake_started`,
  `tls_handshake_completed`.

### HTTP1Actor
- Drives the HTTP/1.1 request/response cycle.
- For each request: spawns a short-lived `RequestActor`, awaits completion,
  then loops for keep-alive.
- On `Connection: Upgrade` (WebSocket): tears itself down and hands the
  reader/writer to a new `WebSocketActor`.
- Supervisor strategy: **isolate** — an error in one request closes the connection.
- Emits (Level A): `http1_request_line_received`, (aggregated into Level B
  `request_received`).

### RequestActor
- Owns one HTTP/1.1 request lifetime.
- Receives the parsed headers and body from `HTTP1Actor`.
- Calls the ASGI app, collects the response, writes it via `HTTP1Actor`'s writer.
- Emits (Level A): used by `EventAggregator` to fire `before_handler`,
  `after_handler`, `request_completed`, `error`.

### HTTP2Actor
- Drives the HTTP/2 connection state machine (settings, flow control, GOAWAY).
- For each `HEADERS` frame with a new stream ID: spawns a `StreamActor`.
- Owns the connection-level send window; `StreamActor`s block via ask-pattern
  when the window is exhausted.
- Supervisor strategy: **propagate** — a framing error on the connection is fatal;
  `HTTP2Actor` sends GOAWAY and exits, causing `ConnectionActor` to close.
- Emits (Level A): `http2_preface_received`, `http2_settings_received`,
  `stream_window_updated`.

### StreamActor
- Owns one HTTP/2 stream.
- Assembles DATA frames, calls the ASGI app, writes response frames back via
  ask-pattern to `HTTP2Actor`.
- Emits (Level A): `stream_headers_received`, `stream_data_received`,
  `stream_priority_updated`, `stream_closed`.
- Supervisor strategy: **isolate** — stream errors send `RST_STREAM` and exit;
  the connection continues.

### WebSocketActor
- Owns one WebSocket connection after the upgrade handshake.
- Runs the `FragmentAssembler` internally; the ASGI app sees only complete messages.
- Emits (Level A): `websocket_frame_received`, `websocket_fragment_appended`.
- Supervisor strategy: **isolate** — a protocol error closes this connection only.

---

## Level A → Level B Aggregation

`EventAggregator` translates Level A messages into Level B `EventDispatcher` calls.
It is instantiated once and passed (by reference) to each Actor at construction.

| Level A message(s) | Level B event |
|---|---|
| `connection_accepted` + app routing complete | `request_received` |
| `RequestActor` pre-dispatch | `before_handler` |
| `RequestActor` post-dispatch | `after_handler` |
| `stream_closed` (HTTP/2) / response sent (HTTP/1.1) | `request_completed` |
| Reader EOF / `RST_STREAM` before response | `request_disconnected` |
| Any Actor unhandled exception | `error` |
| `WebSocketActor` handshake complete | `websocket_connected` |
| `websocket_frame_received` (complete message) | `websocket_message` |
| `WebSocketActor` exit | `websocket_disconnected` |
| `ServerActor` startup | `app_startup` |
| `ServerActor` shutdown drain | `app_shutdown` |

The aggregation logic lives in `blackbull/event_aggregator.py` (new file).
It must not be exported from the package's public `__init__.py`.

---

## Supervisor Strategy Summary

| Actor | Strategy | Rationale |
|---|---|---|
| `ServerActor` | Restart with backoff | Accept loop must stay alive |
| `ConnectionActor` | Isolate | One bad connection must not affect others |
| `HTTP1Actor` | Isolate | Error closes the connection cleanly |
| `RequestActor` | Isolate | Error fires `error` event; connection survives |
| `HTTP2Actor` | Propagate to `ConnectionActor` | Framing error is connection-fatal |
| `StreamActor` | Isolate (RST_STREAM) | Stream error is stream-fatal only |
| `WebSocketActor` | Isolate | Protocol error closes this WS connection only |

---

## Message Protocol

All messages are `dataclass` instances (not dicts) for type safety.
A base class provides the actor-source reference for reply routing:

~~~python
@dataclass
class Message:
    sender: Actor | None = None  # None for externally-originated messages

# Example Level A messages
@dataclass
class ConnectionAccepted(Message):
    reader: AbstractReader
    writer: AbstractWriter
    peername: tuple[str, int]

@dataclass
class StreamHeadersReceived(Message):
    stream_id: int
    headers: HeaderList
    end_stream: bool

@dataclass
class WindowUpdate(Message):       # ask-pattern reply
    stream_id: int
    increment: int
~~~

---

## Exception Propagation Rules (per-event decisions)

| Scenario | Strategy | Reason |
|---|---|---|
| `RequestActor` handler raises | (b) isolate + (c) re-emit as `error` | Handler error must not kill the connection |
| `HTTP2Actor` framing error | (a) propagate to `ConnectionActor` | GOAWAY required; connection is unusable |
| `StreamActor` app error | (b) isolate + RST_STREAM | Other streams continue |
| `WebSocketActor` protocol error | (b) isolate + CLOSE frame | Single connection |
| Observer handler raises | (b) isolate | Existing Level B contract |
| Intercept handler raises | (a) propagate to emitter | Existing Level B contract |

---

## Migration Path

Phase 5 is **design and specification only**. No production code changes in this phase.

Suggested implementation order (Phase 6+):

1. Define `Message` dataclasses and the `Actor` base class (`blackbull/actor.py`).
2. Implement `EventAggregator` with stubs that forward to existing `EventDispatcher`.
3. Extract `HTTP1Actor` from the current `HTTP1Handler` — keep existing tests green.
4. Extract `StreamActor` + `HTTP2Actor` from `HTTP2Handler`.
5. Introduce `ConnectionActor` wrapping the above two.
6. Introduce `ServerActor` wrapping `ASGIServer`.
7. Delete the old handler classes once all tests pass.

Each step keeps the full test suite green before proceeding to the next.

---

## Resolved Questions

1. **Ask-pattern timeout**: use the connection-level idle timeout already
   configured on `ASGIServer`. No separate timeout value introduced.

2. **`EventAggregator` threading**: currently always called from the event loop
   thread (single-event-loop design). Do not add a thread-safety layer now;
   revisit if multi-loop or thread-executor support is added later.

3. **Server push**: yes — pushed streams (even stream IDs) become `StreamActor`s
   with the same lifecycle as request-driven streams.

4. **`ExceptionGroup` surface**: emit a **single** Level B `error` event with the
   `ExceptionGroup` placed in `detail['exception']`. `ExceptionGroup` is a
   `BaseException` subclass, so the event contract is unchanged. Subscribers that
   need the individual failures iterate `eg.exceptions`; subscribers that do not
   care are unaffected. Note: `ExceptionGroup` and `except*` require Python 3.11+;
   `pyproject.toml` should reflect this floor explicitly.
