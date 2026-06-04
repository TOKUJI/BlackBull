# gRPC over BlackBull — assessment

This document is an **evaluation**, not a commitment to ship gRPC
support.  It exists so a future decision about gRPC can start from
a clear picture of what BlackBull already provides, what Sprint 32
just added, and what is still missing.

## What gRPC over HTTP/2 needs

gRPC is a layered protocol that runs on top of HTTP/2.  A minimal
gRPC server has to provide:

1. **HTTP/2 framing.**  Streams, HEADERS / DATA frames, flow control.
2. **Trailers.**  `grpc-status`, `grpc-message`, and any
   `Status-Details-Bin` are sent as HTTP/2 trailing headers, not body.
3. **Bidirectional streaming.**  Both client-streaming (server reads
   N request messages) and server-streaming (server emits N response
   messages) require long-lived `receive()` / `send` loops.
4. **Per-stream cancellation propagation.**  When the peer sends
   `RST_STREAM`, the handler should see it as a cancellation signal,
   not as silent connection closure.
5. **Flow-control awareness.**  Server-streaming RPCs MUST back-pressure
   when the peer's receive window closes — otherwise a slow consumer
   OOMs the server.
6. **Content-type negotiation.**  Routes that handle
   `application/grpc` (or `application/grpc+proto`,
   `application/grpc-web+proto`, etc.) need to dispatch differently
   from regular HTTP routes.
7. **Length-prefixed message framing in the body.**  Each gRPC
   message is `1 byte (compressed flag) + 4 bytes (length) + N bytes
   (payload)`.  Application concern, but the helpers belong somewhere.
8. **`grpc-status` trailer + RST-code mapping.**  Errors map between
   gRPC status codes, HTTP/2 RST_STREAM codes, and the
   `grpc-status`/`grpc-message` trailer headers.

## What BlackBull provides today

| Concern | Status | Notes |
|---|---|---|
| HTTP/2 framing | ✓ | `blackbull.server.http2_actor`, RFC 9113 compliant; conformance harness in `tests/conformance/http2/`. |
| Trailers (`http.response.trailers`) | ✓ | Both HTTP/1.1 (chunked) and HTTP/2 paths.  Implemented in `HTTP1Sender` and `HTTP2Sender`. |
| Bidirectional streaming | ⚠ | `receive()` returns successive `http.request` events as DATA frames arrive; `send()` writes back.  No special bidi support, but the primitives exist. |
| Per-stream cancellation | ⚠ partial | `RST_STREAM` triggers `http.disconnect` on `receive()`.  Adequate signal; not a first-class cancellation primitive. |
| Send-side flow-control visibility | ✓ as of v0.31 | `scope['extensions']['http.response.http2_stream']` snapshots `send_window_remaining` and `connection_send_window_remaining`.  See *What Sprint 32 unlocks* below. |
| `application/grpc` content negotiation | ✗ | Router doesn't dispatch on content-type.  Would need either a routing rule or a middleware that inspects `content-type` and rewrites the route key. |
| Length-prefixed message framing | ✗ | Application concern.  Library code (probably a `blackbull-grpc` extension package) would supply encode/decode helpers around `protobuf` or `grpc-tools` output. |
| `grpc-status` trailer + RST-code mapping | ✗ | Trivial to express on top of trailers — but no helper today. |

Net: BlackBull is closer to a gRPC-capable HTTP/2 server than I
expected before doing this audit.  The framing layer is solid;
what's missing is mostly *gRPC-flavoured glue* on top of primitives
we already have.

## What Sprint 32 unlocks

Sprint 32 added `scope['extensions']['http.response.http2_stream']`
exposing `send_window_remaining` and
`connection_send_window_remaining` (per RFC 9113 §5.2).  This is
the missing primitive a gRPC server-streaming implementation
needs to back-pressure correctly:

```python
async def server_streaming_rpc(scope, receive, send):
    ext = scope['extensions']['http.response.http2_stream']
    for msg in produce_many_messages():
        # If send window is below a threshold, the peer's recv buffer
        # is filling up — defer the next message via cooperative yield
        # or a brief asyncio.sleep instead of blasting it through.
        if ext['send_window_remaining'] < MIN_WINDOW:
            await asyncio.sleep(0)
            # Re-read snapshot: NB - v0.31 ships a snapshot at scope
            # build time; the value above does NOT update mid-request.
            # See "Open question" below.
        await send({'type': 'http.response.body',
                    'body': frame(msg), 'more_body': True})
    await send({'type': 'http.response.trailers',
                'headers': [(b'grpc-status', b'0')]})
```

Without this hint, a gRPC server has no way to know when to slow
down — it would either ignore back-pressure entirely (OOM risk
under slow consumers) or peek at internal HTTP/2 sender state
(layering violation).

## What's still missing for a minimum gRPC server

In rough order of effort:

1. **Live window-snapshot updates** (small, half a sprint).
   Sprint 32 ships a snapshot at scope-build time.  For real
   gRPC server-streaming the application needs the *current*
   value at each iteration of its emit loop.  Either:
   - Re-read the dict and have the populate site re-snapshot
     (requires a hook); or
   - Replace the scalar with a tiny callable
     (`scope['extensions']['http.response.http2_stream']['send_window']()`)
     that reads the sender's current state.

2. **`application/grpc` content-type routing** (small, ~1-2 days).
   A middleware that, when `content-type` starts with
   `application/grpc`, rewrites the route key or sets a scope flag
   the router dispatches on.

3. **gRPC trailer / status helpers** (small, ~1-2 days).
   A `blackbull.grpc.Status` enum + a `grpc_send_status(send,
   code, message)` helper that emits the appropriate trailer.

4. **Length-prefixed message framing helpers** (small, ~1 day).
   `pack_message(payload) → bytes` and an async
   `read_messages(receive) → AsyncIterator[bytes]` that unframes
   the length-prefixed wire format.  Independent of protobuf.

5. **Cancellation as first-class signal** (medium, ~3-5 days).
   Today `http.disconnect` arrives on `receive()`.  gRPC apps
   typically expect an `asyncio.CancelledError`-flavoured signal
   instead.  An adapter that converts disconnect → cancel a
   handler task is reasonable scope.

6. **Bidi streaming ergonomics** (medium, ~3-5 days).
   The primitives are there; a small DSL (`async for req in
   request_iter:`, `await response_stream.send(msg)`) would make
   it much nicer than poking at `receive()` / `send` directly.

7. **Protobuf integration** (large, *out of core scope*).
   Generated code from `protoc` expects specific call shapes
   (`async def MyRpc(request, context): ...`).  An adapter library
   that runs generated stubs against BlackBull handlers belongs in
   a separate package (`blackbull-grpc`) so the framework core stays
   protobuf-free.

## Effort estimate

A **minimal** gRPC unary + server-streaming demo (items 1–4 above)
fits in one ~1-week sprint, on top of Sprint 32's foundation.

A **production-shaped** gRPC implementation (items 1–6 plus the
`blackbull-grpc` package starter) is 2-3 sprints — comparable to
Sprint 31's static-file sendfile work in shape and risk.

## Open question

The biggest design question — and the reason this is an assessment
document, not an implementation — is whether the `send_window`
field should be a **scalar snapshot** or a **live property**.

- Scalar (Sprint 32): simple, ASGI-flat, but useless for
  iterative server-streaming RPCs that need real-time pressure
  signals.
- Live property: a tiny callable on the scope.  More invasive
  (ASGI scopes are conventionally pure data).  But this is the
  shape gRPC genuinely needs.

A future sprint can pick one based on a real adopter's workload.
Until then, the snapshot is *load-bearing for diagnostics* (you
can log it from request_received / before_handler) even if it
doesn't yet enable full back-pressure loops.

## Non-decision

No commitment is being made to ship gRPC.  This document captures
the design state so a future sprint can be scoped accurately
without re-doing the audit.  Promotion criteria: a concrete
adopter need that maps to gRPC and not to plain HTTP/2 streaming.
