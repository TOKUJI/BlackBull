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
      │     └── RequestActor      (one per connection, rebound per request)
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
instead (`BlackBull._dispatch`, except `request_completed`,
which fires after the global middleware chain returns so a
buffering middleware such as `Compression` has finished
sending), so they fire identically under BlackBull's own
server and under external ASGI hosts.

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
- For each request: points its `RequestActor` at the request and
  awaits completion, then loops for keep-alive.  The actor, the
  sender, and the body recipient are built once per *connection*
  and rebound per request — a connection dispatches one request at
  a time, so rebinding is indistinguishable from rebuilding except
  for the allocation.
- On `Connection: Upgrade` (WebSocket): hands the reader /
  writer to a new `WebSocketActor` and exits.
- Supervisor strategy: **isolate** — an error in one request
  closes the connection only.

### `RequestActor`

- Owns one HTTP/1.1 request lifetime.  Under HTTP/1.1 one instance
  serves the whole connection, rebound to each request in turn
  (`bind`); HTTP/2 builds one per stream, because streams are
  concurrent and would otherwise interleave their state.
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

The HTTP/2 actor does not exchange dataclass messages with its callers:
per-stream request/response events flow as ASGI dicts through the
`HTTP2Recipient` (``put_event``), handler coroutines are tracked as
``asyncio.Task`` objects (``_stream_tasks``), and connection-level frames
are dispatched through the Responder registry
(`blackbull/server/response.py`).  HTTP/1.1 and WebSocket equivalents are
colocated with their respective actors.

## How exceptions propagate

| Scenario | Strategy | Reason |
|---|---|---|
| `RequestActor` handler raises | Isolate + re-emit as `error` event | Handler error must not kill the connection |
| `HTTP2Actor` framing error | Propagate to `ConnectionActor` | GOAWAY required; connection is unusable |
| `StreamActor` flow-control violation | Isolate (`RST_STREAM`) | Stream-fatal only; other streams keep going |
| raw protocol handler raises | Caught by `ConnectionActor`, re-emit as `error` event | Bridge handler error must not kill other connections |
| `ConnectionActor` unhandled | Log + close connection | One connection's failure is bounded |
| `ASGIServer` accept error | Backoff + retry | Server stays alive across transient errors |

## Receive-path invariant

The request body crosses the framework as **`bytes`**, and the end of
the body is carried by the call protocol rather than by a field beside
the payload:

```python
chunk = await recipient.next_chunk()   # bytes  → more body follows
chunk is None                          #        → body complete
raise ClientDisconnected               #        → peer vanished mid-body
```

`None` rather than `b''`, for the same reason `NativeResponse` decides
presence with `is not None`: an empty body is a real body.  The sentinel
is unambiguous on both framings — a zero-length chunk *is* the
terminator in chunked encoding (RFC 9112 §7.1), and a Content-Length
slice is never empty.

`Connection.body()` / `stream()` — and `read_body` / `stream_body`
underneath them — consume that channel directly.  The ASGI
`http.request` dict is built **only** by `HTTP1Recipient.__call__` /
`HTTP2Recipient.__call__`, which is to say only when something asks for
the ASGI encoding: a full-form handler calling `receive()`, or an
external host.  It used to be built unconditionally, so a handler using
`conn.body()` paid one dict per chunk that it never read.

The two channels share one end marker on both protocols.  `__call__`
does not *consult* it — a full-form handler calling `receive()` past the
end still gets `http.disconnect` (H1) or still waits for the disconnect
event (H2), exactly as before — but it does *set* it, so a reader that
starts on one channel and finishes on the other cannot block on a queue
that will never be fed again.

Measured on the real H1 recipient — 4 KiB chunks, 2000 requests per run,
mean ± SE over 5 runs:

| channel | ns/chunk |
|---|---|
| `receive()` (ASGI) | 803.2 ± 52.6 |
| `next_chunk()` (native) | 536.3 ± 12.3 |

267 ns/chunk, or **4.27 µs saved on a 64 KiB upload** — and the dict count
for that upload goes from 16 to 0.

## Send-path invariant

Protocol senders never choose between joining and vectored I/O
themselves.  They always call `BaseSender._write_many(parts)`,
and the internal size gate (`_VECTORED_JOIN_THRESHOLD`, 32 KiB)
decides the path:

- **Join path** (≤ 32 KiB total): concatenates parts into one
  buffer and writes once.  Cheaper for small payloads — avoids
  the memoryview setup, `sendmsg` syscall overhead, and
  writer re-registration under backpressure that vectored I/O
  imposes per call.
- **Vectored path** (> 32 KiB): uses `transport.writelines`
  with memoryviews.  Worth the overhead only for large payloads.

Callers must never call `transport.writelines` directly with
small parts.  The gate is the single decision point, and the
invariant keeps performance predictable across payload sizes.

### Response values that come from tables

Two values are rebuilt on every single response and drawn from a
small, known domain, so both are precomputed once at import:

- **The status line.**  `_STATUS_LINES` maps every `HTTPStatus` member
  to the complete `HTTP/1.1 <code> <phrase>\r\n` line, CRLF included.
- **Small Content-Length values.**  `_CONTENT_LENGTHS` holds the
  decimal ASCII for `0…8192`, which covers the body sizes that
  dominate request *count*.

Both fall back to the original expression for an input outside the
table's domain — a status code IANA has not registered renders with an
empty reason phrase (legal: RFC 9112 §4 makes it optional) rather than
raising `KeyError`, and a large body formats with `str(n).encode()`.
Tests assert the tables agree with the expressions they replaced across
the whole domain, because a table that disagrees anywhere is a wire
bug that no amount of speed pays for.

## Parse-path invariant

The HTTP/1.1 parser validates bytes with **C-level bulk operations**,
never with Python-level loops or index arithmetic.  In practice that
means `bytes.translate`, `bytes.find`, `bytes.split`, `bytes.strip`,
`bytes.count`, and precompiled regexes — and it means resisting the
intuition that fewer allocations or fewer passes must be faster.

The idiom used throughout `_parse` is a **delete-the-allowed table**:

```python
_TCHAR_OCTETS = b"!#$%&'*+-.^_`|~0123456789ABC...xyz"

if key.translate(None, _TCHAR_OCTETS):   # non-empty ⇒ a non-token octet
    raise BadRequestError(...)
```

Deleting every permitted octet leaves the empty string for valid
input, so a non-empty result *is* the rejection — one pass in C, and
no allocation in the accept case (CPython returns the shared empty
`bytes`).  Which direction to use depends on the set's size: a large
allowed set (request-target, tchar) is cheapest as a `translate`
delete table, a small forbidden set (the Host authority delimiters) as
a regex character class.

Field values are checked **once for the whole header block** rather
than once per header.  Deleting everything a block may contain leaves
only CR, LF and the CTLs a value may not carry; if what remains tiles
exactly into CRLF pairs, no field value can hold a forbidden octet and
the per-header check is skipped.  A block that fails the pre-scan
falls back to the per-header regex, so error messages never change —
the bulk pass is a fast path, never a rejection.

Three plausible-sounding alternatives were measured and **rejected**;
do not reintroduce them without new numbers:

| Idea | Why it loses |
|---|---|
| `while` + `find()` line loop instead of `split(b'\r\n')` | 1.5–2.2× slower. `split` does the finding *and* the slicing in C; the loop moves the iteration back into Python. |
| Interning pool for well-known header names | Slower than `key.lower()`. The lookup key is a fresh slice every request, so its hash is never cached and hashing costs more than lowercasing. |
| Offset arithmetic instead of `value.strip(b' \t')` | ~1.5× slower. Two Python-level scans lose to one C call. |

The through-line: an optimisation that replaces a C bulk operation
with Python bytecode pays ~30 ns per byte instead of ~2 ns, and no
reduction in pass count or allocation count makes that back.

### Validated header-line cache

The cheapest validation is the one already done.  A keep-alive peer
resends byte-identical header lines on every request — `User-Agent`,
`Accept`, `Accept-Language`, `Cookie` — so each connection keeps a
bounded dict mapping **the exact raw line bytes** to the
`(lowercased-name, stripped-value)` pair that line produced the first
time it was seen and fully validated.  A hit replaces the colon split,
the token check, the lowercase, the OWS strip and the value scan with
one dict lookup.

Four properties are what make this a replay of validated work rather
than a way to skip validation, and none of them is optional:

- **The key is the exact line bytes.** One changed byte is a miss, and
  a miss is validated from scratch.  There is no normalisation, no
  case-folding and no prefix matching on the lookup path.
- **Only a line that reached the bottom of the loop is admitted.**
  Admission is the last statement in the body, after every `raise`, so
  a rejected line can never enter.
- **The cache is per connection.**  It lives on the `HTTP1Actor`
  instance, so validated lines never cross a connection — and
  therefore never cross a tenant.
- **It is bounded** (64 entries).  The key can be attacker-controlled, so a
  peer cycling unique header names must not be able to grow it;
  admission simply stops at the limit rather than evicting.

The precedent is HPACK's dynamic table, which is this same idea blessed
at protocol level — HTTP/1.1 merely lacks the wire form for it.

### The shared spec table

A per-connection cache cannot help the **first** request on a
connection — there is nothing in it yet — and for a
`Connection: close` client that first request is the entire
interaction.  Populating a cache it will never read made that shape
**21 % slower** than having no cache at all.

So the cache has a second tier that needs no warming: a process-wide
table of header lines whose value set is *fixed by a specification*,
validated once at import and shared by every connection.  Fetch
Metadata's `Sec-Fetch-Site` / `-Mode` / `-Dest` / `-User`, the client
hints' boolean and platform forms, and the fixed tokens of RFC 9110
(`Connection`, `TE`, `Pragma`, `Upgrade-Insecure-Requests`, `DNT`)
are the same bytes for every client on every deployment.  On captured
browser traffic they are **56 % of all header lines**, and that share
does not decay with connection churn.

HPACK's *static* table is the same idea, again — and again HTTP/1.1
lacks the wire form, so the table lives in the parser instead.

Three rules keep it from being an over-fit:

- **Specification, not observation.**  A line qualifies because a spec
  enumerates its values, not because it was frequent in a capture.
  That is why no `User-Agent`, `sec-ch-ua`, `Accept-Language`,
  `Cookie`, `Referer` or `Host` line is seeded, though every one of
  them is more frequent than most entries that *are*.
- **No framing header, ever.**  `Content-Length`,
  `Transfer-Encoding`, `Host`, `Expect` and `Upgrade` are excluded by
  an explicit check that raises at import.  A shared table is the last
  place a framing decision should come from.
- **Built through the real rules.**  Each entry is validated at import
  with the same octet checks the parse loop applies, and a test
  asserts every entry equals what parsing that line actually produces
  — so a hand-written pair cannot drift from the parser.

The table is immutable and never written to at runtime; learning goes
to the per-connection dict alone, so nothing a peer sends can affect
another connection.

**Why it is not a benchmark artefact.**  A load generator resends one
byte-identical request, which is the cache's best case and not what a
real client does.  The numbers below therefore come from **captured
traffic**, not from a model of it: a real Chromium loading a real page
(document, 2 stylesheets, 3 scripts, 12 images, 2 XHRs), with every
request head recorded exactly as it arrived
(`bench/hotpath/capture_browser_headers.py`).

That capture is what makes the mechanism legible.  21 requests carried
**275 header lines drawn from only 26 distinct ones** — 8 of which
(`Host`, `Connection`, `User-Agent`, `Accept-Encoding`,
`Accept-Language`, three client hints) appeared on every single
request, while `Accept`, `Referer` and the `Sec-Fetch-*` family varied
by destination.  Because the cache is keyed per **line**, a request
that changes four of its thirteen lines still hits on the other nine;
what governs the hit rate is the *variety* of distinct lines on a
connection, not whether requests are identical.  Chromium also emits
its headers **in different orders** for navigations and subresources —
which a whole-block cache would be defeated by and a per-line one does
not notice.

A cache that can only *learn* has a ceiling of
`(lines − distinct) / lines` — every distinct line must miss once —
which is **90.5 %** on this capture.  The shared table exceeds it,
because a pre-seeded line never misses at all.

The one thing a single capture cannot settle is how far a page spreads
across connections: Chromium opens up to six per origin, and the
learned tier sees fewer repeats the wider the spread.  The seeded tier
does not care.  Re-dealing the same requests across N connections
shows which half is which:

| connections | hit rate | seeded | learned | parse cost |
|---:|---:|---:|---:|---:|
| 1 (as captured, no latency) | 96.7 % | 56.0 % | 40.7 % | 8.96 → 5.85 µs (**−35 %**) |
| 2 | 94.2 % | 56.0 % | 38.2 % | 8.93 → 6.17 µs (−31 %) |
| 4 | 89.8 % | 56.0 % | 33.8 % | 8.95 → 6.28 µs (−30 %) |
| 6 (Chromium's per-origin max) | 85.5 % | 56.0 % | 29.5 % | 8.90 → 6.51 µs (**−27 %**) |
| 12 | 72.7 % | 56.0 % | 16.7 % | 9.00 → 7.27 µs (−19 %) |

Read the two middle columns rather than the total.  That the seeded
column is *flat* is structural — the table does not depend on
connections, so churn cannot erode it.  That it sits at *56 %* is a
property of this client's header mix, and another client's would put
it somewhere else.  The shape generalises; the level does not.

!!! note "Where these numbers come from"

    AMD Ryzen 5 7600X (6C/12T, Zen 4), Ubuntu 24.04 on **WSL2** — a
    desktop VM, not an isolated benchmark host.  CPython **3.14.6**,
    stock `asyncio` (`BB_UVLOOP=0`).  Both arms in one session: the
    "before" column is `v0.66.0` (`450df4a`) in a `git worktree`,
    the "after" column the same commit plus this work.  `min` of 7
    for the µs-scale parser figures, medians of 5–7 interleaved
    sweeps for anything driven by `wrk` (whose run-to-run spread on
    this host is ~10 %).  Browser capture: Edge/Chromium 150 headless.

    Two caveats that bound how far these generalise. The capture is
    **n = 1** — one page load in one browser, whose requests are
    intra-correlated by construction, so it is a point estimate with
    no variance estimate. And **localhost has no latency**, which is
    why Chromium used a single connection; over a real RTT it opens
    up to six per origin, so **−17 % is the figure to quote for WAN
    traffic**, not −30 %.

    Full record, including how to size a proper multi-site sample from
    the HTTP Archive corpus:
    `bench/results/sprint87-frontend-spike-20260730/ENVIRONMENT.md`.
    Every harness under `bench/hotpath/` prints its own environment
    stamp (`bench/hotpath/provenance.py`) as its first output.

So on traffic shaped like this capture, **−27 % to −35 %** is what one
can expect — the lower figure over a real network.  Not a guarantee:
the hit rate follows how much of a client's header set has
spec-enumerated values and how many requests share a connection, and
both vary by client and by deployment.  A client sending mostly
bespoke headers on connections it closes immediately gets close to
nothing; it does not get less than nothing, which is the part that is
guaranteed.

**Short-lived connections.**  The seeded tier needs no warming, so
reconnection costs only the learned half.  Replaying the same capture while
closing the connection every K requests — HttpArena's `limited-conn`
profile is K = 10, and `Connection: close` is K = 1:

| requests per connection | parse cost |
|---:|---:|
| 1 (`Connection: close`) | 8.75 → 8.21 µs (**−6 %**) |
| 2 | 8.96 → 7.23 µs (−19 %) |
| 10 (`limited-conn`) | 8.90 → 6.21 µs (**−30 %**) |
| keep-alive | 8.77 → 6.27 µs (−29 %) |

Without the seeded tier the K = 1 row was **+21 %** — a real regression
on connection-churn traffic, and the reason the tier exists.

On ordinary traffic that merely fails to repeat — every header value
unique, or the cache already full — the path is still **5 % faster**
than not having the cache, because the same change hoisted the
per-request settings read out of `_parse` to connection setup.  A
connection whose headers carry a per-request unique value (a request
id, a timestamp, a rotating token) misses on those lines and still hits
on the rest.

### The worst case, stated

The cache key is attacker-controlled, so the bounds above are not
tuning — they are the security argument, and the resource that needs
bounding is **bytes, not entries**.  With an entry-count bound alone,
64 entries × `BB_HEADER_MAX_LINE` (8 KiB) retains **~1 MiB per
connection**, which a peer pins by sending each line once and then
idling on keep-alive — 9.6 GiB across 10k connections, against 988 B
of real need.

Hence the per-line cap and the byte budget.  A peer choosing the line
length to maximise damage (`bench/hotpath/line_cache_worst_case.py`
sweeps it, because a per-line cap moves the worst case *down* to just
under itself rather than up to the limit):

| | worst case | at |
|---|---:|---|
| CPU | **+19.2 %** vs no cache | 64 never-repeating 128 B lines/request |
| memory | **16 KiB/connection** (153 MiB at 10k) | same |

(Same host and toolchain as above; the memory figure is accounted
bytes — key plus the retained name/value slices — which is what
multiplies by concurrent connections.)

Both are bounded, and the CPU figure sits on top of a request the peer
chose to make expensive — 40.7 µs against ~6 µs for an ordinary one.
Above the per-line cap the cache is skipped entirely, lookup included,
so a peer sending 8 KiB lines makes the parse *faster* than the
no-cache build rather than slower.

None of this applies to HTTP/2: HPACK already does it on the wire, and
better.  This is the HTTP/1.1 path recovering what H/2 gets for free.

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
  every connection's idle timer *and* the write timeout that
  bounds a single `drain()`; arming and disarming a deadline
  are attribute writes + set operations rather than per-arm
  `loop.call_later` calls.  Every configurable timeout goes
  through it, so turning one on costs no per-request timer.
  The write deadline binds its task when it is armed rather
  than when it is built, because HTTP/2 drains one
  connection-level writer from per-stream tasks.
- **Sender / Recipient** — `blackbull/server/sender.py`,
  `blackbull/server/recipient.py`.  Buffer responses on the way
  out, parse incoming frames on the way in.  Cache headers
  (Date, common content-types) for hot-path savings.
- **Send-path size gate** — protocol senders hand multi-fragment
  responses (`(head, body)`, `(frame_header, payload)`) to
  `BaseSender._write_many`, which owns the join-vs-vectored
  decision: fragments totalling at most 32 KiB are joined and
  sent as one `write()`; larger payloads use vectored
  `transport.writelines` to skip the full-body copy.  The gate
  exists because CPython's selector transport makes `writelines`
  a net loss for small payloads — per-fragment `memoryview`
  allocations plus `sendmsg` setup outweigh the avoided memcpy,
  and when the kernel send buffer is full, `writelines` attempts
  a send and re-registers the writer on every call where
  `write()` merely appends to the transport buffer.  Measured
  crossover on a drained socketpair: joining wins up to 16 KiB,
  vectored I/O wins from 64 KiB.  Protocol senders express
  *what* they have, never *how* to send it — the transport
  strategy lives in exactly one method.

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
