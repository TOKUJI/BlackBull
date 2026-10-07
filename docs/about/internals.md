# Internals

This page defines ownership boundaries for contributors. Use the
[Guide](../guide/index.md) for application APIs.

## Actor model

Each mutable state machine has one owner. HTTP/1.1 reuses request work
sequentially on a connection; HTTP/2 requires separate stream work because
requests overlap. MQTT coordinates routing and output with serial inboxes.
Do not mutate another connection's or stream's state through a backchannel.

Read `ConnectionActor`, `HTTP1Actor`, `RequestActor`, `HTTP2Actor` and
`StreamActor` in `blackbull/server/`. The HTTP actors drive protocol loops;
their inherited inbox does not imply that dispatch goes through it.

### gRPC: no dedicated actor

gRPC reuses HTTP/2 stream work and the receive/send boundary. Keep call
semantics in `blackbull.grpc`; see [gRPC design](grpc-assessment.md).

## Events and failures

Connection events belong to `ConnectionActor`. Request-lifecycle events
belong to the application layer and fire exactly once on native and external
ASGI transports. `request_completed` follows the global middleware chain,
so buffering middleware has finished sending before observers run.

Isolate application and stream failures from sibling streams. Connection
framing or HPACK failures terminate the connection; peer GOAWAY permits
accepted response work to drain. Read `blackbull/event_aggregator.py` and
`app.py`; public contracts are in [Events](../guide/events.md).

## Read-path invariant

The native server carries typed `Connection` objects end to end. `scope`
means an ASGI dictionary for an external host or `BB_FORCE_ASGI_SCOPE=1`.
Create it after pre-dispatch mutations; shared `state` and `extensions`
remain live for late updates.

A connection owns one read buffer. `BufferReader` owns receive policy,
`ConnectionProtocol` performs transport callbacks and pauses, and
`ReadBuffer` owns bytes and cursors. Detection must not consume the prefix;
an upgrade must retain bytes already received.

Treat transport size hints as advisory. Release grown allocations only
through reader policy, which knows whether the connection is between
messages. Read `blackbull/server/connection_protocol.py`, `read_buffer.py`
and `blackbull/protocol/rsock.py` before adding a reader adapter.

### Bounded reads

All readers owe the same `read_head` and bounded `readuntil` contract.
Positive limits include separators and constrain accumulation, not only
completed results. Every keep-alive request gets the same budget. Empty EOF
returns an empty head; partial EOF raises `IncompleteReadError`; overruns
carry bounded evidence in `ReadLimitExceeded`. Unsupported line readers
must refuse consistently.

The protocol owns the verdict: a request-line terminator within the budget
permits a 431 header-overrun response; an unterminated or oversized request
line receives 400. Preserve evidence across reader adapters.
See `tests/unit/test_read_head_contract.py`.

### Backpressure

Pause when a consumer falls behind, but release the pause before waiting for
more bytes. A reader waiting for a peer-selected large frame must not
deadlock against its own high-water mark. Slice HTTP bodies before reading;
a declared chunk size must not select an unbounded allocation.

### Rejecting requires lingering

Closing with unread socket bytes can reset the connection and discard a
written refusal. `ConnectionProtocol.linger_close` discards remaining input
within byte and time bounds. Keep both: one bounds work, the other prevents
a refused peer from retaining the connection. Delivery stays best-effort
when the peer does not read.

### Accept admission

Count descriptors from `accept()` to close on every supported loop,
including TLS handshakes and refusals. `connection_made` misses handshakes.
Pause acceptance at the cap plus refusal reserve; do not consume the
process's descriptor budget to manufacture refusal replies.
Read `_AcceptGate` in `blackbull/server/server.py`.

## Receive-path invariant

Native body readers return bytes, `None` for completion, or raise
`ClientDisconnected`. Empty bytes are not the completion sentinel.
Build ASGI events only at the ASGI boundary. Both channels share completion
state so switching between them cannot await input that will never arrive.
Read `blackbull/server/recipient.py` and `blackbull/connection.py`.

## Keep-alive drain invariant

Before reusing an HTTP/1.1 connection, consume unread request content within
the drain budget or close. Every answer path, including one bypassing
routing, reaches this boundary. Never drain after a framing violation:
the next bytes cannot be distinguished from a smuggled request.

A WebSocket upgrade leaves the request loop and cannot rely on its drain.
Refuse handshakes declaring content before switching; `Content-Length: 0`
may still upgrade. Read `blackbull/server/http1_actor.py`.

## Send-path invariant

Protocol senders pass fragments to `BaseSender._write_many(parts)`; only
that method chooses joining or vectored writes at the 32 KiB gate.
Adapters must support both paths: testing only small responses misses the
vectored backing contract.
See `tests/architecture/test_writer_backing_contract.py`.

Normalize ASGI events through `blackbull.native`, not separate handler and
sender conversions. Middleware copies mutable field lists; direct sender
conversion can borrow only until the sender snapshots them. Validate and
lowercase response heads and trailers once on entry. Push headers describe
a promised request and bypass response header injection and middleware.

Bound socket drains and flow-control waits. File transfers use bounded
chunks under the write deadline rather than a whole-file timeout.
Read `blackbull/server/sender.py` and `blackbull/native.py`.

## Parse-path invariant

Use bulk byte operations and `blackbull.protocol.field_grammar`.
Fast paths may reuse validated work but must not weaken checks or change
refusal diagnostics. A failed pre-scan falls back to detailed validation.

Header-line cache keys are exact bytes; admit only fully validated lines.
Learned entries remain per connection and bounded by count, line size and
accounted bytes. Shared seeds must be immutable, validated by the real
grammar, drawn from specification-enumerated values, and exclude framing
fields. Do not turn observations from one peer into shared state.
Read `blackbull/server/parser.py` and its cache tests.

## Protocol and deadline owners

| Concern | Read first |
|---|---|
| HTTP/2 framing and retirement | `blackbull/server/http2_actor.py`, `blackbull/protocol/frame.py` |
| WebSocket framing and complete-message delivery | `blackbull/server/ws_codec.py`, `websocket_actor.py`, `permessage_deflate.py` |
| Shared timer scanner and cancellation | `blackbull/server/deadline.py` |
| HTTP/2 liveness, field-block and PING deadlines | `blackbull/server/http2_actor.py` |
| MQTT routing and sole-writer inboxes | [MQTT broker design](mqtt-actor-design.md) |

Connection-owned credit work survives stream-consumer cancellation;
stream-owned credit must not be emitted after retirement. Request END_STREAM
ends only input, not application or response work. HPACK belongs to one
factory per connection. See [HTTP/2 policies](rfc9113-implementation.md)
and [Conformance](conformance.md) before changing error scope or lifetime.
