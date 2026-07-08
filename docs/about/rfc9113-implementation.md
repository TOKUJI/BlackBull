# RFC 9113, implemented

A section-by-section reading of [RFC 9113](https://www.rfc-editor.org/rfc/rfc9113)
(HTTP/2) against the Python that implements it in BlackBull.

**Who this is for**: you already know RFC 9113 and want to see how a
from-scratch, pure-Python server implements each requirement — and *why* it
made the choices it did.  Each entry below reads *the RFC says X → BlackBull
does Y → because Z*, keyed by the section number you already hold in your head.

**The one thing to know up front**: most Python HTTP/2 servers are built on
the [`h2` library](https://python-hyper.org/projects/h2/en/stable/), a
*sans-I/O state machine* — you feed it bytes, it returns events, the frame
layer is invisible.  BlackBull is not built that way.  It is an **actor** that
owns the socket and drives the frame loop itself, so every requirement below
maps to a method you can open and step through with `pdb`.  The actor
architecture itself is described in [Internals](internals.md); you do not need
it to read this page — it surfaces here only where the RFC's behaviour depends
on it (notably §5.4 error handling).

### Legend

| Mark | Meaning |
|---|---|
| ✅ | Implemented by BlackBull.  Most of it lives in [`http2_actor.py`](https://github.com/TOKUJI/BlackBull/blob/master/blackbull/server/http2_actor.py); where a requirement is met by another component (`ConnectionActor`, the TLS layer) or a dependency (`hpack`), the text says so. |
| ✗ | Not implemented — the reason is given inline, and every such item is optional (see the [coverage summary](#coverage-summary)). |

**Code-reference convention** (used throughout): a **method** is written
`Class.method()` — e.g. `HTTP2Actor.receive()`; a **module-level function** is
written `function() in file.py` — e.g. `parse_headers() in parser.py`; a
**class member** (constant or instance attribute) is written `Class.NAME` — e.g.
`HTTP2Actor._STREAM_ONLY_FRAME_TYPES`, `HTTP2Actor._connection_window_size` — so
the reader always knows which actor owns the state.  A **method-local variable**
is named with its method, e.g. "the `_frame_loop()`-local `waiting_continuation`".
Behaviour is described in terms of the
**method** that does the work, with any constant it consults named alongside —
so every reference is something you can locate, not a bare name floating free.

A [coverage tally](#coverage-summary) at the foot lets you confirm you have
seen every section of RFC 9113, not a curated subset.

---

## §3 — Starting HTTP/2

**§3.1 Version Identification / §3.2 "https" URIs** ✅
Over TLS, HTTP/2 is selected by ALPN `h2`, negotiated by `ConnectionActor`
before `HTTP2Actor` exists; the actor only ever runs once the connection is
known to be HTTP/2.  *Because* protocol detection is a connection-layer concern —
the HTTP/2 driver should not have to re-derive which protocol it is.

**§3.3 Prior Knowledge (h2c)** ✅
Cleartext HTTP/2 *is* supported via prior knowledge.  On a connection that did
**not** negotiate ALPN `h2`, `ConnectionActor._dispatch()` sniffs the first
line; if it is `PRI * HTTP/2.0\r\n` it validates the full 24-byte preface and
spawns `HTTP2Actor` over the plaintext socket.  This shares the HTTP/1.1 port —
there is no separate h2c-only port — which RFC 9113 §3.3 permits.  *Because*
prior knowledge needs no Upgrade dance: a client committed to h2c simply opens
with the preface, and the same listener can serve both protocols.  (The
deliberate port-sharing is noted in
[`KNOWN_LIMITATIONS.md`](https://github.com/TOKUJI/BlackBull/blob/master/KNOWN_LIMITATIONS.md)
as an operational caveat, not a missing feature.)

> The *Upgrade*-based h2c bootstrap (the `Upgrade: h2c` / HTTP/1.1-101
> dance, originally RFC 7540 §3.2, deprecated by RFC 9113 §3.1) is **not**
> implemented — only prior-knowledge h2c is.  RFC 9113 removed the Upgrade
> mechanism that RFC 7540 defined, so this is the forward-looking shape.

**§3.4 Connection Preface** ✅
After the 24-byte client preface, the server **MUST** send a SETTINGS frame as
the very first frame.  `HTTP2Actor.run()` does exactly this on entry, and — when
configured to grow the connection window — follows it with a connection-level
WINDOW_UPDATE.  *Because* the preface is non-negotiable: a client will not
proceed until it has seen the server's opening SETTINGS.

---

## §4 — HTTP Frames

**§4.1 Frame Format** ✅
Every frame is a 9-byte header — length(24) + type(8) + flags(8) +
R(1)+stream_id(31) — followed by the payload.  `HTTP2Actor.receive()` reads
exactly 9 bytes, extracts the length, then reads exactly that many more.
*Because* `readexactly` over a known length is the whole of framing — no
buffering heuristics, no partial-frame state to carry.

**§4.2 Frame Size** ✅
Frames larger than `SETTINGS_MAX_FRAME_SIZE` are an error.
`HTTP2Actor._frame_loop()` decides whether it is a **connection** error or a
**stream** error by testing the frame type against the class-level frozenset
`HTTP2Actor._FRAME_SIZE_CONNECTION_ERROR_TYPES` — header-block and
connection-state frames (HEADERS, CONTINUATION, PUSH_PROMISE, SETTINGS) are
connection-fatal, everything else is stream-fatal.  *Because* an oversized
HEADERS frame corrupts the shared HPACK decoder state, so the whole connection
must die — but an oversized DATA frame only dooms its own stream.

**§4.3 Field Section Compression (HPACK)** ✅
Header bytes are accumulated into `frame.raw_block` and decoded by the
[`hpack`](https://pypi.org/project/hpack/) library (the only third-party
package in the protocol stack), wrapped by `hpack_fastpath.py` for the common
short-header case.  *Because* HPACK is stateful at the connection level (a
shared dynamic table); re-implementing a conformant codec is a sub-project of
its own, and `hpack` is the de-facto Python reference — itself pure Python, so
it stays `pdb`-debuggable.

---

## §5 — Streams and Multiplexing

**§5.1 Stream States** ✅
Four states matter for a server in practice:
IDLE → OPEN → HALF_CLOSED_REMOTE → CLOSED.
(The two "reserved" states exist only during server push and are not shown —
the server sends PUSH_PROMISE but never receives one, so it only ever sees
IDLE, OPEN, HALF_CLOSED_REMOTE, and CLOSED.)
`HTTP2Actor._validate_stream_state(stream, frame_type)` is the gate: it returns
`(error_code, level)` for an illegal frame in the current state, or `None` to
allow it.

| State | Legal frames | Violation |
|---|---|---|
| IDLE | HEADERS, PRIORITY, CONTINUATION, PUSH_PROMISE | connection PROTOCOL_ERROR |
| HALF_CLOSED_REMOTE | PRIORITY, WINDOW_UPDATE, RST_STREAM | stream STREAM_CLOSED |
| CLOSED | PRIORITY always; HEADERS/CONTINUATION → connection error; else → stream RST |

CONTINUATION on IDLE is listed because `_validate_stream_state` permits it,
but `_frame_loop()` catches a stray CONTINUATION — one not preceded by
HEADERS without END_HEADERS — as a connection PROTOCOL_ERROR earlier in the
loop.  The state check is the second line of defence.

*Because* the state table is the heart of multiplexing — getting it wrong means
either rejecting valid concurrent streams or leaking resources on dead ones.
A subtlety: closed streams are not kept as full `Stream` objects.  When a task
finishes, `HTTP2Actor._make_done_cb()` prunes the node and records just
`HTTP2Actor._closed_streams[stream_id] = closed_via_rst`.  *Because* a late frame on a
closed stream still needs the CLOSED branch of validation, but an integer
lookup is enough — you should not pay a `Stream` object per completed request.

**§5.1.1 Stream Identifiers** ✅
Peer-initiated streams **MUST** use odd identifiers, strictly increasing.
Checked in `HTTP2Actor._frame_loop()`: an even id from the peer, or one `≤
HTTP2Actor._last_peer_stream_id`, is a connection PROTOCOL_ERROR.  Server push uses even
ids from `HTTP2Actor._allocate_push_stream_id()`.  *Because* the monotonic-odd
rule is what lets both ends allocate ids without a round-trip; a violation means
the peer's state machine has diverged from ours and the connection is no longer
trustworthy.

**§5.1.2 Stream Concurrency** ✅
RFC 9113 lets a server *choose* how many streams may run at once, and the server
publishes its choice as `SETTINGS_MAX_CONCURRENT_STREAMS`.  When a new HEADERS
frame would push past that limit, `HTTP2Actor._on_headers_frame()` answers it
with **RST_STREAM REFUSED_STREAM** — a stream-level error, not a connection
error.  *Because* the client did nothing wrong: it just bumped into a ceiling
the server set for itself.  REFUSED_STREAM says exactly that — "I never started
this one, so just retry it" — and the client keeps all its other in-flight
streams, re-sending only the stream that didn't fit.  A connection error would
be the wrong tool: it tears down the whole connection and forces the client to
replay *every* request, punishing it for the server's own limit.

**§5.2 Flow Control** ✅
A DATA frame may be sent only when **two** windows both have credit — the
stream window and the connection window.  On the *send* (outbound) side,
`HTTP2Actor` tracks `HTTP2Actor._peer_initial_window_size` (seeds a new
stream's window) and a single shared `HTTP2Actor._conn_window`
(`ConnectionWindow`, `sender.py`) that every stream's `HTTP2Sender` debits and
awaits — one object, not a per-sender copy, so N concurrent streams share one
stream-0 budget rather than drifting apart (the bug this fixed: see §6.9.1's
sibling note below). Updated by `SettingsResponder` and
`WindowUpdateResponder`. On the *receive* (inbound) side each stream's
`HTTP2Recipient` tracks its own byte budget against the window we advertised
in SETTINGS — the credit mechanics, and the bug that follows from getting
either direction wrong, are in §6.9.1.  *Because* the two windows do two jobs
that a single window can't do at once.  The **stream** window stops any one
stream from hogging the connection — fairness between streams.  The
**connection** window caps the *total* data in flight across *all* streams at
once — a bound on how much the receiver has to buffer.  You need both:
per-stream fairness alone can't bound total memory, and a total bound alone
can't stop one stream from starving the rest.

**§5.3 Prioritization** ✅ (as deprecation)
RFC 9113 **§5.3.2** deprecated the priority *tree* (dependencies and weights)
that **RFC 7540** had originally defined.
BlackBull does not build the tree; it accepts PRIORITY frames and translates
them, plus RFC 9218 `PRIORITY_UPDATE`, into a simple
`{'urgency', 'incremental'}` hint (built by `_resolve_priority() in
http2_actor.py` / `_build_h2_extensions() in http2_actor.py`) exposed at
`scope['extensions']['http.response.priority']`.  *Because* the tree was
unimplementable interoperably — RFC 9113 itself removed it, and modern clients
send RFC 9218 urgency signals instead.  (Background: §5.3.1.)

**§5.4 Error Handling** ✅ — *this is where the actor model earns its place.*
A **connection error** (§5.4.1) goes through `HTTP2Actor._connection_error()`:
build GOAWAY with the accumulated `HTTP2Actor._last_peer_stream_id`, flush it,
then `writer.close()` so the peer sees FIN after the GOAWAY; idempotent via
`HTTP2Actor._goaway_sent`.  A **stream error** (§5.4.2) sends RST_STREAM and lets the
stream's task die *without taking the connection down* — because
`HTTP2Actor.run()` supervises every stream task in an `asyncio.TaskGroup`.
*Because* the RFC's two-tier error model maps exactly onto the actor supervision
model: stream-fatal = isolate the child task, connection-fatal = propagate and
GOAWAY.

**§5.5 Extending HTTP/2** ✅
Unknown frame types **MUST** be ignored (outside a header block).  The parser
returns `None` for an unrecognised type and `HTTP2Actor._frame_loop()` does
`continue`.  *Because* forward-compatibility is a hard requirement — an endpoint
that errored on unknown frames could not coexist with a peer using a newer
extension.

---

## §6 — Frame Definitions

**How frames are represented and dispatched.**  Every frame type is its own
Python class — a `FrameBase` subclass in `frame_types.py` (`Data`, `Headers`,
`Ping`, `SettingFrame`, …) that knows how to parse its own payload.
`HTTP2Actor._frame_loop()` then routes each parsed frame one of two ways:

- the four frames that drive stream state and the request lifecycle — **HEADERS,
  CONTINUATION, DATA, GOAWAY** — go to a dedicated `HTTP2Actor._on_*_frame()`
  method (the `case` arms of a `match` on the frame type);
- the connection-control frames — **PING, SETTINGS, WINDOW_UPDATE, PRIORITY,
  RST_STREAM** — fall through to a `Responder` class built by `ResponderFactory`
  (e.g. `PingResponder`, `SettingsResponder`), keeping the loop itself small.

The subsections below take the frame types in RFC order.  Each heading names
the frame's **class** (the RFC definition → Python); the body says where it is
dispatched and which method or responder handles it — DATA, in particular, is
not one method but a chain of steps.

**§6.1 DATA — `Data(FrameBase)`** ✅
`HTTP2Actor._frame_loop()` dispatches a parsed `Data` frame to
`HTTP2Actor._on_data_frame()`, but the handling fans out across several steps —
the `Data` object is read in more than one place: a state check (DATA on
HALF_CLOSED_REMOTE or CLOSED → RST STREAM_CLOSED, since the peer already sent
END_STREAM), content-length accounting as bytes accumulate (§8.1.1), delivery to
the stream's recipient, dual flow-control crediting on *consumption* (§6.9.1),
and back-pressure (an inbound-window overrun or degenerate tiny-frame flood →
RST ENHANCE_YOUR_CALM — the true abuse backstop once crediting is consume-based).
*Because* DATA is the only frame that both moves application bytes and consumes
flow-control credit — so it carries the most invariants and touches the most code.

**§6.2 HEADERS — `Headers(FrameBase)`** ✅
A `Headers` frame is handled by `HTTP2Actor._on_headers_frame()`, which runs the
admission gauntlet: concurrency check first → REFUSED_STREAM if over the limit
(§5.1.2).  If `END_HEADERS` is unset, stash the frame and set the
`_frame_loop()`-local flag `waiting_continuation` (§6.10).
Malformed headers → RST PROTOCOL_ERROR before dispatch (§8.1.1).  Extended
CONNECT (`:protocol`) routes to WebSocket (RFC 8441).  *Because* HEADERS is the
stream's birth certificate — every admission, framing, and routing decision has
to happen here, before any application code runs.

**§6.3 PRIORITY — `Priority(FrameBase)`** ✅
A `Priority` frame is dispatched to `PriorityResponder`, but its payload-length
guard (it **MUST** be exactly 5 bytes → stream FRAME_SIZE_ERROR) is enforced in
`HTTP2Actor._frame_loop()`, and the urgency signal is mapped by
`_resolve_priority() in http2_actor.py`.  PRIORITY on a not-yet-seen stream
creates an idle `Stream` node.  The signal is mapped to RFC 9218 urgency (see
§5.3).  *Because* the frame is still valid wire
syntax even though the tree semantics are deprecated — reject the malformed,
accept-and-translate the well-formed.

**§6.4 RST_STREAM — `RstStream(FrameBase)`** ✅
The basic "any → CLOSED" transition is applied by `RstStreamResponder`; ahead of
it, `HTTP2Actor._frame_loop()` carries the **CVE-2023-44487 (Rapid Reset)**
mitigation: a rolling 1-second counter, >20 RST/s → GOAWAY ENHANCE_YOUR_CALM,
raised *before* stream-state validation so abusive RSTs on idle/unknown streams
still count.
*Because* `SETTINGS_MAX_CONCURRENT_STREAMS` cannot catch the attack — a stream
reset in the same round-trip never counts as "concurrent."

**§6.5 SETTINGS — `SettingFrame(FrameBase)`** ✅
The server sends its own SETTINGS first from `HTTP2Actor.run()` (§3.4); a peer's
`SettingFrame` is handled by `SettingsResponder`, which updates the window sizes
and answers with ACK (§6.5.3).
`SETTINGS_ENABLE_CONNECT_PROTOCOL` (§6.5.2 / RFC 8441 §3) is advertised **only**
when `BB_H2_ENABLE_WEBSOCKET=1`.  *Because* you must not invite Extended CONNECT
unless you can service it.

**§6.6 PUSH_PROMISE — `PushPromise(FrameBase)`** ✅
A server-sent `PushPromise` is built by `HTTP2Actor._handle_push()`: allocate the
next even id, build pseudo-headers from the parent scope, send PUSH_PROMISE
**on the parent stream** (so the client can associate it), then
spawn the synthetic pushed request as its own stream task.  See §8.4 for the
server-push semantics.  `HTTP2Actor._frame_loop()` rejects a `stream_id==0`
PUSH_PROMISE by testing the type against the class-level frozenset
`HTTP2Actor._STREAM_ONLY_FRAME_TYPES`.  *Because* the promise has to reference
the request that triggered
it, which is the parent stream — sending it on the new stream would leave the
client unable to correlate.

**§6.7 PING — `Ping(FrameBase)`** ✅
A `Ping` frame is handled by `PingResponder`: PING with ACK → no-op (it answers
our own PING); PING without ACK → echo with ACK, unchanged opaque data.
*Because* PING is the connection liveness primitive;
the only correct response is the identical payload with the flag flipped.

**§6.8 GOAWAY — `GoAway(FrameBase)`** ✅
A `GoAway` frame is handled in two directions.  **Incoming**
(`HTTP2Actor._on_goaway_frame()`): echo a GOAWAY mirroring the peer's
`last_stream_id`, then inject `http.disconnect` into every recipient and return
from the loop.  **Outgoing** (`HTTP2Actor._connection_error()`): the §5.4.1
connection-error path.  *Because* §6.8
asks an endpoint to tell its peer which streams it processed before closing —
echoing the last id is how the peer learns what it may safely retry.

**§6.9 WINDOW_UPDATE — `WindowUpdate(FrameBase)`** ✅
A `WindowUpdate` frame is handled by `WindowUpdateResponder`: `stream_id==0`
credits the connection window; non-zero credits a stream's send window.  A zero
increment is PROTOCOL_ERROR; overflow past 2³¹−1 is
FLOW_CONTROL_ERROR.  *Because* the two scopes share one frame type but mean
different things — the responder must dispatch on the stream id.

**§6.9.1 The Flow-Control Window** ✅
The dual-credit mechanic in full — credited on *consumption*, not delivery:

```python
# in HTTP2Recipient.__call__() — when the app pops an event off the queue
event, credit = await self._ensure_queue().get()
if credit and self._credit_cb is not None:
    self._uncredited -= credit
    await self._credit_cb(credit)   # → window_update(stream_id, n); window_update(0, n)
```

A single DATA frame debits *both* windows (§5.2), so both must be credited
back — one `WINDOW_UPDATE` on the stream, one on stream 0 (the connection).
Credit is sent when the **application consumes** the event, not when the
frame is *delivered* to the recipient's queue — a stalled handler (blocked on
`yield` under response back-pressure, or simply CPU-starved) then stops
crediting, the peer's window closes, and the peer back-pressures on its own:
the RFC's intended mechanism, working end-to-end rather than stopping at the
server's queue.

*Because* enqueue-time crediting hides exactly the failure it should catch.
Crediting the moment a frame lands in the queue reopens the peer's window
whether or not the application is keeping up — so a queue with a bounded
depth (frames, not bytes) becomes the only backstop, and once a slow-consumer
handler fills it, the server's only remaining move is `RST_STREAM`. That
surfaces as *application* churn (dropped streams under load) rather than the
protocol back-pressure RFC 9113 actually describes. Crediting on consumption
means the recipient's queue is sized in **bytes** — capped at the same
value advertised in SETTINGS_INITIAL_WINDOW_SIZE, since a conformant peer can
never have more un-credited bytes in flight than that — so `RST_STREAM` is
reserved for a genuine abuse case (a peer that keeps sending past its closed
window, or a degenerate flood of near-zero-length frames the byte budget
can't see; the latter gets its own small frame-count cap).

A stream that finishes — or is cancelled by `RST_STREAM` — without its
handler draining the whole body leaves an "un-credited" balance: bytes the
peer already debited from the shared *connection* window that were never
paid back, because the app never popped them. `HTTP2Actor._release_recipient_credit`
replays that balance to stream 0 when the stream is released (either
completion path), so the connection window can't ratchet down to zero across
a long-lived, high-churn connection. The stream-level side is not replayed —
the stream is gone (§5.1) and any further frame on that id is `STREAM_CLOSED`.

*Because* the connection window is the easy half to forget, and forgetting it
fails *late*.  Credit only the stream window — the obvious half — and everything
works until ~65535 cumulative bytes have flowed; only then does the shared
connection window reach zero, after which *every* stream stalls, even ones with
plenty of their own credit.  A bug that surfaces only on a long-lived connection
is exactly the kind that is easy to introduce and hard to notice, so crediting
both windows — accounting for every byte the peer was ever debited for,
including ones an abandoned stream never read — is the invariant to hold onto.

**§6.10 CONTINUATION — `Continuation(FrameBase)`** ✅
A `Continuation` frame is handled by `HTTP2Actor._on_continuation_frame()`,
legal only while the `_frame_loop()`-local `waiting_continuation` is set; any
other frame in that state → connection PROTOCOL_ERROR, checked *first* in the
loop.  Bytes accumulate into
`raw_block`; if it exceeds `BB_HEADER_MAX_TOTAL` (64 KiB) the stream is reset
with ENHANCE_YOUR_CALM *before* `Headers.parse_payload()` — the
**CONTINUATION-flood / CVE-2024-27983** defence.  *Because* an unbounded
CONTINUATION stream is an OOM vector: you must cap the buffer before handing it
to the HPACK decoder.

---

## §7 — Error Codes

**§7 Error Codes** ✅
The full `ErrorCodes` enum is used across the loop and responders:
PROTOCOL_ERROR, STREAM_CLOSED, FRAME_SIZE_ERROR, FLOW_CONTROL_ERROR,
REFUSED_STREAM, CANCEL, ENHANCE_YOUR_CALM.  Each appears above next to the
condition that raises it; the §9 threat table cross-references the
security-relevant ones.  *Because* the error code *is* the protocol's contract
with the peer — REFUSED_STREAM vs CANCEL vs ENHANCE_YOUR_CALM each tell the
client a different thing to do next.

---

## §8 — Expressing HTTP Semantics

**§8.1 Request/Response Exchange** ✅
`HTTP2Actor._spawn_stream_task()` bridges connection and application: count the
stream, create the `StreamActor` (or direct-dispatch coroutine), optionally wrap
in a request-timeout (`BB_REQUEST_TIMEOUT` → RST CANCEL on expiry) and a
per-worker concurrency semaphore (`BB_H2_ACTIVE_STREAMS_1W`), and register the
done-callback.  An HTTP message is HEADERS → zero or more DATA → optional
trailing HEADERS.  *Because* this is the seam between protocol and ASGI app;
everything protocol-level must be settled before the app sees a scope.

**§8.1.1 Malformed Messages** ✅
Malformed requests are RST PROTOCOL_ERROR *before the application sees them*,
checked at two points: the direct-HEADERS path in
`HTTP2Actor._on_headers_frame()` and the post-CONTINUATION path in
`HTTP2Actor._on_continuation_frame()`.  Content-length is validated by
accumulating `stream.received_data_bytes`: excess on any frame → immediate RST;
deficit at END_STREAM → RST (padding excluded).  *Because* §8.1.1 makes the
server, not the app, responsible for rejecting framing-level malformation — a
malformed request must never reach handler code.  (RFC 7540 located the
content-length rule at §8.1.2.6; RFC 9113 folds it into §8.1.1.)

**§8.2 HTTP Fields / §8.2.1 Field Validity** ✅
Field-level violations are flagged by `Headers.parse_payload()` /
`parse_headers() in parser.py` (the `malformed` flag) and rejected as above.
**§8.2.2 Connection-Specific Header Fields** (e.g. `Connection`,
`Transfer-Encoding`) are rejected at parse time (in `frame_types.py`).
**§8.2.3 Cookie crumb compression** ✗ — not specially handled; cookies pass
through as ordinary fields.  *Because* §8.2.3 is a compression optimisation, not
a correctness requirement.

**§8.3 HTTP Control Data** ✅
Pseudo-headers are parsed and validated before dispatch.  **§8.3.1 Request
Pseudo-Headers** — `:method`, `:scheme`, `:path`, `:authority`; the `:path`
split for pushed requests lives in `HTTP2Actor._handle_push()`.
**§8.3.2 Response Pseudo-Headers** — `:status` is synthesised on the response
path.  For the seven most common status codes (200, 204, 206, 304, 400, 404,
500) the wire bytes are precomputed in `hpack_fastpath.py` via HPACK
static-table indexing — a single-byte lookup that avoids the full encoder
path.  *Because* pseudo-headers are the request line of HTTP/2; missing or
duplicated ones are a malformed-message condition (§8.1.1).

**§8.4 Server Push → `HTTP2Actor._handle_push()`** ✅
Triggered by the ASGI `http.response.push` event from inside a handler.  Pushed
requests are GET, safe, cacheable, body-less (**§8.4.1**); the pushed response
streams on its own even-numbered stream (**§8.4.2**).  *Because* push lets the
server pre-empt a request it knows the client will make — but only for the
method/cacheability class the RFC permits.

**§8.5 The CONNECT Method** ✅ (Extended CONNECT only)
Plain CONNECT tunnelling is not offered, but **Extended CONNECT** (RFC 8441,
`:protocol=websocket`) is — that is the WebSocket-over-HTTP/2 path, opt-in via
`BB_H2_ENABLE_WEBSOCKET=1`.  *Because* the project's CONNECT use case is
WebSocket bootstrapping, not proxy tunnelling.

**§8.6 Upgrade / §8.7 Request Reliability / §8.8 Examples** ✗ / n/a
`Upgrade` does not exist in HTTP/2 (§8.6 explicitly forbids it).  §8.7
(idempotency/retry hints) is the client's concern; §8.8 is illustrative.

---

## §9 — HTTP/2 Connections

**§9.1 Connection Management / Reuse** ✅
Connection lifetime, idle timeouts, and reuse are owned by `ConnectionActor` and
the deadline subsystem, not by `HTTP2Actor`.  *Because* these are transport
concerns shared with HTTP/1.1 and WebSocket.

**§9.2 Use of TLS Features (§9.2.1–§9.2.3, Appendix A cipher list)** ✅
TLS version and cipher policy are configured where the `SSLContext` is built
(in `ASGIServer`, via `server.py`) — not in the frame driver.  *Because* the HTTP/2
layer runs only after the TLS handshake it does not perform.

---

## §10 — Security Considerations

**§10.1 Server Authority / §10.2 Cross-Protocol / §10.3 Intermediary
Encapsulation / §10.4 Cacheability of Pushed Responses** ✅
Server authority and TLS cross-protocol defence rest on the TLS layer (§9.2);
intermediary-encapsulation defence is the §8.2.1/§8.2.2 field-validity checks
already enforced at parse time; pushed-response cacheability follows from the
§8.4.1 safe/cacheable constraint.

**§10.5 Denial-of-Service Considerations** ✅ — the cross-cutting threat table:

| Threat | RFC | Mitigation | Code |
|---|---|---|---|
| Rapid Reset (CVE-2023-44487) | §6.4 | Rolling 20/s RST limit → GOAWAY ENHANCE_YOUR_CALM | `HTTP2Actor._frame_loop()` |
| CONTINUATION flood / CVE-2024-27983 (§10.5.1) | §6.10 | `len(raw_block) > BB_HEADER_MAX_TOTAL` → RST before parse | `HTTP2Actor._on_continuation_frame()` |
| `stream_id==0` for stream-only frames | §6.1–6.4, 6.6, 6.10 | `HTTP2Actor._STREAM_ONLY_FRAME_TYPES` → connection PROTOCOL_ERROR | `HTTP2Actor._frame_loop()` |
| Oversized single header block (§10.5.1) | §4.3, §8.1.1 | `malformed` flag set during parse → RST | `parse_payload() in frame_types.py` / `parse_headers() in parser.py` |
| Stream exhaustion | §5.1.2 | RST REFUSED_STREAM before scope is built | `HTTP2Actor._on_headers_frame()` |
| WebSocket stream exhaustion | RFC 8441 | `HTTP2Actor._ws_stream_count` cap (`BB_H2_WS_MAX_STREAMS`) | `HTTP2Actor._handle_h2_websocket()` |
| Concurrent-handler flood (1 worker) | operational | `BB_H2_ACTIVE_STREAMS_1W` semaphore via `_run_guarded() in http2_actor.py` | `HTTP2Actor._spawn_stream_task()` |

*Because* DoS hardening is the part of the spec a from-scratch server most easily
skips, and the part attackers most reliably probe.  Making it a single auditable
table is the point.  Each of these defences is exercised by the
[conformance and fuzz suites](conformance.md) — h2spec's error-handling and
flow-control cases, the in-tree Rapid-Reset and CONTINUATION-flood tests, and
the parser fuzzers — so the security posture is *tested*, not just asserted.

**§10.6 Compression / §10.7 Padding / §10.8 Privacy / §10.9 Remote Timing** ✗
Not specifically mitigated. HPACK compression-ratio attacks (§10.6) are bounded
indirectly by the header-size caps; padding (§10.7) is accepted and excluded
from content-length accounting but not otherwise normalised. *Because* these are
defence-in-depth refinements beyond the project's current threat model; they are
named here so the gap is explicit, not hidden.

---

## §11 / Appendices

**§11 IANA Considerations, Appendix A (cipher list), Appendix B (changes from
RFC 7540)** — registry and historical material; no implementation surface.

---

## Coverage summary

The denominator below is the **subsections of RFC 9113 that state a server
requirement or option**, not all ~87.  Excluded (non-normative, no
implementation surface): §1 (introduction), §2 (document organisation &
conventions), §5.3.1 (background on deprecated RFC 7540 priority), §8.8
(illustrative examples), §11 (IANA registries), Appendix A (prohibited
cipher list), and Appendix B (changes from RFC 7540).  Against that
denominator:  Against that
denominator:

| Of RFC 9113's server requirements & options | Share (approx.) | Examples |
|---|---|---|
| ✅ Implemented by BlackBull's own code | ~80% | Framing (§4.1–4.2), frame definitions (§6), stream state machine (§5.1), flow control (§5.2/§6.9.1), error handling (§5.4, §7), HTTP semantics & server push (§8), Extended CONNECT (§8.5), DoS defences (§10.5), connection setup & ALPN (§3, §9.1) |
| ✅ Implemented via dependencies | ~8% | HPACK (§4.3) → the `hpack` package; TLS 1.2/1.3 features and ciphers (§9.2, Appendix A) → Python's `ssl` / OpenSSL |
| ○ Not implemented — all optional | ~12% | Upgrade-based h2c (§3.1), cookie-crumb compression (§8.2.3), reducing a stream window mid-flight (§6.9.3), the §10.6–10.9 hardening refinements |

**No mandatory (MUST) requirement is missing** — every unimplemented item is a
MAY/SHOULD-level option.  The behaviour that makes a correct, safe server —
framing, multiplexing, flow control, stream-state and error handling, and the
§10.5 denial-of-service defences — is fully implemented, and is exercised
end-to-end by the [conformance suite](conformance.md) (h2spec for RFC 9113 and
HPACK, plus the in-tree security and fuzz tests), not merely asserted on this
page.  (Prior-knowledge h2c, §3.3, *is* supported; only the deprecated Upgrade
bootstrap is not.)

Every ✅ in the sections above is implemented; the prose notes when a
requirement is met by a dependency (`hpack`, the `ssl` / OpenSSL stack) rather
than BlackBull's own code — that is the split between the two implemented rows.

---

## See also

- [Internals](internals.md) — the actor model, hierarchy, and supervisor
  strategies referenced from §5.4.
- [Conformance](conformance.md) — the RFC test suites (h2spec, Autobahn) that
  exercise this surface end-to-end.
- [HTTP/2 guide](../guide/http2.md) — the user-facing feature surface.
- [RFC 9113](https://www.rfc-editor.org/rfc/rfc9113) ·
  [RFC 8441](https://www.rfc-editor.org/rfc/rfc8441) (WebSocket over H/2) ·
  [RFC 9218](https://www.rfc-editor.org/rfc/rfc9218) (priorities) ·
  [RFC 7541](https://www.rfc-editor.org/rfc/rfc7541) (HPACK).
