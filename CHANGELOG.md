# Changelog

All notable changes to BlackBull are recorded here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## Versioning

BlackBull uses [ZeroVer](https://0ver.org/) prior to a 1.0 commitment:

- `0.MINOR.PATCH`
- `MINOR` advances at a sprint close that changes what users see — a new
  capability, a new or changed public API, new environment variables, or a
  behaviour change an application has to react to.  Not every sprint earns
  one: a version bump asks every adopter to work out what changed, and
  spending one on an unchanged public surface spends their attention for
  nothing.  The minor number therefore does **not** equal the sprint number —
  patch releases and combined-sprint releases have introduced an offset
  (Sprint 49 closed as `v0.43.0`; Sprint 97 closed as `v0.73.1`).
- **Decide on the strongest justification present, not the first one found.**
  A release usually satisfies more than one of the conditions above, and they
  are not equally strong: a documented public-API addition qualifies formally,
  while a behaviour change an application must react to is what the *effective
  surface* test is actually asking about.  Name the strongest — the release
  notes are what tells an adopter whether they have work to do, and leading
  with a formal qualifier buries that.  (v0.79.0 was first explained as a MINOR
  because it added `app.drain_events()`, a test seam an adopter can ignore
  entirely.  The version was right; the reason given was the weaker of the two
  it had.  What earned it was MQTT QoS 3 becoming a disconnect.)

- **MINOR is judged by effective surface, not the diff.**  Removing a public
  API key that has been deprecated with a documented replacement long enough
  for adopters to have migrated is a **PATCH**: an adopter who followed the
  deprecation does nothing, so the surface they see is unchanged.  Only a
  removal that forces action on current, doc-following adopters is a MINOR.
  (Sprint 99 removed `scope['http2_priority']`, deprecated since v0.31.0; it
  shipped as v0.75.0 under the pre-change rule, which now reads it as a
  PATCH.)
- **Exception (2026-06-25)**: Sprints 50 through 54 are not independently
  released — they ship together as `v0.44.0` (the next minor after `v0.43.0`),
  the MQTT-broker debut plus its actor-model rebuild and the protocol-agnostic
  connection dispatcher.  Normal per-sprint versioning resumes at the next
  sprint close as `v0.45.0`.
- **Exception (2026-08-16)**: the attack-resistance programme ships as **one**
  MINOR when it is complete, not one release per sprint.  It spans several
  sprints (request-body limits, WebSocket message bounds, MQTT resource
  bounds, HTTP/2 time bounds, frame-rate metering) and its deliverable is a
  *coherent* resource-governance surface — a partial release would advertise a
  security posture the code does not hold yet, and would ask adopters to
  re-read the same subject three times.  Sprints inside the programme close
  without cutting a release; `[Unreleased]` accumulates until the last one
  lands.
- `PATCH` covers bug fixes, security fixes, and harness work — whether they
  land between sprints or close one.
- No `1.0.0` until the framework's identity (pure-Python H1 parser,
  BlackBull-internal `ASGIServer`, per-process tick scanner deadline
  subsystem) and public API have stabilised across several sprints.

The runtime version is exposed as `blackbull.__version__` via
`importlib.metadata.version("blackbull")` — single source of truth is
`pyproject.toml`.  Re-run `pip install -e .` after a local version bump
so the editable install's metadata catches up.

## [Unreleased]

### Changed

- **A worker now drains on `SIGTERM` instead of dying where it stands.**  A
  request in flight when the signal arrived was dropped — the client saw the
  connection close with no response — because the worker restored `SIG_DFL`.
  That was not carelessness: the inherited handler did not stop the asyncio
  loop, so terminating the process was the only thing that worked, and the
  master's `shutdown_timeout` was a wait rather than a drain.

  The worker installs a loop signal handler instead.  It closes the listeners,
  stops accepting, and lets the connections it already accepted finish, for up
  to **8 seconds** — inside the master's 10-second budget, so the wait ends in
  the worker's own cancel rather than in a `SIGKILL`.  Nothing in flight is
  cancelled while the budget lasts, because a cancelled handler is a client
  holding a half-written response.

  `--reload` uses the same path, so a code change now recycles workers by
  draining them rather than by killing them.

  No documented behaviour changed — `docs/` never promised in-flight
  completion, and now states the contract in
  `docs/deployment/workers.md#shutdown`.

### Added

- **A deployment states the sockets it wants.**  `Server` bound one HTTP
  listener — `self.port`, singular — and held one server-wide `ssl_context`
  that applied to it.  A deployment needing cleartext and TLS at once could not
  say so, and ran one process per port to get there.

  ```python
  from blackbull import BlackBull, Listener, Tcp

  app.run(listeners=[
      Listener(Tcp(8080)),                 # cleartext: h1 / h2c / WebSocket
      Listener(Tcp(8443), tls=ctx),        # TLS: ALPN picks h2 or http/1.1
      Listener(Tcp(1883), speaks='mqtt'),  # one owner, by default
  ], workers=4)
  ```

  Every listener is served by every worker unless it says otherwise, and TLS
  belongs to the listener that terminates it — so two ports can present
  different certificates, and configuring one no longer silently converts a
  port to HTTPS.  `app.run(port=8000)` is unchanged: it builds one listener
  and nothing else moves.  See `docs/guide/listeners.md`.

- **`raw_handler(..., stateful=True)`** — whether an exchange depends on what
  an earlier one left behind, true by default.  A stateful protocol is served
  by one worker, so with `workers > 1` it is reached on its own port only; the
  shared port would answer from whichever worker accepted.  One with *no*
  dedicated port cannot be given an owner at all and is now refused before the
  workers fork, naming the three ways out.  Pass `stateful=False` for a
  protocol that keeps nothing between exchanges.

### Fixed

- **A port-bound protocol is bound however the HTTP listener was said.**
  `open_socket` had four paths that each returned as soon as they had set the
  HTTP sockets, and the protocol-socket bind was appended to the last one.  A
  Unix-socket deployment, a socket-activated one, and **every process after an
  auto-reload** therefore had no broker — the reload handoff carries the HTTP
  descriptors only.  No warning, no test.

- **A worker lets go of the listeners it does not serve.**  `fork` copies the
  descriptor table, so every worker held the broker's listening socket and
  every other worker's — measured 4 of 4.  Single ownership was a property of
  what nobody called, not of who could; a worker now closes what it was not
  given, in its own process.

- **A stream's coroutine is built past the two places it may never reach**,
  removing the `coroutine 'StreamActor.run' was never awaited` warning (EC2
  count 4 → 0 across sixteen profiles).

- The `AttributeError: 'NoneType' object has no attribute 'close'` that
  survived the v0.78.1 log work is **not BlackBull's** — a CPython defect in
  `selector_events.py`, reproduced deterministically on 3.12, 3.13 and 3.14 and
  reported upstream as [gh-156512](https://github.com/python/cpython/issues/156512).

## [0.79.0] — 2026-08-28

### Added

- **`app.drain_events(timeout=)`** — wait for fire-and-forget `@app.on`
  observers instead of sleeping.  Two of the three hook kinds were already
  assertable (`@app.intercept` and `@app.on(..., blocking=True)` are awaited
  before a request returns); the third is detached on purpose, so asserting
  its side-effect straight after a request raced it.

  Returns `False` if the timeout expired with work outstanding, and
  **cancels nothing** — a helper that cancelled the work it was asked to
  observe would destroy the effect the test exists to see.  It drains to
  quiescence rather than to a snapshot: an observer may itself emit, and
  what that spawns is waited for too.

### Fixed

- **A cached streaming response was sent twice.**  The cache middleware holds
  the header arm until the body completes so it can attach an ETag; a
  streaming response has no completion to wait for, so it switches to
  pass-through — and the switch flushed what it held without clearing it.  The
  terminal chunk flushed the same buffer again: two `http.response.start`
  events on HTTP/1.1, a duplicated body on HTTP/2.  Three chunks of
  `aa`/`bb`/`cc` arrived as `aaaabbcc`.  Middle chunks were withheld as well,
  and the stream was accumulated for caching, which the docstring says does
  not happen.

- **A non-ASCII `Host` header closed the connection with no reply.**  Neither
  check in front of `_parse_host_header` excluded a high byte, so decoding
  raised where nothing was ready to answer.  It is now a `400`, and the
  header parser decodes with `errors='replace'` so the value cannot take the
  connection down on the way to being rejected.

- **A self-referencing dataclass never terminated OpenAPI schema
  generation.**  `_type_to_schema` recursed into `_dataclass_to_schema` and
  back with no record of what was already being walked.  One self-reference
  produced a 53,753-byte, 1000-deep schema — stopping only because a
  `RecursionError` inside `get_type_hints` was swallowed by a bare `except
  Exception`.  Two self-references branch `2^depth` and had no such accident.
  A `frozenset` of types under expansion is now threaded through every
  recursive call.  Node: 53,753 → 164 bytes; tree: hung → 225 bytes.

- **Object-form WebSockets broke under an external ASGI host.**  The
  native→ASGI boundary expanded `NativeResponse` but passed `NativeWSMessage`
  straight through, so what `WebSocket.accept()` / `.send()` emitted reached
  uvicorn as an object where it subscripts `event['type']`.  Both edges (the
  external host and a `scope`-declared middleware) now share one expansion, so
  a native message cannot be handled at one and forgotten at the other.

- **The bare-`yield` provider warning fired on providers that were correct.**
  The check counted every statement *later in the file* rather than every
  statement *reachable* after the yield, so a provider that degrades
  gracefully — `yield None` then `return`, with the real acquisition and its
  `finally` further down — was reported as leaking.  It is judged per
  execution path now; a genuine cleanup-after-bare-`yield` is still caught.

- **Shutdown abandoned observers that observers spawned.**  The dispatcher's
  shutdown drain waited on one snapshot of the pending set, so an observer
  that itself emits — close the session, then emit an audit record, the shape
  `docs/guide/events.md` documents — had its second generation left running
  while the drain reported success.  Silently: the overrun `WARNING` only
  covers tasks that were in the snapshot.  It drains to quiescence now, as
  `drain_events()` already did.  `observer_shutdown_timeout` remains the
  ceiling.

### Changed

- **MQTT QoS 3 is rejected instead of delivered.**  QoS is a two-bit field,
  so it can hold 3, and the codec returned whatever the mask produced.  MQTT 5
  §3.3.1-4 makes both bits set a Malformed Packet.  Nothing downstream caught
  it: the broker acknowledges QoS 1 and 2 only, so a QoS-3 PUBLISH was routed
  to subscribers and retained **with no acknowledgement at all** — delivered,
  and invisible to the publisher.  It is now rejected in the codec and again
  in the broker, with DISCONNECT `0x81` (Malformed Packet) then close, per
  §4.13.

  **This is a behaviour change.**  A client that sent QoS 3 and had it
  delivered will now be disconnected.

- **HTTP/2 pseudo-header and frame-type lookups no longer go through the
  enum metaclass.**  `EnumClass(value)` is not a constructor — members are
  singletons, and the call is a value lookup routed through
  `EnumType.__call__` → `Enum.__new__`, two Python frames to perform one
  dict lookup.  Module-level maps do the same lookup directly.

  Two structural improvements came with it, and they matter more than the
  instructions: `_KNOWN_PSEUDO` held the same six names as `PseudoHeaders`
  in a second representation, so membership was checked twice with a `str`
  allocated to feed the second check — retired.  And an unknown frame type
  is a *specified normal outcome* (RFC 9113 §5.5 requires ignoring it), not
  an exception; `.get()` returning `None` says so.

  **Measured +2.65 % ± 0.15 throughput on the HTTP/2 lane** — 73.89 →
  71.89 µs/req, so 2.00 µs/req saved, 2.71 % of BlackBull's own per-request
  cost.  EC2 `m7a.2xlarge`, 12 ABBA rounds, 24 runs per arm; the paired null
  (A/A) floor was 0.28 %, which the effect clears by 9.5×.

  An HTTP/1.1 control lane ran in the same session and moved −0.30 % ± 0.20,
  consistent with zero — the change is HTTP/2-only, and a control that had
  moved with it would have meant the session measured something other than
  the diff.
## [0.78.1] — 2026-08-27

### Fixed

- **A dead connection was rediscovered once per HTTP/2 stream.**  The sender
  records "the peer is gone" in per-sender state, but it is a property of the
  *connection*: HTTP/2 builds one sender per stream over one shared writer, so
  every open stream learned it the only way a sender could — by writing into
  the dead socket.  asyncio drops those writes silently and logs a warning for
  each one past its threshold of five, so the cost showed up as log volume
  rather than as an error.

  Measured on HttpArena's published logs for this server: one 30-second
  `baseline-h2` run produced **264,278** lines of "SSL connection is closed"
  and **4,415** of "socket.send() raised exception.", while the HTTP/1.1
  lanes — one sender per connection — produced none.  Reproduced at 96 wasted
  writes out of 100 streams on both the TLS and cleartext paths.

  The discovery is now published on the writer, which every sender on the
  connection shares, and — the half that matters most — it is read from the
  transport rather than waited for as an exception.  `connection_lost` is
  delivered through `call_soon`, so between the transport recording the loss
  and the protocol learning of it there is a window in which `write()` drops
  silently and `drain()` returns without raising.  A guard that waits for an
  exception never fires there.  Measured in that window: **96 of 100 writes**
  produced a warning before, none after.

- **A client that reset mid-upload printed a full traceback.**  The read path
  surfaces the OS `ConnectionResetError` when the reset lands while a handler
  is inside `conn.stream()`.  It reached the generic handler and was logged at
  ERROR with a stack — 307 times in one sixteen-profile run — for something
  that is a client's ordinary prerogative.  It is now treated as the client
  disconnect it is, alongside `ClientDisconnected`: recorded for
  `after_handler`, logged at DEBUG, nothing sent.  The exception is still
  raised into the handler, so a truncated body is never mistaken for a
  complete one.

- **A TLS half-close claimed something asyncio would ignore.**
  `eof_received()` returned `True` unconditionally, which keeps the write half
  open after a peer half-closes — what a cleartext client doing
  `shutdown(SHUT_WR)` while awaiting its response needs.  TLS cannot offer it:
  asyncio's SSL protocol closes on EOF whatever the app protocol returns, and
  logs a warning for each ignored claim — **3,425** in a sixteen-profile run.
  The protocol now answers what the transport can honour.  Cleartext behaviour
  is unchanged.

- **A stream that lost the spawn race left an orphaned coroutine.**  A HEADERS
  frame arriving in the same turn the peer went away found the connection's
  TaskGroup already winding down; `StreamActor.run()` had been built before
  `create_task` could refuse it, so Python reported "coroutine
  'StreamActor.run' was never awaited" whenever the GC reached it — naming a
  stream unrelated to wherever the line landed.  The coroutine is closed on
  that path now.

  No public API changes.

## [0.78.0] — 2026-08-20

### Added

- **The fault-injection grid is symmetric on both axes.**  Sprint 108
  filled the last cell and then drove all four at counterparts that are
  not BlackBull — nginx, `httpx`, `curl`, `h2`.  Every cell ran.  The
  *result* objects did not match: the broken-**server** cells could assert
  what the peer did (`ExpectRequest` / `ExpectClientFrame` and an
  `expectations` log), and the broken-**client** cells had a single
  `response` slot.

  So an HTTP/2 client scenario could send an illegal frame and could not
  observe the `GOAWAY` it drew — `ReadResponse` reads one frame, the first
  frame a correct server sends is its handshake SETTINGS, and the verdict
  sits further down a stream whose depth varies by peer.  That cell could
  answer *did the server survive?* and not *what did the server decide?*

  The four cells now run one scheme.  `WaitFor…` filters, reading past
  what does not match and counting the skips; `Expect…` guards, reading
  exactly one message and recording `(match, matched)` either way:

  | | HTTP/1.1 | HTTP/2 |
  |---|---|---|
  | breaking client waits | `WaitForResponse` | `WaitForServerFrame` |
  | breaking client asserts | `ExpectResponse` | `ExpectServerFrame` |
  | breaking server waits | `WaitForRequest` | `WaitForClientFrame` |
  | breaking server asserts | `ExpectRequest` | `ExpectClientFrame` |

  Both client results gained `received` (every read, in order — `response`
  still means the most recent, so existing scenarios are untouched),
  `server_bytes_received`, `expectations`, `wait_skipped` and
  `wait_timed_out`, matching the server-side names exactly.
  `frame_matches` gained `error_code`, which is what turns "a GOAWAY
  arrived" into "the peer rejected this for *that* reason".

- **`HalfClose`, in all four vocabularies** — shut down the sending
  direction only and keep reading.  Every cell shipped `Abort` (RST) and,
  on the server side, `CloseGracefully` (a full close); neither is a
  half-close.  A peer that sends FIN and keeps reading has finished its
  request and is waiting for the answer — the ordinary end of a
  non-keep-alive exchange, and a distinct path on the receiving side,
  because a reset discards buffered data and a FIN does not.  A test
  reaching for `Abort` to stand in for it tests reset handling and reports
  a pass.

  `half_closed` on all four results records whether the transport actually
  accepted it.  TLS has no half-close, so a scenario that assumed one
  would otherwise pass while exercising the keep-alive path.

- **A named catalogue for every cell.**  The HTTP/1.1 client cell had
  none — it was reachable only through the atheris and Hypothesis
  harnesses, which generate inputs rather than name them.  Fourteen cases
  ship in `blackbull.fault_injection.catalogue.h1_client`, drawn from RFC
  9112's framing rules and the request-smuggling literature.
  `catalogue.CATALOGUES` keys all four cells by protocol **and** role
  (`h1_client`, `h1_server`, `h2_client`, `h2_server`), so a suite can
  sweep the toolkit without remembering four module paths.

- **`blackbull.testing.grpc.GrpcTestServer`** — an app-facing seam for
  testing your own gRPC servicers.  All four RPC shapes shipped and
  `grpc.md` had no testing section, so the path an application developer
  found was `grpcio`.  The framework's own gRPC tests have always driven a
  real h2c socket with `HTTP2Client`, for a reason worth stating in the docs
  rather than rediscovering: every gRPC response reports its status in
  **trailing headers**, success and error alike, so an ASGI transport
  without `http.response.trailers` support never observes the call finish —
  which is why `TestClient` cannot test gRPC at all.  `GrpcTestServer` wraps
  the boilerplate those tests repeat and hands back a `GrpcReply` with the
  trailers already read out.

  **Not a client.**  BlackBull ships a gRPC server and no gRPC client, and
  this does not change that: it is a test environment, and `.port` is public
  so `grpcio` can drive the same server if you would rather assert against
  the client your users will use.

- **`HTTP2Client.execute_scenario`** — the fault-injection grid's last cell.
  `HTTP1Client` had a scenario executor and `HTTP2Client` did not, so an
  HTTP/2 client-side scenario could only be written as procedural code
  against a raw socket.  The vocabulary mirrors the HTTP/1.1 client side
  (`SendBytes`, `ReadResponse`, `Sleep`, `Abort`, and `ScenarioResult`'s
  field names) and adds the two steps HTTP/1.1 has no use for: `SendPreface`,
  because HTTP/1.1 has no connection preface and a client scenario may want
  to delay, split or withhold it; and `SendFrame`, because HTTP/2 is framed —
  its `declared_length` sets the header's length independently of the
  payload, which is the direct way to say "the peer lied about how much is
  coming".  Eleven named cases ship in
  `blackbull.fault_injection.catalogue.h2_client`, drawn from the HTTP/2 rows
  of the project's own attack-surface work so the names line up with the
  defences that answer them.

- **`H1FaultServer`** — a deliberately misbehaving HTTP/1.1 server, for
  testing your own HTTP/1.1 client.  The toolkit documented "two directions
  are supported" and that was true, but each protocol had only **one** and
  they were opposite ones: HTTP/1.1 had the broken *client*, HTTP/2 the broken
  *server*.  A reader who took it as "both directions on both protocols" was
  wrong and nothing corrected them.  Scenarios are typed steps —
  `WaitForRequest`, `SendRawBytes` (with an optional per-byte interval),
  `Sleep`, `Abort`, `CloseConnection` — and nine named cases ship in
  `blackbull.fault_injection.catalogue.h1`: a `Content-Length` that overstates
  or understates the body, two that disagree, a chunked body that stops
  mid-chunk or never terminates, a status line trickled a byte at a time,
  headers that never end, a connection reset without a response, and a server
  that simply never answers.

  It carries the same two safety locks as `H2FaultServer` — it refuses to run
  in a production context, and refuses a non-loopback bind without
  `allow_remote=True`.

  **It assembles its own bytes.**  There is no typed `SendResponse` step and
  nothing imports the production send path, because a fault server built on
  the production serialiser cannot emit a fault that serialiser has — the one
  bug class it would be least able to find is the one in the code it shares.
  The HTTP/2 half already worked this way; the rule is now tested rather than
  implied.

### Changed

- **The two fault-injection result objects report the same things by the same
  names.**  `ScenarioH1ServerResult` had invented `completed`, `bytes_sent`
  and `elapsed` where `ScenarioH2Result` already had `steps_completed`,
  `server_bytes_sent` and `elapsed_s`; nothing was wrong with either set, and
  having both was the defect.  The HTTP/1.1 result also gains `exception`,
  `terminated` and `client_bytes_received`, all protocol-neutral and all
  already on the HTTP/2 side, and `ScenarioH2` gains the `name` its HTTP/1.1
  counterpart had.  Only `request_head` is now asymmetric, because HTTP/2 has
  no single head to capture.

- **The four fault-injection scenario vocabularies are consistent, and the
  sweep that checked it is kept as a test.**  Sprints 107 and 108 ship
  together so the question could be asked once, about a finished grid: is the
  notation unified, and can each protocol's own faults be expressed?  Running
  it found six things nothing inside the code was going to surface.

  - **`SendRawBytes` is now the name on all four.**  The escape hatch was
    `SendBytes` on the client halves and `SendRawBytes` on the server halves —
    the same two fields, split by role rather than by anything a reader could
    predict.  `SendBytes` keeps working and is **deprecated**: removal no
    earlier than **2027-08-19**, and at an arbitrary time after that.
  - **`SendFrame` can lie about its length on both HTTP/2 halves.**  The
    client half had `declared_length` from the start; the server half could
    not express the same fault.  Old usage is unchanged.
  - **Every scenario carries a `name`.**  Three of four already did.

  The gap the sweep found in `WaitForRequest` is closed too — see the next
  entry.  What remains open is CONNECT tunnelling on the HTTP/2 client side,
  which is unmodelled.

- **`match` on the request-waiting steps, as two steps rather than one.**
  `WaitForRequest` had no `match` grammar where `WaitForClientFrame` does,
  and porting it literally would have changed what the word means: on
  HTTP/2 the executor skips non-matching frames and keeps waiting, which is
  harmless because streams are independent.  HTTP/1.1 responses are
  positional (RFC 9112 §9.3), so a skipped head is a request the scenario
  can never answer — everything after it is off by one.

  - **`WaitForRequest(match=...)`** filters a pipeline: read heads until one
    matches, skipping the rest, so a scenario can answer the GET normally
    and break on the POST.  Skips land on `wait_skipped`, because they
    desync the connection and that must be read from the result rather than
    deduced.
  - **`ExpectRequest(match=...)`** is a guard: one head, nothing skipped,
    and the verdict recorded on `expectations`.  A scenario staging a fault
    against `Expect: 100-continue` is testing nothing if the client never
    sent it, and without this the run looks like a pass.

  Both halves gained the same pair — `ExpectClientFrame` on HTTP/2 — and
  both results report `wait_skipped` and `expectations` under one name.  The
  grammar (`method`, `target`, `version`, `header`, `header_absent`) **fails
  closed on an unrecognised key**, as `frame_matches` already did: a typo in
  a scenario is a bug, and silently matching on a key nobody reads hides it.

- **Typed steps for the faults that previously needed hand-built bytes.**
  `SendHeaders` (HTTP/2) builds a header block with HPACK — so a
  pseudo-header out of order, a connection-specific header, or a block HPACK
  itself would not produce (`raw_block`) stop being hex literals.
  `SendStatusLine`, `SendHeader` (with `fold=` for obs-fold), `SendChunk`
  (with `declared_size=`, the HTTP/1.1 twin of `declared_length`),
  `EndHeaders` and `EndChunkedBody` do the same for HTTP/1.1 framing.

- **An HTTP/2 scenario containing a PING can be read back.**  `Ping` is the
  one frame class whose constructor requires `data`, and the JSON
  deserialiser did not pass it — so a scenario that serialised cleanly raised
  `TypeError` on the way back.  Present since the serialiser was written;
  found by the sweep rather than by use.

- **The three fault-injection scenario vocabularies are exported
  role-qualified.**  HTTP/1.1 client-side, HTTP/1.1 server-side and HTTP/2
  server-side share step names because they describe the same shapes of
  misbehaviour, and a bare `SendRawBytes` from the package resolved to
  HTTP/2's — so handing one to an `H1FaultServer` raised *unknown scenario
  step*.  The package now exports `H1S…` for the HTTP/1.1 server half and
  keeps `H2…` for HTTP/2; the submodules still offer the unprefixed names.
  Four further asymmetries went with it: the close step is `CloseGracefully`
  on both halves (it was `CloseConnection` on one), `ScenarioH1Server` gains
  the JSON round-trip `ScenarioH2` already had, `H2FaultServer` exposes the
  public `host` / `port` that `H1FaultServer` does, and both catalogues are
  reachable the same way (`CATALOGUE_H1` / `CATALOGUE_H2`, with `CATALOGUE`
  unchanged).  `ScenarioH1Server.steps` is a tuple, as `ScenarioH2.steps`
  always was.

- **The two fault-injection examples are one.**
  `examples/scenario_h1_fault_injection.py` and
  `examples/scenario_h2_fault_injection.py` are replaced by
  `examples/fault_injection.py`, organised by grid cell: a broken client
  against a real server (A), a broken server against real clients (B — all
  nine HTTP/1.1 cases driven **twice**, once with BlackBull's client and once
  with `httpx`), a broken HTTP/2 server against `httpx` (C), what is not
  implemented and why (D), and the same scenarios as replayable JSON (E).
  Both old files' content is carried over.  One file per cell would have meant
  four, and the next cell five.

- **The fault-injection documentation states its reach as a grid**, not a
  count.  Which cell you need depends on which side you are testing, and the
  page now says where the toolkit stops: gRPC has no fault injection of its
  own (it gets transport-layer misbehaviour because it rides HTTP/2, but
  gRPC-specific faults cannot be expressed), MQTT has none and is not planned
  to, WebSocket has none in either direction.

  Three claims elsewhere were corrected to match.  `translation-hub.md` — a
  page about MQTT ⇄ gRPC ⇄ REST — said every protocol conversation is
  observable with the same event API *and fault-injection tooling*; the first
  half is true on every protocol and the second is not.  `why-blackbull.md`
  offered "differential oracles against a reference implementation" inside an
  HTTP/2 paragraph while the oracle is HTTP/1.1-only.  `README.md` described
  the catalogue as HTTP/2's alone.

### Fixed

- **The HTTP/1.1 client's JSON codec dropped the scenario name.**  It had
  no `HEADER` line, the convention the other three vocabularies share, so
  a named catalogue case came back anonymous from a round trip.  Files
  written without one still parse.

- **An HTTP/2 client scenario now owns the wire**, via a required
  `HTTP2Client(..., scenario_mode=True)`.  `HTTP2Client` sends the connection
  preface and a SETTINGS frame as soon as it connects, so a scenario's
  `SendPreface` was the *second* preface on the wire and every byte after it
  was unparseable to a strict peer — the fault never reached the code meant
  to judge it.  `scenario_mode` makes `_start` the no-op its HTTP/1.1 twin
  already is (HTTP/1.1 has nothing to send on connect, so that half was never
  affected), and also removes a latent race in which `ReadResponse` and the
  receive loop both read the same socket.

  `execute_scenario` validates ownership before writing a byte, so the
  collision now raises instead of going out as garbage.

  Found by pointing the cell at something that is not BlackBull: a reference
  server built on `h2`, cross-checked against nginx.  `settings_ack_with_payload`
  had been reporting success against both; delivered intact it draws
  `GOAWAY(FRAME_SIZE_ERROR)` from each.

## [0.77.2] — 2026-08-19

### Fixed

Seven defects in the bundled clients (`blackbull.client`), each verified
present before the fix.  The attack-surface map covers BlackBull as the
*listening* party; these are the connecting direction, where the peer is the
server and a misbehaving one turns into a client-side failure worse than the
disconnect itself.

- **HTTP/2 GOAWAY failed streams the server said it had processed.**
  `last_stream_id` was read from the frame's own stream identifier, which
  RFC 9113 §6.8 fixes at 0; the field is the first four payload bytes.  Every
  pending stream therefore compared as unprocessed, so a *graceful* shutdown
  failed responses the peer had just promised to complete — the opposite of
  what GOAWAY communicates.
- **DATA for an unknown HTTP/2 stream leaked the connection window.**  The
  frame was dropped without returning flow-control credit.  The connection
  window is shared by every stream (§6.9), so each such frame shrank it
  permanently, and at zero every response body stalls in the peer's writer.
  A stream that closes while its DATA is in flight is ordinary, not hostile.
- **`HTTP2Client.request()` hung forever after the peer disconnected.**  Only
  a GOAWAY marked the connection dead, so a peer that simply vanished left the
  next request awaiting a future with no remaining resolver.  `HTTP1Client`
  raises `ConnectionError` here; the two clients no longer disagree.
- **A failed send leaked its pending future.**  The entry stayed in the
  response map until GOAWAY or `__aexit__`, growing once per failure.
- **A half-delivered HTTP/2 frame parked the connection.**  Waiting for a
  frame to *begin* is still unbounded — server streaming and long-polling both
  require it — but once nine header bytes declare a payload length, the
  remainder is bounded (30 s) and a peer that stops mid-frame is treated as
  gone.  The client-side twin of the server's `BB_HEADER_TIMEOUT`.
- **An HTTP/1.1 response could declare two different lengths.**  Only the
  first `Content-Length` was read.  Repeated and comma-combined values are now
  collapsed and a conflict refused, matching the rule the server already
  applies to requests — believing the wrong one makes the surplus octets the
  next keep-alive response's status line.
- **`stream()` did not stream a `Content-Length` body.**  One `readexactly`
  returned the whole body as a single chunk, so the memory bound streaming
  exists to provide was absent on exactly the path that asked for it.  It now
  yields in slices, as the chunked path always did.

## [0.77.1] — 2026-08-18

### Added

- **`BB_WS_IDLE_TIMEOUT`** (default `300.0`) and **`BB_WS_PONG_TIMEOUT`**
  (default `30.0`) — a WebSocket connection now has a bound on how long a
  silent peer may hold it.  HTTP/1.1 closes an idle keep-alive connection and
  HTTP/2 probes a silent peer with a PING; WebSocket did neither, so a peer
  that completed the handshake and then said nothing held its connection, its
  actor task and its buffers for the life of the process.  After the idle
  bound the server sends a PING (RFC 6455 §5.5.2); if nothing arrives within
  the pong bound it closes with **1001 (Going Away)** — nothing was violated,
  so the code says so.  **Any inbound frame counts as the answer**, not only a
  PONG: a peer that is talking to us is demonstrably alive, and requiring the
  specific reply would close a connection that is merely busy.  Both defaults
  match `BB_H2_IDLE_TIMEOUT` / `BB_H2_PING_TIMEOUT` because the question is
  the same one — an idle connection is legitimate on both protocols, so
  neither can reap on idleness alone.  `0` disables the probe.

  **Server-side only.**  The bundled clients under `blackbull.client` share
  the same read implementation but do not probe: the bound exists to stop an
  untrusted peer holding a *server* connection, which is not a question a
  client has about a server it chose to connect to.

  The receive path pays one integer increment for this, not a clock read —
  the arrival counter is turned into a time once per idle connection per
  scanner tick, in the callback that was already running.

## [0.77.0] — 2026-08-18

### Added

- **`BB_BODY_CHUNK_MAX`** (default `524288`) — per-read bound for a
  `Content-Length` request body.  Reads are up-to-n and transport-paced: each
  returns whatever the peer has delivered so far, up to this cap, instead of
  waiting to fill a fixed slice.  A slow peer yields small slices (no read is
  ever a latency commitment `BB_BODY_TIMEOUT` might not deliver); a fast one
  earns fewer, larger ones.  `BB_BODY_CHUNK_SIZE` now applies only to the
  chunked-transfer path.
- **`BB_MAX_BODY_SIZE`** (default `31457280`, 30 MiB) — total request-body
  ceiling, enforced on HTTP/1.1 and HTTP/2 alike.  A declared `Content-Length`
  over the cap is refused at head time, before a body octet is read; an
  undeclared or chunked body is refused the moment the running total passes it.
  HTTP/1.1 answers **413 Content Too Large** and closes the connection (the
  refused octets are still arriving, so keep-alive would parse them as the next
  request); HTTP/2 answers 413 + `RST_STREAM(NO_ERROR)` before dispatch, or
  `RST_STREAM(ENHANCE_YOUR_CALM)` mid-stream, and keeps the connection.  The
  30 MiB default is the same class as Kestrel's `MaxRequestBodySize`
  (30,000,000 bytes = 28.6 MiB); `0` disables the cap.
  **This is a behaviour change**: a request body over 30 MiB is now rejected by
  the server unless the cap is raised.
- **`BB_MIN_BODY_RATE`** (default `240.0` B/s) and **`BB_MIN_BODY_RATE_GRACE`**
  (default `5.0` s) — minimum sustained body-delivery rate, the anti-trickle
  defence transport-paced reads made necessary: an up-to-n read returns on any
  arrival, so `BB_BODY_TIMEOUT` alone degrades to "send *something* every 30 s",
  which a one-byte drip satisfies indefinitely.  Below the rate, past the grace
  period, the request is abandoned.  Matches Kestrel's
  `MinRequestBodyDataRate`; `0` disables.  What the rate is measured against
  differs by protocol on purpose — HTTP/1.1 counts only time spent waiting on
  the transport (so a slow handler is never mistaken for a slow peer), HTTP/2
  counts wall clock but exempts a peer our own closed inbound window
  back-pressured.
- **`BB_WS_MAX_MESSAGE_SIZE`** (default `16777216`, 16 MiB) — bounds a
  WebSocket message *as the application receives it*: after fragment
  reassembly and after `permessage-deflate` inflation.  `BB_WS_MAX_FRAME_PAYLOAD`
  bounds a frame on the wire and cannot express this — deflate ratios measured
  in this tree reach **1028.8:1**, so a 1 MiB frame inflates to roughly 1 GiB,
  and a fragmented message accumulates frames that are each individually legal.
  Over the bound closes with **1009 Message Too Big** (RFC 6455 §7.4.1) and
  logs a `ws_max_message_size` cap hit.  `0` disables.  The default admits the
  largest message the Autobahn suite sends, so conformance passes unconfigured;
  applications that do not serve huge messages should lower it.
  **This is a behaviour change**: a WebSocket message over 16 MiB now closes
  the connection unless the bound is raised.
- **MQTT resource bounds** — the broker now answers the three questions every
  other protocol here already answered, and **advertises** each answer in
  CONNACK where MQTT 5 has a property for it, so a conforming client never
  meets the enforcement path:
    - **`BB_MQTT_MAX_PACKET_SIZE`** (default `1048576`, 1 MiB), advertised as
      `Maximum Packet Size` (§3.2.2.3.6).  Judged from the declared Remaining
      Length as soon as the fixed header is readable — MQTT 5 permits a peer to
      declare 268,435,455 bytes and dribble them, so the payload is refused
      unread.  Over the cap: `DISCONNECT` **0x95 Packet Too Large**, connection
      closed.
    - **`BB_MQTT_RECEIVE_MAXIMUM`** (default `64`), advertised as
      `Receive Maximum` (§3.2.2.3.3).
    - **`BB_MQTT_MAX_QUEUED_MESSAGES`** (default `1000`) — per-session bound on
      QoS>0 messages held while the *client's* Receive Maximum window is full.
      The client's `receive_maximum` was decoded but never honoured, so a
      subscriber that never acknowledged made the broker hold every matching
      message for the life of its session.  At the bound the newest message is
      refused and the oldest kept.
    - **`BB_MQTT_MAX_RETAINED`** (default `10000`) — retained messages are
      permanent by design, so the store needed a total.  At the cap a retained
      publish to a *new* topic is refused; updating and deleting an
      already-retained topic always work.
  **This is a behaviour change** for a broker exposed to peers that send
  packets over 1 MiB, retain more than 10,000 topics, or rely on an unbounded
  offline backlog.
- **HTTP/2 now has a time axis.**  `HTTP2Actor` previously held no deadline of
  any kind: a peer could open a header block and dribble CONTINUATION forever,
  complete the preface and go silent, or request a large response and never
  open its flow-control window — each holding a connection, its actor task and
  its buffers indefinitely, at no cost to itself.  Three bounds, reusing
  HTTP/1.1's knobs rather than inventing HTTP/2 vocabulary:
    - **`BB_HEADER_TIMEOUT`** now also bounds an HTTP/2 header block opened
      without END_HEADERS.  Answered with `GOAWAY(ENHANCE_YOUR_CALM)` rather
      than a stream reset, because HPACK state is connection-wide: a block
      whose bytes never arrived leaves the decoder unable to read any later one.
    - **`BB_WRITE_TIMEOUT`** now also bounds waiting for `WINDOW_UPDATE` (the
      *data dribble* shape of CVE-2019-9511).  The stream gives up with
      `RST_STREAM(CANCEL)` instead of parking its task forever.
    - **`BB_H2_IDLE_TIMEOUT`** (default `300.0`) and **`BB_H2_PING_TIMEOUT`**
      (default `30.0`) — a silent connection is *probed* with a PING, not
      reaped.  Idle HTTP/2 connections are normal (a browser holds one across a
      page's lifetime; a gRPC channel idles between calls), so a peer that
      answers is never closed and any inbound frame counts as an answer.  A
      peer that does not answer gets `GOAWAY(NO_ERROR)`.  `0` disables probing.
  h2spec: 146 tests, 145 passed, 1 skipped, 0 failed — no bound fires during a
  conformance case.
- **`BB_FRAME_RATE_LIMIT`** (default `20`) and **`BB_FRAME_RATE_WINDOW`**
  (default `1.0`) — a per-type, per-connection budget for control frames that
  are cheap to send and oblige the server to work.  BlackBull metered exactly
  one frame type (inbound `RST_STREAM`, from the Rapid Reset work); four more
  shapes had no meter at all:
    - HTTP/2 `PING` flood (CVE-2019-9512) — one ACK write per frame;
    - HTTP/2 `SETTINGS` flood (CVE-2019-9515) — one ACK write per frame;
    - zero-length `CONTINUATION` / `DATA` (CVE-2019-9518's shape) — a parse and
      a loop turn for **no bytes at all**, so `BB_HEADER_MAX_TOTAL`, which
      counts bytes, never saw them;
    - WebSocket control-frame flood — one PONG write per PING.
  Each type gets its own budget, so a peer may legitimately spend its PING
  allowance *and* its SETTINGS allowance.  Over the budget:
  `GOAWAY(ENHANCE_YOUR_CALM)` on HTTP/2, close `1008` on WebSocket, plus a
  `frame_rate` cap hit.  `0` disables metering.  h2spec re-run with the meters
  live: 146 tests, 145 passed, 1 skipped, 0 failed.
  The Rapid Reset budget was a class constant (`HTTP2Actor._RST_RATE_LIMIT`,
  20/s); it is now this knob, at the same default.
- **`BB_MQTT_MAX_SUBSCRIPTIONS`** (default `1000`) and
  **`BB_MQTT_MAX_SESSIONS`** (default `10000`) — the unit and total bounds on
  MQTT session state, which had neither.  A session's Topic Filter list was
  appended to without limit, so one connected client could grow broker memory
  (and, with it, the per-PUBLISH routing walk) without opening a second
  connection; and the session table itself had no cap, which matters because
  §3.1.2.11.2 defines a Session Expiry Interval of `0xFFFFFFFF` as *does not
  expire* — a peer cycling Client Identifiers could pin one entry per
  identifier while breaking no rule.  At the subscription cap a **new** filter
  is refused with `0x97 (Quota Exceeded)` in the SUBACK (§3.9.3) while
  re-subscribing to one the session already holds still works, since §3.8.4
  makes that a replacement; at the session cap a CONNECT for an **unknown**
  Client Identifier is refused with `0x97` in the CONNACK and the connection
  closed, while a client resuming a session already in the table is admitted,
  because refusing it frees nothing.  Both log to `blackbull.caps`; `0`
  disables either.

### Security

An audit of the paths a peer can make the server allocate on found nine gaps.
Re-reading that audit afterwards found two more, in rows it had already written
down — so eleven are closed here, and the count is the point: **this is a pass
over the surface, not a finished job.** They are listed plainly rather than
folded into the feature notes above: BlackBull has no known production adopters
and none of these was reported from the field, but a security page that appears
quietly after silent fixes is worth less than the fixes.

The organising defect was one shape repeated — **a cap on one unit standing in
for a cap on the total**. The frame was bounded and the message was not; the
packet was bounded and the session state was not. Ten of the eleven are that
shape. The eleventh is the one worth remembering, because it needed a different
question: the HTTP/2 priority tree had no total because nothing ever *read*
what it stored, so it was never counted as storage at all. **A write with no
reader is still a growable path.**

| What was unbounded | Reachable by | Now |
|---|---|---|
| WebSocket message after `permessage-deflate` inflation | one compressed frame; ratios measured at **1028.8:1** in this codebase, so a 1 MiB frame inflated to ~1 GiB | `BB_WS_MAX_MESSAGE_SIZE`, enforced by zlib's own `max_length` so the payload is never built |
| WebSocket message across fragments | N continuation frames, each individually legal | same knob, checked before each append |
| MQTT packet | declaring a Remaining Length up to the 256 MiB spec ceiling and dribbling it | `BB_MQTT_MAX_PACKET_SIZE`, judged from the header before buffering |
| MQTT session backlog | subscribing and never acknowledging | client's `Receive Maximum` honoured; excess bounded by `BB_MQTT_MAX_QUEUED_MESSAGES` |
| MQTT retained store | one retained PUBLISH per distinct topic, forever | `BB_MQTT_MAX_RETAINED` |
| HTTP/2 connection time — no deadline of any kind existed in the actor | opening a header block and dribbling CONTINUATION; going silent after the preface; never opening the flow-control window | `BB_HEADER_TIMEOUT`, `BB_H2_IDLE_TIMEOUT` + `BB_H2_PING_TIMEOUT`, `BB_WRITE_TIMEOUT` |
| HTTP/2 PING / SETTINGS / zero-length frame floods | one ACK write or one loop turn per frame, at no byte cost (CVE-2019-9512 / -9515 / -9518 shapes) | `BB_FRAME_RATE_LIMIT` per type |
| WebSocket control-frame flood | one PONG write per PING | same meter |
| Rapid Reset counter's blind spot | provoking *server-emitted* resets rather than sending them | emitted resets counted in the same window |
| MQTT session state — subscriptions per session, sessions per broker, and an expiry that was recorded but never enforced | one CONNECT per Client Identifier at `session_expiry_interval = 0xFFFFFFFF`, which §3.1.2.11.2 defines as *never expires*; or one client subscribing to endless filters | `BB_MQTT_MAX_SUBSCRIPTIONS`, `BB_MQTT_MAX_SESSIONS`, and a one-shot expiry sweep |
| HTTP/2 priority-tree growth | PRIORITY for arbitrary idle stream ids — legal under §6.3, unmetered, and measured at 10,000 frames → 10,000 nodes that nothing removed | the state was never read, so it is no longer recorded at all; PRIORITY_UPDATE hints bounded by `SETTINGS_MAX_CONCURRENT_STREAMS` |

Also in this release: the request-body total cap and minimum delivery rate
(`BB_MAX_BODY_SIZE`, `BB_MIN_BODY_RATE`), and a finite default connection cap
(`BB_MAX_CONNECTIONS`) — both described under *Added* and *Changed*.

**Two defaults are worth setting, stated because a default-open knob is only
useful if you know it is open**: `BB_REQUEST_TIMEOUT` is `0`, so a peer
delivering at exactly the minimum rate up to the body cap legally holds a
connection for ≈36.4 hours; and the derived connection cap bounds descriptor
exhaustion, not event-loop health, so set an explicit number. Both are
documented in
[the security model](https://github.com/TOKUJI/BlackBull/blob/master/docs/about/security-model.md),
which also states what this project does **not** claim — no third-party audit,
no red-team exercise, no volumetric-DoS protection.

### Changed

- **`blackbull.__all__` now states the public surface, and two names that
  leaked into it are gone.**  The package ships `py.typed`, and for an
  inline-typed package the typing spec treats `from .x import Y` in
  `__init__.py` as a *private* import unless it is re-exported — so the 37
  names this package intends you to import were not formally part of its typed
  contract.  They are now.

  Two of the names previously reachable were accidents of implementation, and
  have been bound privately:

  - `blackbull.logging` resolved to the **standard library** `logging` module —
    an artifact of `import logging` at the top of `__init__.py`, and confusing
    next to the real `blackbull.logger`.
  - `blackbull.PackageNotFoundError` was `importlib.metadata`'s exception,
    imported only to detect a source checkout.

  Neither was documented and neither is used anywhere in the tree, so this
  should be invisible; if you were importing either from `blackbull`, take them
  from `logging` and `importlib.metadata` directly.  Submodules
  (`blackbull.server`, `blackbull.middleware`, …) are deliberately *not* in
  `__all__` and are unaffected — import them by path as before.  `Request`, the
  deprecated `Connection` alias, is also excluded so `import *` no longer risks
  a `DeprecationWarning` for code that never asked for it.

- **Internal `DEBUG` logging is now decided at import, not per call.**  A
  `logger.debug(...)` that emits nothing is not free — the call happens, its
  arguments are built, and the level is checked — and the framework was making
  twenty of them per HTTP/2 request and three per HTTP/1.1 request, measured at
  4.5 % and 1.7 % of those lanes.  The per-request modules (request dispatch,
  HTTP/2 frame parsing, the response senders) now read the level once at import
  and branch on the result.

  **What this changes for you**: raising the log level to `DEBUG` *after*
  importing `blackbull` no longer switches on those internal traces.
  Configure it first:

  ```python
  import logging
  logging.basicConfig(level=logging.DEBUG)   # before the import below
  from blackbull import BlackBull
  ```

  This is the bargain the `@log` decorator has always made, and the caveat in
  `docs/guide/logging.md` now covers internal debug logging generally rather
  than only `@log`.  `WARNING`/`ERROR` and the access log are unaffected and
  are still checked per call.  The paths this gates emit around twenty lines
  per request, so they are a development setting rather than something to
  enable on a running server.

- **`BB_MAX_CONNECTIONS` now defaults to a finite, derived value** instead of
  `0` (uncapped).  It accepts `auto` (the new default), `0`, or a number.
  `auto` derives the cap from the process's own `RLIMIT_NOFILE`, less a
  64-descriptor reserve for listeners, the event loop's selector, log files and
  the application's own descriptors.
  The value is derived rather than picked because a cap above the fd budget is
  decorative: `accept()` fails with `EMFILE` before the cap is consulted, and
  the peer gets a dropped connection instead of the `503` + `Retry-After` the
  mechanism exists to send.  Derived, it can only refuse connections the OS was
  going to refuse anyway — which is what makes a finite default safe to ship —
  and it follows the operator's own intent, since raising the fd limit is how
  you say how large a process may become.  The resolved value is logged at
  startup, because a derived default nobody can see is a default nobody can
  size.
  An explicit number is still honoured as given and is *not* clamped to the fd
  budget.  **This bounds descriptor exhaustion, not event-loop health**: a
  ceiling reflecting what one asyncio loop serves well is a policy number that
  depends on the workload, so set it explicitly (1024 is a typical single-loop
  value).
- **Transport-paced `Content-Length` body delivery** — the body-read slice is
  no longer a fixed `readexactly(chunk_size)` per `http.request` event; each
  read returns whatever the transport has delivered, up to `BB_BODY_CHUNK_MAX`.
  The transport offer follows the reader's pending read demand (capped at the
  backpressure high-water mark), so a fast peer's body arrives in large
  chunks instead of floor-sized ones.  The exact-bytes contract is unchanged:
  EOF before the declared length is still a truncated upload, never a complete
  one.
- **Receive-path responsibility separation** — the receive competence moves
  onto `BufferReader`, the only object that knows both what was asked for and
  what was consumed.  It now owns the stop-reading decision, the backpressure
  release, and the grown-buffer release hysteresis; `ConnectionProtocol` keeps
  the transport callbacks, the rendezvous, and executing `pause_reading` /
  `resume_reading`; `ReadBuffer` keeps bytes, scanning, and growth, reporting a
  drained message boundary instead of acting on one.  Internal only — no public
  API, environment variable, or wire behaviour changes.
  - Side effect of deleting the old inference: a reader that is already parked
    now arms **no** backpressure pause at all.  The transport front end used to
    guess "is anybody waiting" from the rendezvous future, which clears when a
    reader is *woken*, so arrivals in that window cost a
    `pause_reading`/`resume_reading` pair that the next park undid.
  - The reader's transport offer is now *published* (`ConnectionProtocol.read_offer`)
    rather than polled through a method call — the mirror of `reading_paused`
    going the other way.  `get_buffer` runs on every arrival on every
    connection, including the ones that never read a body, so the decision is
    kept out of that path on principle rather than measured into it — an EC2
    A/B on `/conn` puts the change at +0.13 % (95 % CI [−0.13, +0.39]), i.e.
    no throughput claim either way.  Ownership is unchanged: the party that
    decides is the party that writes.
- **Server-emitted `RST_STREAM` frames now count toward the Rapid Reset
  budget.**  The meter watched inbound resets only, so a peer could get the
  same stream-slot churn for free by *provoking* ours — protocol violations,
  window overruns, and (new in this release) the body-size and body-rate
  refusals are all reachable on demand.  A stream reset is a stream reset
  whoever sent it.  **Deliberate consequence**: a client that repeatedly trips
  a legitimate limit — an upload loop over `BB_MAX_BODY_SIZE`, say —
  eventually loses its connection.  That is the correct outcome for a client
  behaving abusively even unintentionally; the cap-hit log names which limit it
  kept tripping so an operator can tell the two apart.
- **MQTT is visible in `blackbull.caps`.**  The broker previously had no
  `log_cap_hit` call anywhere, so nothing it refused reached the operational
  channel every other protocol reports through.  Each of the new limits emits
  one, with the topic in `scope_path`.
- **MQTT framer resync is linear, not quadratic.**  A desynchronised stream
  used to drop one byte and re-decode from the start, so a junk run cost a
  decode attempt per byte — and the length of a junk run is the peer's choice.
  It now skips to the next byte that could plausibly begin a packet (a
  control-packet type of `0` is reserved, §2.1.2) before asking the decoder
  again.
- **WebSocket cap-hit records now carry the request path.**  The WebSocket
  actor passes its `Connection` to the recipient, so `ws_max_frame_payload` and
  `ws_max_message_size` report with `scope_path` set instead of `None` — the
  one field an operator needs to act on the record.
- **A refused body ends the HTTP/1.1 connection.**  `HTTP1Recipient.must_close`
  makes explicit what a desynced chunked stream already implied, and extends it
  to a body refused for size: the actor breaks the keep-alive loop instead of
  reading the next request out of octets the peer chose.
- **The cost of the new limits, measured and then worked down.**  The close A/B
  for this programme found a regression against `v0.76.1`: HTTP/1.1 `/conn`
  −1.98 % and HTTP/2 `/1kb` −3.75 % (EC2 m7a.2xlarge, 8 rounds ABBA with a
  passing A/A null).  Paying the limits once per connection instead of once per
  request roughly halved it — to −1.06 % and −1.85 % — and that is the last
  figure confirmed on EC2.

  Attribution then moved to counting *executed instructions* per request, which
  is deterministic where this box's timing is not.  Its most useful finding:
  about **40 % of the added cost was not the limits at all** but the receive
  path's ownership split, which shipped in the same window.

  Four further changes brought the instruction cost against `v0.76.1` from
  +2.23 % to +0.19 % on `/conn` and from +1.85 % to +0.29 % on HTTP/2 — the
  largest of them being the DEBUG-logging gate below, which was never about
  the limits.  A second EC2 A/B (20 rounds, targeting ±0.5 % equivalence)
  confirmed both lanes are **bounded within ±1 %** of `v0.76.1`, which is the
  bound the original regression was measured against, but did not reach the
  stricter ±0.5 % target: HTTP/2 `/1kb` keeps a real, CI-confirmed residual of
  **−0.35 % to −0.42 %** (91–89 % of the original −3.75 % recovered);
  `/conn` did not resolve either way — its confidence interval cannot rule out
  zero or a cost approaching −1 % at every trim level but one, though a
  regression larger than 1 % is excluded.  Neither is release-blocking on this
  evidence; further reduction remains an open, non-blocking candidate.

  The instruction count under-predicted both lanes (by 1.2–1.5× on HTTP/2 and
  1.6–2.4× on `/conn`) — it counts Python bytecode, not the C-level work,
  syscalls, allocation and GC underneath it.  Treat any "N % of the lane"
  figure derived from it as a **lower bound**, not an estimate.

  What the limits themselves now cost, and what was taken back:
  - the declared-body check reads the length `_validate_message_framing`
    already validated, instead of asking the header store again — a request
    with no `Content-Length` was paying an index miss plus the `bytes.lower()`
    allocation of the fallback probe;
  - `HTTP2Recipient` and `HTTP2Sender` are handed their limits by the
    connection's actor rather than resolving settings themselves.  One of each
    is built per stream, so a function-level import was being resolved through
    `importlib._bootstrap` on every request;
  - the HTTP/2 declared-body refusal is a comparison at the call site, so the
    common answer — no — costs neither a coroutine nor a method call per
    stream;
  - the HTTP/1.1 actor asks the recipient one question after dispatch
    (`after_dispatch`) rather than combining two predicates itself.

  Behaviour is unchanged in every case.  `BB_H2_IDLE_TIMEOUT`'s per-frame clock
  read was left alone deliberately, because it is what makes that bound mean
  the period it states; the frame-rate meters and the body-cap state were
  likewise left, because they are the checks rather than the way they are
  written.

### Fixed

- **A PRIORITY flood no longer grows HTTP/2 server state.**  RFC 9113 §6.3
  permits PRIORITY for a stream in any state, including idle, so a peer could
  send it for arbitrary stream identifiers; each one created a priority-tree
  node, and `Stream.remove_child` had no caller anywhere in the tree.  Measured
  before the fix: 10,000 PRIORITY frames left 10,000 nodes, no GOAWAY, the
  connection still open — a few bytes on the wire for a node that outlived
  them.  §5.3 deprecated that
  prioritisation scheme and BlackBull does not implement it (`Stream.weight` and
  `.parent` were written by the responder and read by nothing), so the fix is to
  stop recording it rather than to cap it: the frame is validated and the signal
  dropped.  The exclusive-dependency branch went with it, which had walked every
  child of root to rewrite that same unread field — quadratic on top of
  unbounded.  §5.3.1's self-dependency check is unchanged and h2spec is
  unchanged at 146 tests, 145 passed, 1 skipped, 0 failed.  RFC 9218
  `PRIORITY_UPDATE` still pre-creates a node so a hint arriving before HEADERS
  survives to meet it, now bounded by `SETTINGS_MAX_CONCURRENT_STREAMS` as §7
  permits, with `h2_priority_update_buffer` logged over the bound.  `Priority`
  frame construction also logged one `INFO` record per frame on the grounds that
  "PRIORITY frames are rare" — true only of well-behaved peers; it is now DEBUG
  behind the module gate.
- **MQTT Session Expiry Interval is now enforced.**  `_expiry` was recorded on
  CONNECT and read in exactly one place — a `<= 0` test at detach — so every
  session that declared a non-zero interval was retained for the life of the
  process.  Sessions with a finite interval are now removed when it elapses,
  driven by a single one-shot timer armed at the earliest pending deadline, so
  a broker with nothing pending holds no timer at all.  Three consequences of
  the same defect are fixed with it: a reconnect took `max(old, new)` of the
  intervals, so one CONNECT at `0xFFFFFFFF` pinned the session permanently and
  the client could never shorten it (§3.1.2.11.2 makes the new value the
  session's value); a reconnect to an elapsed session answered
  `session_present=True` and replayed its unacknowledged messages, resurrecting
  deliveries the client had already accounted for; and `DISCONNECT` carried a
  Session Expiry Interval (§3.14.2.2.2) that never reached the broker — it is
  now honoured when it shortens the interval and refused when it would raise a
  zero one, which that section makes a Protocol Error.

## [0.76.2] — 2026-08-17

Client-side patch.  Reported as
[#241](https://github.com/TOKUJI/BlackBull/issues/241) against
`WebSocketClient`; the same defect was present in every client.

### Fixed

- **Connection establishment is bounded on all five clients** — `Client`,
  `HTTP1Client`, `HTTP2Client`, `WebSocketClient`, and `WebSocketH2Client`
  opened their transport with a bare `await asyncio.open_connection()`.  A peer
  that completes the TCP handshake and then goes silent left the coroutine
  pending with nothing to end it: TLS negotiation has no kernel-side deadline,
  so `async with SomeClient(...)` could hang for the lifetime of the process.
  All five now accept **`connect_timeout=`** (default `30.0` s, matching the
  server's `BB_BODY_TIMEOUT` default) and raise `TimeoutError` when it expires.
  **This is a behaviour change**: a connect that legitimately takes longer than
  30 s now fails.  Pass `connect_timeout=None` to restore the unbounded wait and
  impose your own deadline.
  `HTTP1Client` already carried the parameter but defaulted it to `None`, which
  left every caller who never passed it exactly as exposed as the others.
- **`WebSocketClient.connect()` bounds the handshake read** — a peer can accept
  the connection and then never send the 101, which no transport-level deadline
  covers.  New **`response_timeout=`** (default `5.0` s) matches what
  `WebSocketH2Client.connect()` already enforced for RFC 8441 Extended CONNECT.
- **`docs/guide/testing.md` WebSocket example corrected** — it awaited
  `connect()` as a context manager and called `send`/`recv`, none of which
  exist on the session.  The example now mirrors the passing test in
  `tests/conformance/http1/test_client.py`.

## [0.76.1] — 2026-08-14

Emergency correction.  v0.76.0 released the Sprint 102 upload body-read work
out of scope — its design is under re-examination, so it should not have
shipped.  This patch restores the v0.75.1 receive path.

### Removed

- **Adaptive request-body read sizing and `BB_BODY_CHUNK_MAX`** — withdrawn
  pending the design re-examination.  The `Content-Length` body read is back
  to the v0.75.1 fixed-slice behaviour.
- **HttpArena crud_create contract pin** — the test that accompanied that
  work is withdrawn with it.

### Fixed

- v0.76.0's out-of-scope upload release corrected; the receive path matches
  v0.75.1 byte-for-byte.

## [0.76.0] — 2026-08-14

### Added

- **`conn.disconnected`** — a named accessor for mid-request disconnect state,
  previously readable only as a private field or through the module-level
  `disconnected()` helper.  Useful in a long-running handler whose result
  nobody is waiting for any more.  The module-level `disconnected()` remains
  the form for the two ASGI boundaries, where the same state may live on a
  `scope` dict.

- **Adaptive request-body read sizing** (`BB_BODY_CHUNK_MAX`, default
  `524288`).  `BB_BODY_CHUNK_SIZE` is now the *starting* slice for a
  `Content-Length` body rather than a fixed one: while a peer keeps the
  transport running ahead of the server the slice doubles up to the new
  ceiling, and two consecutive reads that drain the transport halve it again,
  never below the starting size.  Fewer, larger reads for a fast uploader;
  unchanged reads for a slow one, which is what the ceiling is for — slices
  stay exact-size, so an unbounded ramp would eventually promise more than
  `BB_BODY_TIMEOUT` allows.

  The grow-on-evidence / two-quiet-reads-before-backing-off / hard-ceiling
  rule is [Netty][netty-adaptive]'s `AdaptiveRecvByteBufAllocator`, adapted to
  an exact-size reader.

  No behaviour change for applications: bodies still arrive as successive
  `http.request` events with `more_body`, a truncated body still raises, and
  only the number of events varies.  Set `BB_BODY_CHUNK_MAX` equal to
  `BB_BODY_CHUNK_SIZE` for the previous fixed-size slices.

  Measured on EC2 against v0.75.1 (m7a.8xlarge, 16 workers, 20-profile
  HttpArena sweep): upload/32 **+15.0 %**, upload/256 **−14.5 %** — the
  256-connection upload cell regresses and the mechanism is not yet
  attributed.  The body-read design is under re-examination; a follow-up
  release will revise it.

[netty-adaptive]: https://netty.io/4.1/api/io/netty/channel/AdaptiveRecvByteBufAllocator.html

### Changed

- **Deployment docs cover three reverse proxies, not one.**
  `deployment/behind-nginx.md` becomes
  `deployment/behind-reverse-proxy.md`, adding **HAProxy** and **Envoy**
  alongside nginx, each with HTTP/1.1 *and* HTTP/2 backend configuration, plus
  a guide to choosing between them.  The distinction that matters is whether a
  proxy can speak HTTP/2 to the backend — BlackBull does natively, and a proxy
  that downgrades to HTTP/1.1 on the back leg throws that away.  HAProxy
  (2019) and Envoy (2016) have shipped it for years; nginx's
  `proxy_http_version 2` arrived in 1.29.4.  `guide/http2.md` gains a
  cross-reference.

- **The actor→sender disconnect signal is a method, not an event.**  When a
  read proved the connection dead, the HTTP/1.1 actor told its sender by
  pushing an `http.disconnect` dict down the *send* channel — a receive-side
  ASGI event sent the wrong way through the pipe.  It is now
  `BaseSender.mark_client_gone()`.  The cost was never the dict: every
  sender's event union had to widen to admit a message no application or
  middleware may legally send, so the private `_SenderEvent` alias now
  collapses back to `ASGISendEvent`.

  `http.disconnect` on `receive()` — the direction ASGI defines it in — is
  unchanged.  Senders no longer honour it on the send channel, so an
  application or middleware holding `send` can no longer close its own
  connection by sending that dict; it is logged and dropped like any other
  unknown send event.

### Internal

- **Per-request closure annotations stripped** on the HTTP/2 and gRPC
  streaming hot paths (four sites each).  A nested `def` pays for its
  annotations on every creation; the types move to comments, saving ~250 ns
  per H/2 request stream, and an architecture test now guards the rule that
  per-request factories stay unannotated.

## [0.75.1] — 2026-08-13

Sprint 100.  A patch rather than a minor: the public surface is unchanged —
no new API, no new environment variable, nothing new to call or configure.
What shipped is a set of read-path fixes (the largest term named by the
per-request attribution run) and the measurement harness that named them.

### Fixed

- **Every HTTP/1.1 cleartext connection crashed under uvloop.**
  `buffer_updated` released the buffer's memoryview while uvloop still held
  its Py_buffer export (uvloop releases the export in a `finally` after the
  transport callback returns), raising `BufferError: memoryview has 1
  exported buffer` and killing the connection.  TLS was unaffected (a
  different transport path).  The tolerant call site fixes the crash; the
  strict call sites still fail loudly on a genuine leak.
- **Small keep-alive requests churned a 64 KiB allocation per request.**
  `BufferedProtocol.get_buffer`'s sizehint — which uvloop fills with
  libuv's fixed 64 KiB on every cleartext read — was treated as a demand,
  growing every connection's buffer to 64 KiB on its first request and
  shrinking it back at the message boundary: a 64 KiB alloc/free per
  request.  The sizehint is advisory (CPython's documented contract), so
  growth is now driven by the bytes actually arriving.  Measured on EC2:
  the read-path term of the B1 cleartext deficit dropped +4.24 → +0.16 µs/req.

### Internal

- **Empty head-scan skip.**  `read_head` no longer scans for the head
  terminator while the buffer is empty — an empty buffer can never exceed
  the scan's limit, so the scan was a control-flow artifact, not a
  conformance requirement.  Measured on EC2: empty scans 1.00 → 0.00/req.
- **`_release` hysteresis.**  A buffer grown for a large message now returns
  to its floor only after several fully-consumed small messages, so a
  keep-alive connection that repeats a large body reuses its allocation
  instead of growing and shrinking per message.
- **Benchmark attribution harness.**  The per-seam timing instruments, null
  seam, gate stamps, and EC2 driver used to attribute the read-path deficit
  land under `bench/` (harness only; nothing runs in a stock launch).
  Full Sprint 100 record: `.claude/sprint-logs/sprint-100.md`.

### Docs

- `docs/about/internals.md` §Read-path invariant — the sizehint is advisory
  and the buffer returns to its floor with hysteresis.

## [0.75.0] — 2026-08-11

Sprint 99.  The app boundary becomes one shared `RequestActor`.  What the
application is called with — the native `Connection`, or a materialised
ASGI scope on the `BB_FORCE_ASGI_SCOPE=1` compat lane — is now decided in a
single actor shared by HTTP/1.1 and HTTP/2, and the forty-three-release-old
`scope['http2_priority']` deprecation finally ships its removal.  The
separation cost on the H/1 native lane (≈0.4-0.5 %, below the A/B null
floor's spread) is accepted and recorded in `bench/results/`.

Versioned as a MINOR under the then-current rule (a public-API removal).
The versioning rule was tightened on 2026-08-11 to judge removals by
effective surface: a removal of an API deprecated long enough that adopters
have migrated is now a PATCH — under that rule this release would have been
`v0.74.1`.

### Removed

- **`scope['http2_priority']`** — deprecated in v0.31.0 with removal scheduled
  for v0.32.0, then shipped for another forty-three minor releases.  It is
  gone.  Read the RFC 9218 hint from
  `scope['extensions']['http.response.priority']` (or
  `conn.extensions[...]` natively), which is where it has lived since v0.31.0.

  The extension is not merely the newer spelling — it is the only one that was
  ever correct.  `extensions` is shared by reference with the `Connection`, so
  a `PRIORITY_UPDATE` arriving *after* dispatch reaches the application through
  it; the top-level key was a dispatch-time copy that silently went stale for
  the rest of the request.  Only the ASGI-compat lane
  (`BB_FORCE_ASGI_SCOPE=1`) ever carried it; native handlers never saw it.

### Internal

- **The app boundary moved into one shared `RequestActor`.**  What the
  application is called with — the native `Connection`, or a materialised
  ASGI scope on the `BB_FORCE_ASGI_SCOPE=1` lane — is now decided in a single
  actor shared by HTTP/1.1 and HTTP/2, instead of one per-protocol
  dispatch site.  No user-facing API change; the A/B measurement of the
  separation cost is recorded in `bench/results/`.
- **Access-log record construction unified.**  H/1's per-request record is
  built inline (master-equivalent); the record owners for H/2 and WS share
  the same helpers.
- **Bench tooling fixes:** `ab.sh` finish's pgrep self-match (50-min poll
  budget) fixed via the `[.]` bracket trick; `ab_commit_h2.sh` no longer
  recreates `.venv` on the EC2 instance; `BB_FORCE_ASGI_SCOPE` threaded
  through the ab-verify tooling.

---

## [0.74.0] — 2026-08-10

Sprint 98.  The perf-survey T1 remainder.  Two of the three items turned out
to be defect reports rather than new work: the CPU pinning this sprint was
meant to *add* has shipped since `v0.28.0`, unconditionally and undocumented,
and the `sendfile` path had quietly escaped the write timeout that guards
every other response body.

### Security

- **A large static file could be held open indefinitely.**  `BB_WRITE_TIMEOUT`
  bounds every response write except one: `AsyncioWriter.sendfile` issued a
  single unbounded `loop.sendfile` for the whole file, and flushed the response
  headers through the raw `drain()` rather than the guarded one.  A peer that
  requested a large file and then read it a byte per second held the
  connection, its file descriptor, and its transport for as long as it liked —
  the exact slow-read slowloris shape the write timeout exists to stop, on the
  one path it could not reach.  The transfer is now issued in 1 MiB chunks and
  each chunk re-arms the bound, so the policy is "make a megabyte of progress
  within the budget" rather than the unsettable "send this whole file within
  the budget".  A legitimately large transfer that keeps progressing is never
  cut off for its size, and files below the chunk still go out in exactly one
  call.

### Fixed

- **Worker CPU pinning overrode the operator's placement.**  Each worker pinned
  itself to `worker_id % os.cpu_count()`, computed against the machine's core
  count rather than the affinity mask the process actually held — and since a
  process may widen its own mask, `taskset -c 8-11 … --workers 4` put its
  workers on CPUs 0–3.  Placement is now drawn from `sched_getaffinity`, so
  `taskset`, `numactl`, and a container's cpuset are honoured.  On an
  unrestricted host the chosen cores are unchanged.
- **Worker CPU pinning confined the thread pool as well.**  Linux threads
  inherit the creating thread's affinity mask, so pinning the event loop also
  pinned every `run_in_executor` compression offload and every
  `asyncio.to_thread` static-file read to the single core the loop was already
  saturating — the opposite of what offloading is for.  Pool threads are now
  handed the full mask back.

### Added

- **`BB_CPU_PINNING`** — `auto` (default, the corrected behaviour above),
  `off` to leave placement alone entirely, or a `taskset`-style CPU list
  (`2,4,6-9`; `0` is CPU 0, not the off switch) intersected with the mask the
  process was granted.  Pinning had no off switch before this, which is the
  wrong default for a shared or externally-orchestrated host.

### Internal

- **A mistyped CPU range no longer allocates the range.**  `BB_CPU_PINNING`
  expanded each range before intersecting it with the process's mask, so
  `0-20000000` in place of `0-20` built 20 000 001 entries — 1.15 GiB and
  1.8 s, in every worker, at fork time — and then discarded all of it.  Ranges
  are clamped to the highest allowed CPU, which nothing above could have
  survived anyway.  Caught in review; the parser is new this sprint, so no
  release ever carried it.
- **The per-connection serve task starts eagerly.**  `connection_made` now
  builds the task with `eager_start=True`, running the serve prologue inline
  instead of queueing its first step for a later loop iteration.  Measured at
  **0.387 µs saved per accepted connection** (95% CI 0.343–0.431, five pooled
  ABBA runs against an A/A null of +0.059 ± 0.025; `bench/accept_hop_ab.py`).
  That is ~0.8 % of a churn request and ~0.008 % of a request on a
  100-request keep-alive connection — bookkeeping, not a latency win, since an
  accepted connection waits for the peer's first packet either way.

### Harness

- **The Autobahn image is pinned by digest.**  `autobahn_run.sh` pulled
  `crossbario/autobahn-testsuite:latest`, so "the wire behaviour regressed" and
  "the test suite changed" arrived as the same red X and no passing run was
  repeatable.  Override with `AUTOBAHN_IMAGE=` to test an upgrade.  The pinned
  digest is the one Docker Hub's `latest` resolved to on 2026-08-10, so what CI
  runs is unchanged.

---

## [0.73.1] — 2026-08-09

Sprint 97.  A patch rather than a minor: the public surface is byte-for-byte
unchanged — no new API, no new environment variable, nothing new to call or
configure.  What shipped is a security fix, a shutdown-signal fix, one new log
line, and a test that can now explain its own failures.

### Security

- **A peer-declared `chunk-size` chose how much the server buffered.**  A
  `Transfer-Encoding: chunked` chunk was read with a single
  `readexactly(chunk_size)`, and `chunk-size` is a number the *client* writes —
  up to ~8190 hex digits within the 8 KiB chunk-line bound.  Worse than an
  oversized allocation: a read above the backpressure high-water mark must
  reopen the transport the mark just paused, since it would otherwise be
  waiting for the bytes its own pause refuses to accept.  Declaring a huge
  chunk therefore switched backpressure *off* and buffered whatever the peer
  could push until the body timeout.  `drain()` was affected the same way — its
  `max_bytes` was consulted only after a whole chunk had been buffered, so it
  bounded the report rather than the allocation.

  Chunked bodies are now sliced by `BB_BODY_CHUNK_SIZE` (64 KiB, below the
  mark) exactly as `Content-Length` bodies already were, so no single read is
  larger than the slice whatever the peer declares.  No limit is imposed on the
  chunk itself: a large upload still arrives in full, just as several
  `http.request` events.  Pre-existing — not introduced by v0.73.0, though that
  release's deadlock fix is what turned an oversized read into a
  backpressure bypass.

### Fixed

- **A stop signal delivered while the master was still starting up was
  silently discarded.**  `MultiWorkerServer.run()` installed the SIGTERM /
  SIGINT handlers first and then reset the flag those handlers set, so any
  signal arriving during worker spawn — or, under `reload=True`, during the
  watcher thread's start — was overwritten before the supervision loop ever
  read it.  The master then kept supervising a shutdown it had already
  acknowledged in the log, until something SIGKILLed it: an orchestrator that
  SIGTERMs a slow-starting container waited out its full grace period, and
  `Ctrl-C` in the first moments of `--reload` did nothing.  The flag is now
  owned from construction and never re-initialised.  Measured on the reload
  end-to-end path: SIGTERM-to-exit went from a 15 s SIGKILL to 0.12 s.

### Added

- **`auto-reload: change detected in <paths>`** — the file watcher now logs the
  change it observed, before handing off to the master.  Previously the first
  evidence of a reload was the master's own "recycling workers" a tick later,
  which only appears if the master acted; a reload that never happened gave no
  way to tell a watcher that stayed silent from a master that ignored it.

### Docs

- **Hot reload** — documented the reload log sequence as a diagnostic ladder,
  and a caveat that was not previously written down: `watchfiles`' poller
  (forced on by default under WSL) treats a file as modified only when its
  mtime moves *forward*, so a backwards clock step — NTP correction, VM
  resume, WSL2 time resync — silently drops every save for the next few
  seconds.  Verified: 12/12 saves dropped with an mtime forced into the past,
  0/12 with the mtime left alone or forced forward.

---

## [0.73.0] — 2026-08-09

### Removed

- **`BB_H1_PROTOCOL` and its buffer-owning read front end.**  The flag shipped
  as an explicit measurement gate — "not a supported switch … will either
  become the default or be removed once that measurement lands" — and the
  measurement landed: it is **slower**, by 2.02 % ± 0.11 at a browser-like
  header count and 2.9–4.8 % at wrk's default (local ABBA paired by round,
  against a +0.00 % ± 0.14 A/A null floor).  The cause is structural rather
  than incidental: the reader was layered *over* `asyncio.StreamReader`, so it
  was a third buffer rather than a replacement, and its premise — "one loop
  turn per header line today" — does not hold, because `StreamReader.readuntil`
  only suspends when the separator is not already buffered.  Removed rather
  than left opt-in: an unmeasured, untested, slower duplicate of the header
  read is worse than either path alone.

### Fixed

- **A read larger than the backpressure high-water mark (128 KiB) deadlocked
  the connection.**  The buffer paused the transport once that many unconsumed
  bytes were resident, but a reader parked in `readexactly` was waiting for the
  very bytes the pause was refusing to read — so the connection hung with no
  error, no log, and no response until the peer gave up.  Reached by a
  WebSocket frame or a single `Transfer-Encoding: chunked` chunk above the
  mark, both sized by the *peer*; a `Content-Length` body was unaffected only
  because `BB_BODY_CHUNK_SIZE` slices it below the mark first.  Parking to wait
  now releases the pause, which is what `asyncio.StreamReader` does at the same
  point.  Introduced by this release's buffered read front end and caught by
  the Autobahn CI lane timing out.

- **Every HTTP/2 server push raised `AttributeError` under
  `BB_FORCE_ASGI_SCOPE=1`.**  `_handle_push` read the push parent's headers
  through a dual-shape ternary whose compat branch returned the scope's raw
  `list[tuple]`, then called `.get(b'host')` on it.  The parent is now always
  the native `Connection`, so the read is a plain attribute and the failure is
  gone by construction rather than by patch.  Native-lane pushes were never
  affected.

### Changed

- **HTTP/2 keeps native state end-to-end.**  `stream.conn` is a `Connection`
  on every lane, and the only place an ASGI scope comes into existence is the
  app boundary — the pattern HTTP/1.1 already used.  Nine dual-shape branches
  and the second conversion site are gone.  ⚠️ One delta, compat lane only: a
  `PRIORITY_UPDATE` arriving after dispatch still reaches the application
  through `scope['extensions']['http.response.priority']` (shared by
  reference), but no longer rewrites the deprecated top-level
  `scope['http2_priority']` alias, which is now a dispatch-time snapshot.

- ⚠️ **An over-budget header block with no line terminator in it is now
  answered `400`, not `431`.**  `BB_HEADER_MAX_TOTAL` can be overrun two ways
  and they are different violations: many well-formed field lines is "your
  header fields are too large" (RFC 6585 §5, still `431`), but 64 KiB with no
  CRLF anywhere is a start-line that never ended (RFC 9112 §3).  Telling that
  peer to retry with fewer header fields names a cause it does not have.
  Requests that draw `431` today are unaffected.

- **The head budget now applies to every request on a connection.**  It
  previously bounded only the first: subsequent keep-alive heads were read with
  an unbounded `readuntil`, backstopped in practice by `asyncio.StreamReader`'s
  own 64 KiB buffer limit, which the new front end does not have.  A keep-alive
  peer can no longer send a head of any size and escape the `431` the same
  bytes draw on its first request.

### Internal

- **The H/1.1 inbound path is one buffer and one cursor.**
  `ConnectionProtocol` (an `asyncio.BufferedProtocol`) has the kernel write
  straight into the connection's `ReadBuffer`; the server accepts through
  `loop.create_server` with a protocol factory instead of a `StreamReader`
  callback pair.  Three workarounds go with the second buffer that is no
  longer there: protocol detection consumes nothing (so the winning binding
  needs no replayed prefix), the message head is found in one resumable scan
  rather than a `readuntil` per line, and an upgrade hand-off to WebSocket or
  h2c carries nothing because the peer's surplus is already resident.

- **`read_head` is part of `AbstractReader`, not a capability callers sniff
  for.**  One call returns a head, `b''` for an idle close, or
  `IncompleteReadError` carrying the partial for a truncated one;
  `ReadLimitExceeded` reports a budget breach and carries the bytes so the
  protocol — not the reader — decides between `400` and `431`.  The keep-alive
  loop no longer has a second head-read path of its own: every request after
  the first re-enters the same read, differing only in which idle window
  applies.  `tests/unit/test_read_head_contract.py` holds all three reader
  kinds to identical answers.

- **`NativeTestServer` accepts through the production protocol factory.**  It
  used `asyncio.start_server`, so the large share of the suite that runs
  through it was exercising the legacy read path rather than the one that
  ships.

## [0.72.0] — 2026-08-08

### Security

Two instances of one defect class — a path that answers a request without
reading its body, leaving those octets to be re-read as something else.  Both
are keep-alive framing desyncs of the shape request smuggling exploits; neither
has been demonstrated cross-client, and the measured cases are self-inflicted.
The remedies differ because the RFC treats the two methods differently.

- **A WebSocket handshake that declares content is now refused with `400`.**
  ⚠️ **Behaviour change** — an upgrade request with `Content-Length` above zero
  or any `Transfer-Encoding` draws a 400 and a close; nothing switches
  protocols.  `Content-Length: 0` declares no content and still upgrades.
  This closes the
  sibling of the `OPTIONS *` defect below, found while fixing it: the upgrade
  path leaves the keep-alive loop before the drain, so those bytes were read
  back after the 101 as WebSocket frames and **delivered to the application as
  a message** — an HTTP request body arriving in a handler as something no
  client sent.  Reaching the same drain is not the remedy here, because after
  a protocol switch the octets carry two contradictory framings and an
  intermediary may resolve them the other way; refusing removes the
  disagreement instead of picking a side.  RFC 9110 §9.3.1 gives content on a
  `GET` no defined semantics, so no real client sends one — verified against
  `websockets` 16.1.1 (handshake, text/binary/70 KB echo, ping-pong, clean
  close, all unaffected).

- **`OPTIONS *` carrying a request body desynchronised the connection.**  The
  server-wide answer (RFC 9112 §3.2.4) replies without routing and without
  reading the body, and it was the one answered path that skipped the
  keep-alive drain, so the leftover bytes were parsed as the start of the next
  request: `OPTIONS *` with `Content-Length: 4`, pipelined with `GET /`,
  answered `204` then **`405`** — the method had parsed as `bodyGET`.  RFC 9110
  §9.3.7 explicitly permits content on `OPTIONS`, so this is a conforming
  request shape, and behind a reverse proxy that pools upstream connections the
  desync is the standard request-smuggling shape (not demonstrated
  cross-client here — the measured case is self-inflicted).  The drain
  predicate now lives in the loop tail with no per-path exemption, so every
  request that stays on the connection reaches it, and the 64 KiB bound applies
  uniformly: a body past it closes the connection instead of draining
  unboundedly.
  Http11Probe covers bodyless `OPTIONS *` and origin-form `OPTIONS /` with a
  body, but never the product and never with a pipelined follow-up, so its
  159/159 was clean on both sides of the defect.

## [0.71.0] — 2026-08-07

### Fixed

- **A static file larger than the `StaticFiles` cache threshold (4 MiB),
  requested with `Accept-Encoding`, returned no response at all.**  Above the
  threshold `StaticFiles` sends `http.response.start` then
  `http.response.pathsend`; with a codec negotiated the Compression middleware
  was still holding the start pending a compress decision, so the sender —
  which drops a pathsend it has no buffered start for — emitted nothing.  The
  held header is now released before any event compression cannot act on.
  Pre-existing and older than v0.67.0; never surfaced because the benchmark
  corpus is all small files.
- **The `static` lane no longer round-trips through ASGI dicts.**  The native
  complete-response path matched only a response arriving as one object, so
  `StaticFiles` — which sends its header and body as two events — expanded
  through `to_asgi()` and was re-converted below, on every request that
  negotiated a codec.  Compression now holds a header arm and merges it with
  the terminal body that follows, restoring one-object-one-send.  Worth
  ~0.6 µs/req (microbenchmark, N=30,000), which an ABBA A/B on the static
  lane could not separate from noise: −0.52 % ± 0.31 against a 0.26 % null.
  The v0.67.0 → v0.70.0 HttpArena `static` delta is **not** explained by this
  round trip and remains unattributed.

- **`Expect: 100-continue` is answered on every request, not just the first
  on a connection.**  The interim response was written before the shared
  `HTTP1Sender`'s per-request reset, so its "response already complete" guard
  — still set from the previous response — dropped it from request two
  onward, and the peer stalled until its own Expect timeout before sending
  the body.  An interim response no longer completes the exchange or commits
  a status, so a request that later times out can still be answered with 408.
- **A 1xx response no longer carries `Content-Length` or `Transfer-Encoding`**
  (RFC 9110 §8.6, RFC 9112 §6.1).  An informational response has no body, and
  a length a proxy believes bounds one desyncs the connection the real
  response still has to use.
- **The HTTP/1.1 client reads a body-less response as empty instead of
  crashing.**  `Headers.get` returns `b''` for a missing field, so the
  presence test for `Content-Length` was always true and the documented
  "no `Content-Length` → empty body" branch was unreachable: a 101, 204, or
  304 raised `ValueError: invalid literal for int() with base 10: b''`.
  Latent until the 1xx framing fix above stopped the server from padding its
  own 101 handshake with `Content-Length: 0`.
- **The deferred WebSocket reader is actually reachable.**  The branch that
  marked the reader deferred sat behind a condition that could never be true,
  so a `websocket_message` listener started an eager reader at connect
  instead — the per-message queue handoff the deferred design exists to avoid,
  paid by every *consuming* handler.  `start_deferred_reader()`, the idle
  watchdog's deferred branch, and the `_deferred_pending` gates were all dead
  code.
- **WebSocket wire ownership is enforced.**  The flag marking the app as owner
  of the transport while it drives `receive()` inline was never set, so the
  idle watchdog could read underneath a handler parked mid-frame and
  interpret payload bytes as a frame header.  Switching read modes also no
  longer strands an event the previous mode had buffered.

### Added

- **`@as_middleware` honours a `scope` parameter.**  The middleware guide has
  always documented the ASGI form — `async def mw(scope, receive, send,
  call_next)` reading `scope['method']` — but BlackBull's own server threads a
  native `Connection`, which is not subscriptable, so that example raised
  `TypeError` on every request.  A middleware whose first parameter is
  literally named `scope` is now handed a real ASGI scope dict, and is adapted
  at both of its own edges: the events its `send` wrapper observes are ASGI
  dicts (it will inspect `event['type']`), and its emissions are native again
  on the way out.  The dict form therefore exists across exactly that one
  frame — `call_next` always passes the `Connection` down.

  Any other parameter name (`conn`, `connection`, …) is native and is not
  adapted at all, so the default path pays nothing.  The decision is made once
  from the signature, at decoration time, and recorded as
  `__blackbull_asgi_scope__`.

### Changed

- Registering a `websocket_message` listener no longer forces read-ahead on
  at `BB_WS_QUEUE_DEPTH=0`.  The event still fires when the server *reads*
  the message: a consuming handler drives the wire itself, and the idle
  watchdog starts the deferred reader if the handler goes quiet.  A positive
  `BB_WS_QUEUE_DEPTH` is unchanged — an explicit opt-in to read-ahead.

### Internal

- **HttpArena correctness gate runs on WSL2; EC2 only ready-checks.**  New
  `bench/httparena/build_wheel.sh` (git-archive wheel build + sha256 record),
  `validate_local.sh` (clone+patch the `MDA2AV/HttpArena` harness, stage the
  framework, pre-build the image, run `validate.sh` under a wall-clock bound,
  verdict to `bench/results/httparena-local/<UTC>/verdict.txt`) and
  `ready_check.sh` (minimal container/port/WS/TLS/h2c/gRPC smoke).
  `run_httparena.sh` runs `ready_check.sh` in place of the full `validate.sh`
  when `SKIP_VALIDATE=1`; `httparena_compare.sh` uploads it and records the
  wheel sha256 in `provenance.md`.  The identity rule: validate and benchmark
  the **same wheel file** (`BB_WHEEL_PATH`).

- **HttpArena crud profile contract completed.**  The get-by-id route now
  emits `x-cache: MISS|HIT` from the Redis cache status (the harness's
  cache-aside check), and the create INSERT supplies the NOT NULL columns
  (`active`, `tags`, `rating_score`, `rating_count`) the schema requires —
  the harness's crud POST was 503 without them.  Both gaps previously caused
  HttpArena's `validate.sh` to silently abort mid-crud (the empty `x-cache`
  grep tripped `set -euo pipefail`), so validation never reached the later
  profiles on EC2 either; the local gate surfaced and fixed them.

- **Every framework-owned response producer emits native.**  `StaticFiles`
  (cache hit, sendfile, chunked fallback, and its error/`304` responses),
  the `CORS` preflight, and the `Cache` middleware's stored entries now build
  `NativeResponse` directly instead of ASGI event dicts; `NativeResponse`
  grew a `file_path` arm so the sendfile form is one native shape rather than
  a `http.response.pathsend` dict.  `Cache` stores `(status, header, body)` as
  data and builds a fresh response per hit, keeping the header-list copy the
  dict form provided.

  With no dict producer left inside the seam, `Compression`'s ASGI-dict lane
  (`_dict_event` and the buffered-parts tail) and the app's `_boundary_wrap`
  are both deleted — the second conversion altitude has nothing left to
  catch.  `parse_response_event` now has no caller inside the framework; it
  remains exported for the external compat surface.

  Counted on the `static` lane (`Accept-Encoding: gzip`, one request):
  send-path adapter closures 2 → 1, `NativeResponse` allocations 3 → 2,
  `to_asgi()` round trips 0, per-request function-level imports 9 → 8.  This
  is object-count work; it is not an A/B result, and the unattributed
  v0.67.0 → v0.70.0 `static` delta stays open.

- **Architecture guard: the ASGI boundaries are enumerated.**
  `tests/architecture/test_single_native_world.py` scans the package for
  native↔ASGI conversions (`to_asgi()`, `to_asgi_scope()`, response-event dict
  literals, `http.request` dict literals) and fails on any site not named in
  its allowlist with the reason it exists.  Entries are marked *boundary*
  (permanent — where BlackBull meets ASGI) or *residual* (a producer awaiting
  conversion); every entry is *boundary*, on every rule and both directions,
  so the enumerated edges are the only places a dict is built.  The guard
  self-checks, so a scanner that stopped matching cannot pass vacuously.

- **`Response` and `StreamingResponse` emit native (proposal §8.2).**  Both are
  BlackBull-owned serialisers on BlackBull's own send path, and both emitted
  `http.response.*` dicts that `wrap_native_send` converted straight back —
  the last response-dict round trip in the framework.  `Response` gets the
  bigger win: a complete response is now **one object, one send** (header and
  body together) where the dict form always cost two.  The shared
  `_emit_response` helper carries the app's `send(body, status, headers)`
  convenience form and the default error handler with it.

  No residual response-dict producers remain; the architecture guard's
  allowlist is boundary-only on that rule.

- **`CORS` and `Compression` no longer crash an object-form WebSocket
  handler.**  Both middleware wrap `send` with a header-injecting wrapper whose
  non-native branch assumed every event is a dict and called `.get('type')` on
  it.  Once the WS send channel went native (below), an object-form handler
  sending through either raised
  `AttributeError: 'NativeWSMessage' object has no attribute 'get'` —
  `NativeWSMessage` is a `__slots__` class with no `.get`.  Reachable on
  `CORS` for any object-form WS handler whose upgrade request carries an
  allowed `Origin`, and on `Compression` via the no-matching-codec path.  The
  raw `(conn, receive, send)` form was unaffected, since its events really are
  dicts — which is why the break was invisible until the object form was run.

  `Cache` was already correct.  Both branches are now guarded with
  `isinstance(event, dict)`, and both guards are mutation-tested: removing
  either turns the new suite red.

- **The WebSocket receive channel is native too (proposal §6, receive half).**
  `WebSocketRecipient` built a `websocket.receive` dict for every message,
  which the `WebSocket` object then took apart one frame later to hand the
  application the `str | bytes` it had a moment earlier.  The channel now
  carries the message itself — `str` for text, `bytes` for binary, which *is*
  the discriminator the object's public contract already publishes — via
  `next_message()`, with the peer's close raising `WebSocketDisconnect`
  carrying the RFC 6455 §7.4 code and a `ProtocolError` propagating unchanged.
  `await_connect()` is the handshake counterpart.  Same shape as the HTTP body
  channel, for the same reason.

  `receive()` is unchanged and still mints the ASGI dicts, for the raw
  `(conn, receive, send)` form and the external host.  Measured on the real
  recipient (20 000 messages, 64-byte payloads, mean ± SE over 3 runs):
  **2550.1 ± 30.1 → 2438.5 ± 30.6 ns/message**, −4.4 %.

- **A WebSocket's close code is recorded once.**  `WebSocketActor` kept its own
  copy of the code, updated only when a disconnect *event* passed through its
  receive wrapper — but a protocol violation emits the exception instead, so
  the copy stayed at its `1006 ABNORMAL` default while the server had already
  sent `CLOSE(1002)` on the wire.  `websocket_disconnected` and the access log
  now report the code the peer actually received.  The wrapper is gone with
  it: one less per-message coroutine hop on the WebSocket hot path.

- **The WebSocket send channel is native (proposal §6).**  The `WebSocket`
  object was called the native form but was a facade: `accept()` /
  `send_text()` / `send_bytes()` / `close()` built `websocket.*` ASGI dicts and
  pushed them down the *same* channel the raw `(conn, receive, send)` form
  uses, and `WebSocketSender` had no native arm at all — HTTP gained
  `case NativeResponse():` in Sprint 93; WS never did.  The handler never saw
  those dicts, but middleware, the actor, and the sender all did.

  New `NativeWSMessage` (accept / send / close, tagged by `kind` because the
  variants carry disjoint payloads), a native arm on `WebSocketSender` sharing
  its framing helpers with the dict arm, and a native accept arm on
  `WebSocketActor`.  `websocket.*` dicts now appear only at the enumerated
  boundaries: the external ASGI edge, the raw compat form, and the Tier-2 test
  client.  The sender tests the **dict** shape first: a dict is the one arm
  here that nothing cheaper than `isinstance` recognises, so ordering it first
  costs the native arm nothing and saves the compat path — every raw-form
  handler and every external ASGI host — the second check it would otherwise
  pay as a type guard.  Worth 61 ns on a ~800 ns send (ABBA, in-process,
  n=16/arm); a test asserts the dict arm is reached without the native type
  being consulted at all, so the ordering cannot regress silently.
  A test asserts the two arms put **identical bytes on the wire**, so
  the compat surface cannot drift.

- **The gRPC bridge emits native (proposal §7).**  `serve_grpc` is dispatched
  from `BlackBull._dispatch` *before* the handler-boundary adapter, so its
  `http.response.*` dicts reached middleware and the sender unconverted — an
  ASGI shape on the native seam that nothing had asked for, from
  framework-internal code that only lives in a module called `asgi.py`.  All
  seven emission sites now build `NativeResponse`; the wire contract is
  unchanged and tested in both directions.

- **The last request-dict producer is gone (proposal §8.1).**  The H2 trailers
  path (RFC 9113 §8.1 — a second HEADERS on an open stream) built an
  `http.request` dict for the recipient to translate straight back.
  `put_event()` is replaced by `put_end_of_stream()`, which enqueues the native
  pair.  RFC 8441 WS-over-H2 (`HTTP2WSWriter`, and the client's `_send_ws`)
  emits `NativeResponse` too (§8.2).

- **The request body crosses the framework as `bytes`.**  `HTTP1Recipient` and
  `HTTP2Recipient` grew `next_chunk()`, which returns the chunk itself and
  `None` once the body is complete; a peer that vanishes mid-body raises
  `ClientDisconnected`, and so does a body-read timeout (still recorded as a
  cap hit).  `None` rather than `b''` for the reason `NativeResponse` decides
  presence with `is not None` — an empty body is a real body — and the
  sentinel is unambiguous on both framings, since a zero-length chunk *is* the
  terminator in chunked encoding (RFC 9112 §7.1) and a Content-Length slice is
  never empty.

  `Connection.body()` / `stream()` — and `read_body` / `stream_body` beneath
  them — consume that channel directly.  The `http.request` dict is now built
  **only** by the recipients' `__call__`, i.e. only when something asks for the
  ASGI encoding: a full-form handler calling `receive()`, or an external host.
  It used to be built unconditionally, so a handler using `conn.body()` paid
  one dict per chunk it never read.

  No user-visible contract changes: `receive()` returns the identical event
  sequence (`more_body` is recovered from the end marker `next_chunk` just
  set), and both channels share that marker so a reader starting on one cannot
  block on the other.  Measured on the real H1 recipient at 4 KiB chunks over
  2000 requests per run (mean ± SE, N=5): **803.2 ± 52.6 → 536.3 ± 12.3
  ns/chunk**, −33.2 % — 4.27 µs on a 64 KiB upload, which also goes from 16
  `http.request` dicts to **0**.

- **The receive and event paths take a `Connection`, not "a `Connection` or a
  scope dict".**  `HTTP1Recipient.__init__` / `bind()` carried a three-way
  shape check — native `Connection`, a scope dict with a stashed `Connection`,
  a raw scope dict — plus a `Headers` re-wrap, on a per-request path.  Only
  `HTTP1Actor._dispatch_request` ever builds or rebinds a recipient and it is
  typed `conn: Connection`; under `BB_FORCE_ASGI_SCOPE=1` the *app* gets the
  scope dict while the recipient still gets the `Connection`.  The dict shape
  was reachable from tests alone.  Same deletion in `WebSocketRecipient`
  (`conn`, the `websocket_disconnected` detail, the frame-payload cap log) and
  in `EventAggregator._ws_fields`.

  `RequestActor` keeps its `dict | Connection` union — that one is the real
  `BB_FORCE_ASGI_SCOPE` lane, not a compat leftover.

### Docs

- WebSocket guide + env-vars reference: the `websocket_message` note now
  describes the deferred reader instead of forced read-ahead.
- Middleware guide: the `scope` / `conn` parameter names now select the
  request form a middleware receives, and the examples say which is which.
  The `scope`-subscripting examples throughout the middleware, logging,
  extensions, requests-and-responses, testing, and first-app pages were
  rewritten to the native form — they raised `TypeError` as written, because
  an undecorated middleware receives a `Connection`, which is not
  subscriptable.  Injection is now documented as `conn.state[...]`: a
  top-level scope-key write never reached inner layers.
- **`blackbull.testing.NativeResponse` is now `NativeTestResponse`.**  The
  old name collided with `blackbull.native.NativeResponse` — the framework's
  send message — so two unrelated classes were indistinguishable in a
  traceback or an `isinstance` check, and `testing/native.py` had to import
  the framework one *inside a function* to dodge the shadowing.  It joins its
  siblings `NativeClient` / `NativeTestServer`.  `NativeResponse` remains as
  an alias, so existing imports keep working.
- Handler-facing docs no longer teach the request as a scope dict.  The
  `hello-world` request table, `routing`'s `path_params`, `error-handling`'s
  `state`, `requests-and-responses`' header and query-string sections,
  `http2`'s `extensions`, and `behind-nginx`'s `client` / `scheme` were all
  written as `scope['x']` — which raises `TypeError` on the `Connection` a
  handler actually receives.  They now use attributes, and 27 full-form
  handler signatures were renamed from `scope` to `conn`.  Every rewritten
  idiom was executed against a running app.  The `scope[...]` references that
  remain are the ones that genuinely mean the ASGI scope: the parameter-name
  rule's own example, two field-origin notes, and a pre-v0.31 migration note.
- Events guide: the request- and connection-scoped events carry
  `detail['conn']` — the native `Connection` — which the reference tables
  called `scope` and typed as an ASGI dict.  The four examples that read it
  as a mapping (`.get('state', {})`, `['headers']`, `['path']`) now use
  attributes, and the `websocket_connected` / `websocket_disconnected` rows
  list the keys those events actually emit.

---

## [0.70.0] — 2026-08-04

### Changed

- **HTTP/2 sends natively — the send-side native seam now covers every HTTP
  boundary.**  `HTTP2Sender` consumes `NativeResponse` (header / body /
  trailers arms mirroring the H1 path), and the three Sprint-92
  `http_version == '1.1'` gates are removed: the handler-boundary adapter,
  `_boundary_wrap`, and `as_middleware` normalise to the native contract
  unconditionally.  ASGI event dicts remain only on the WebSocket lane and
  the external-host edge (`BlackBull(asgi=True)` under uvicorn, via
  `to_asgi()`).  An EC2 A/B (m7a.2xlarge, H1/WS/H2 lanes) showed no
  regression: the H2 native arm measured **+0.66 % ± 0.26** over the dict
  lane (2.5 SE), H1 neutral, WS neutral.

- **WebSocket: the deferred reader (design A′) is gone; canonical
  post-terminal receive.**  A `websocket_message` listener now switches
  read-ahead back on at connect — a consuming handler still keeps the
  Sprint-89 inline win, because the event fires when the *server* reads,
  which in inline mode is exactly when `receive()` drives the read.  Once
  the terminal event (disconnect or protocol error) has been handed to the
  app, `receive()` keeps answering a disconnect with the last terminal close
  code in both modes — eager mode previously blocked forever on a dead
  queue; inline returned a hardcoded ABNORMAL.

### Internal

- **Orphan audit (vulture 2.16):** removed verified dead code —
  `blackbull/server/http2_messages.py` (never imported), WebSocket
  `has_received_closed`, `Compression._brotli_quality`,
  `Reloader._watchfiles`, `Headers.has_continuation`,
  `Headers.set_table_size` / `table_size` (RFC 9113 §6.2 has no
  `table_size`), `Stream.on_rst_received` / `closed_via_rst`, and a dead
  Router f-string.  Public API kept even where test-only (project policy).

- **A/B harness:** new HTTP/2 lane runner (`ab_commit_h2.sh`, h2c + h2load)
  and WebSocket/H2 runner fixes (deleted-file swap handling, venv `python`
  resolution, no "AD" index state on restore).

## [0.69.0] — 2026-08-03

### Changed

- **WebSocket reads inline by default; `BB_WS_QUEUE_DEPTH` now means read-ahead
  depth and defaults to `0` (was `256`).**  Each connection used to run a
  background reader task that handed every inbound message to the handler
  through an `asyncio.Queue`.  That handoff cost one future plus one
  `call_soon` per message — the whole of WebSocket's event-loop overhead above
  HTTP/1.1.  Reading inline in the handler's own task drops the per-message
  loop touches from **4.09 to 2.06**, exactly HTTP/1.1's figure
  (`python bench/loop_touches.py`); HTTP/1.1 and HTTP/2 are unchanged.

  Control frames are still handled for you in both modes — a `ping` is
  answered and a `close` echoed per RFC 6455 §5.5 — but inline mode does so
  when the handler drives the next `receive()` rather than ahead of it
  (RFC 6455 §5.5.2 permits a delayed `pong`).  Set `BB_WS_QUEUE_DEPTH` to a
  positive value to restore the background reader and its bounded buffering;
  that is the right choice for a handler that does slow work between reads.

  Registering a `websocket_message` listener switches read-ahead back on
  automatically, so the documented contract that the event fires when the
  *server* reads a message — not when the handler consumes it — is preserved
  without configuration.  Client sessions are unaffected: they keep read-ahead
  on by default.

- **Design A′: a `websocket_message` listener no longer forces read-ahead on
  at connect; bounded control-frame servicing for non-reading handlers.**
  The Sprint 89 inline win was conditional on not registering a
  `websocket_message` listener (a listener forced the background reader and
  its 4.09 loop touches).  Now a consuming handler keeps the inline win even
  with a listener registered — the read-time emit adapter fires the event
  when the message is read, which in inline mode is exactly when the handler
  calls `receive()`.  If the handler goes quiet for more than ~one scanner
  tick, the deadline scanner starts a deferred reader that produces the
  events (and buffers messages) without ever adding a timer per connection.

  Control frames for a handler that is *not* reading are bounded by two new
  mechanisms instead of being deferred to the next `receive()`: send-time
  servicing (each `send()` answers PINGs/CLOSE already fully buffered,
  non-blocking) and an idle watchdog on the per-process deadline scanner (a
  connection idle > ~0.3 s gets its buffered control frames serviced each
  tick).  Worst-case PONG latency is bounded to ~one scanner tick with no
  per-connection timers.

  The server path also emits the documented canonical `websocket_message`
  detail shape `{'conn', 'text', 'bytes'}` (it had drifted to
  `{'conn', 'message'}` on the real server path).  Loop touches stay at the
  inline floor: **WebSocket 2.08**, HTTP/1.1 2.06, HTTP/2 5.21 — unchanged
  (`python bench/loop_touches.py`).

### Internal

- **The HTTP per-request listener checks use the generation-keyed plain-bool
  cache the WS path already had.**  `has_request_completed_listeners` /
  `has_request_disconnected_listeners` collapse to a cached bool + int
  compare instead of a dispatcher set lookup per request.  An EC2 four-row
  A/B showed no measurable throughput change (the zero-listener workload
  cannot resolve sub-0.3 % effects); shipped as structure matching the WS
  pattern.
- **The WebSocket idle watchdog is armed once at connect, not per message.**
  `send_touch` no longer re-arms the watchdog, removing the per-message arm
  check from the echo path.  Measured on the EC2 WS echo lane at
  **+0.78 % ± 0.49** (four-row rule: clears its own SE and the null floor);
  the ~1-tick worst-case PONG-latency contract is unchanged.

## [0.68.1] — 2026-08-02

Post-Sprint-88 patch — router param-kind classification (which also fixed a
`{body}`-placeholder regression), a zero-hop dispatch merge, docs, and CI/
dependency bumps.  Internal refactor + fix only: no new public API, no new
env vars, no behaviour change beyond the fix below.

### Fixed

- **A `{body}` path placeholder now binds the path value in the plain
  wrapper too.**  The zero-overhead-pin wrapper dispatched a parameter
  literally named `body` to the request-body branch regardless of the route,
  so `@app.route(path='/x/{body}')` with `async def h(body)` bound the raw
  request body where the extended wrapper — and the path-param classifier,
  which gives path placeholders top precedence — bound the path segment.  The
  two wrappers now agree: the placeholder wins, and a bare `body` parameter
  with no `{body}` placeholder still binds the request body, unchanged.
  Pinned by three new regression tests covering both wrappers and the normal
  case.

### Internal

- **Simplified-handler parameters are classified once at registration into a
  `_ParamKind` enum, and the plain and extended wrappers dispatch on it with
  `match`.**  The wrappers previously re-derived parameter kinds from string
  literals (`'conn'`, `'body'`, `'query'`, …), which is how the plain and
  extended wrappers drifted apart; the classification now lives in one place,
  produced once per handler at registration.  A plain `Enum` is deliberate —
  `kind == 'query'` must fail loudly, not string-match.
- **The extended wrapper is built by a factory extracted at registration
  time**, so both wrapper shapes share one construction path instead of two
  independent `_adapt_handler` branches.
- **Per-request dispatch prep merged into `_dispatch_request` (zero-hop).**
  The HTTP/1.1 actor's per-request preparation is folded into the dispatch
  call, removing one call boundary from the request path (no benchmark claim;
  shipped as structure).

### Docs

- **A/B verdict asymmetry documented.**  `bench/peers/AB-HIGH-PRECISION.md`
  records why local and EC2 A/B verdicts can disagree, and the ab-verify
  workflow is wired into the agent docs (`AGENTS.md`) with EC2 calibration
  and a two-consecutive-polls wait rule for reading check rollups.
- **ab-verify EC2 launcher added** — `bench/aws/ab.sh` (ABBA measurement +
  import-hash proof) with `install.sh` uv/.git provisioning and a
  `native_app` bench target, so high-precision A/Bs can run on EC2 without
  ad-hoc setup.

### CI

- **Dependabot group and dependency bumps.**  The `python-deps` group gains a
  `dependabot.yml` entry; pyright → 1.1.411, codeql-action steps → v4.37.4,
  and `pypa/gh-action-pypi-publish` → 1.14.2.  No runtime dependency changes.

## [0.68.0] — 2026-08-01

### Added

- **A test client that drives the path a request actually takes.**  BlackBull
  threads a typed `Connection` end to end, but the only test client drove the
  app through `httpx.ASGITransport` → ASGI scope → `from_scope()` — so the
  `isinstance(conn, Connection)` branch of `BlackBull.__call__`, the branch
  every production request takes, was never exercised by a `TestClient` test.
  A defect could live on the native path with the whole suite green, and one
  did: Sprint 87's `HEAD`-answers-405.  Two tiers close it.
  **`blackbull.testing.native`** builds a `Connection` and calls
  `app(conn, receive, send)` directly — everything from `Connection` inward
  (dispatcher, middleware chain, router, dependency injection, events,
  response serialisation), no socket and no protocol actor.
  **`NativeTestServer`** binds a loopback socket and runs BlackBull's own
  server, so a request travels accept → `HTTP1Actor` parse → `Connection` →
  dispatch → wire bytes, which is the only place framing headers, keep-alive
  reuse, and the RFC 9110 §9.3.2 `HEAD` body strip are observable.
- **Both tiers are async-first with synchronous façades.**  The core is
  coroutines — the app entry point *is* a coroutine, and a handler runs on the
  caller's event loop exactly as it does in production.  `NativeClient` and
  `NativeTestServer`'s `with` form wrap them for tests written as plain `def`,
  each owning one background event loop for the whole session rather than one
  per request.
- **`NativeTestServer.connections_served`** — a TCP *accept* counter, so a
  test can assert keep-alive reuse as a fact (`== 1` after ten requests)
  instead of inferring it from the absence of a `Connection: close` header
  that a server is never obliged to send.

### Changed

- **`TestClient`'s documented role is narrowed to what it uniquely covers.**
  Its behaviour and API are unchanged; what changed is the recommendation.  It
  is the **ASGI compatibility boundary** instrument — the `as_scope()` /
  `from_scope()` round-trip driven the way an external ASGI host drives it —
  and it stays precisely because a missing `_CONNECTION_FIELDS` entry or a
  coercion bug in `from_scope()` surfaces there and nowhere else.  For
  application-logic tests, `blackbull.testing.native` is now the default.
- `blackbull/testing.py` is now the package `blackbull/testing/`.  Every
  existing import (`from blackbull.testing import TestClient`,
  `WebSocketTestSession`, `WebSocketDisconnect`) resolves unchanged.

### Docs

- `docs/guide/testing.md` rewritten around which instrument answers which
  question, with the two dispatch paths shown side by side so the choice
  between `native` and `TestClient` is a structural one rather than taste.
- `KNOWN_LIMITATIONS.md` records Tier 2's scope: HTTP/1.1 and WebSocket,
  cleartext only — TLS, ALPN, and HTTP/2 stay with the BlackBull clients +
  ephemeral-port pattern.

### Internal

- The dual-path corpus moved to `tests/conformance/http1/_dual_path_corpus.py`
  as a single definition of the request shapes the compat lane must be
  invisible for.  For the vectors a conformant client can issue, the client
  spec is the definition and the raw bytes are derived from it, so the two
  drives cannot drift; the malformed and raw-form vectors (`OPTIONS *`,
  obs-fold, HTTP/9.9, …) stay raw-drive-only.  Byte identity is still asserted
  over all 19; the client-expressible subset is now also replayed through a
  real socket on both lanes.
- Http11Probe re-scored at **159/159 scored, 0 failed, 0 errors**, with a
  per-test verdict diff **empty** against the v0.67.0 baseline on both the
  native and `BB_FORCE_ASGI_SCOPE=1` lanes.

## [0.67.0] — 2026-07-31

### Changed

- **Header lines whose values a specification enumerates are validated once at
  import, not once per request.**  A process-wide table seeds Fetch Metadata's
  `Sec-Fetch-*`, the UA client hints' boolean/platform forms and RFC 9110's
  fixed tokens (`Connection`, `TE`, `Pragma`, `Upgrade-Insecure-Requests`,
  `DNT`) — **56 % of all header lines** on captured browser traffic, and a
  share that does not decay with connection churn.  It is the HPACK *static*
  table's idea, which HTTP/1.1 has no wire form for.  Entries are admitted
  only because a spec fixes their value set, never because they were frequent
  in a capture; framing names (`Content-Length`, `Transfer-Encoding`, `Host`,
  `Expect`, `Upgrade`) are excluded by a check that raises at import; and each
  entry is asserted equal to what parsing that line actually produces.  This is
  what makes the win survive short-lived connections: at one request per
  connection the per-connection cache alone was **21 % slower** than no cache,
  and with the shared table it is **6 % faster**.
- **Keep-alive connections stop re-validating header lines they have already
  validated.**  A peer that resends a byte-identical header line — which every
  browser does for `User-Agent`, `Accept`, `Accept-Language` and `Cookie` —
  now gets that line answered from a per-connection cache of
  `raw line bytes → (name, value)` pairs, replacing the colon split, token
  check, lowercase, OWS strip and value scan with one dict lookup.  Scored on
  **captured** traffic — a real Chromium loading a real page, every request
  head recorded as it arrived — 21 requests carried 275 header lines drawn from
  only 26 distinct ones, and parse cost falls **8.96 → 5.85 µs (−35 %)** at the
  observed single connection, **−27 %** when the same requests are re-dealt
  across the six connections Chromium opens per origin.  The cache is keyed per
  *line*, so a request that changes four of its thirteen lines still hits on
  the other nine, and header **order** changing between navigations and
  subresources costs it nothing.  Against an adversarial peer whose every
  header value is unique it is still **5 %** faster, so no input shape
  regresses.  Because the key is attacker-controlled the cache is bounded by
  **bytes, not entries** — lines over 1 KiB skip it entirely (lookup included)
  and a per-connection 8 KiB budget caps admission — which holds the worst case
  to **16 KiB/connection and +19.2 % CPU** under a peer sending 64
  never-repeating 128-byte headers per request.  An entry-count bound alone
  would have retained ~1 MiB per connection (9.6 GiB across 10k connections)
  against 988 B of real need.  The per-header slope falls
  **0.413 → 0.174 µs** (−58 %) and a 32-header request **17.06 → 8.78 µs**.
  Nothing enters the
  cache unvalidated, the key is the exact line bytes so one changed byte is a
  miss, the cache is per connection so validated lines never cross a tenant,
  and it is bounded at 64 entries so an attacker cycling unique names cannot
  grow it.  Requests parse to exactly what they parsed to before — asserted by
  a differential test, and by an unchanged 213-test Http11Probe verdict.
- **The HTTP/1.1 header-size limit is read once per connection, not once per
  request.**  `_parse` ran a `from ..env import get_settings` statement on
  every call; the limit is connection-scoped, so it is memoised on the actor.
  This is what makes the cache-miss path faster too.
- **The status line and small Content-Length values come from tables.**  Both
  were rebuilt per response (`f'HTTP/1.1 {status} {status.phrase}'.encode()`
  and `str(n).encode()`); they are now precomputed at import for every
  `HTTPStatus` member and for lengths `0…8192`, each falling back to the
  original expression outside that domain.
- **`EventDispatcher.has_listeners` answers from a registration index.**  It
  probed three dicts, once per lifecycle emit site per request; it is now one
  set lookup.

Measured end to end on one box in one session, one worker, medians of five
interleaved sweeps: **+6.7 % to +10.9 %** req/s, the larger figure on
browser-shaped header sets.

### Fixed

- **`HEAD` requests were answered `405 Method Not Allowed` under
  `BB_FORCE_ASGI_SCOPE=1`.**  BlackBull synthesises a HEAD response by
  rewriting the method to `GET` and stripping the body, but the compat lane
  materialised its scope *snapshot* before that rewrite, so the router saw
  `HEAD`, found no HEAD route, and answered 405 where the native lane answered
  200 (`COMP-HEAD-NO-BODY`, RFC 9110 §9.3.2).  The app argument is now built
  after every pre-dispatch mutation of the `Connection`.  Both lanes now score
  an identical 159/159.

## [0.66.0] — 2026-07-30

### Changed

- **The HTTP/1.1 parser validates octets with C-level bulk operations.**  The
  request-target scan, the Host authority scan, and the field-name token check
  were per-byte Python generator expressions or regexes; each is now a single
  `bytes.translate` delete-table pass or a precompiled character class,
  whichever the set size favours.  Field values are checked **once for the
  whole header block** instead of once per header: deleting every permitted
  octet leaves only CR, LF and forbidden CTLs, and a residue that tiles into
  CRLF pairs proves no value can carry a forbidden octet.  A block that fails
  the pre-scan falls back to the per-header regex, so every error message and
  status code is unchanged — Http11Probe re-scores 159/159 with a
  **per-test verdict diff that is empty across all 213 vectors**.  Measured on
  a Zen 4 box: per-header cost **0.510 → 0.346 µs**, fixed cost **4.53 → 3.57
  µs**, and a 32-header request **20.33 → 14.25 µs (−30 %)**.
- **Header names are lowercased once instead of twice.**  `_parse` already
  lowercases each name while validating it, then handed the list to
  `Headers.__init__`, which lowercased every name again; `Headers.from_lowered`
  is the alternate constructor for callers that can guarantee pre-lowered
  input.  HTTP/2 qualifies by protocol — RFC 9113 §8.2.1 makes an uppercase
  field name malformed and the frame is rejected before any pair reaches the
  header list.  `Headers` lookups (`get`, `getlist`, `__contains__`,
  `__getitem__`, and the Structured Fields accessors) now probe with the
  caller's bytes before lowercasing.  The index is keyed lowercased, so the
  probe can only hit on a key the old path would also have found — a
  lowercase literal, which is what essentially every internal call site
  passes, drops from 47 to 25 ns.
- **`app.static()` registers a route instead of global middleware.**  Static
  serving no longer runs on every request: `<prefix>/{filepath:path}` (plus
  `<prefix>` and `<prefix>/`, so `index=` can still answer the mount root)
  resolves in the router, and a non-static request never enters `StaticFiles`
  at all.  This is also what Starlette, Sanic, aiohttp, Flask and Django all
  do.  The production gate is resolved once and memoised rather than calling
  `get_settings()` per request.  **Behaviour change**: a miss under the prefix
  is now answered 404 by the static route rather than falling through to
  another route that also matches the prefix; the 404 takes the normal error
  path, so `@app.on_error(HTTPStatus.NOT_FOUND)` still applies.  An explicit
  route on the bare prefix always wins — `app.static()` never replaces a path
  you registered yourself.
- **`Compression` no longer parses body events on its no-codec path.**  When
  the client accepts nothing the server can produce, the response is forwarded
  verbatim through a wrapper that stamps `Vary: Accept-Encoding` on the
  response start (bug 1.21f).  That wrapper ran `parse_response_event` on
  every event, allocating a `ResponseBody` copy of each body chunk only for
  the next line's `isinstance` to reject it.  It now discriminates on the raw
  event type first, so a streamed body costs one dict lookup per chunk.
- **`BB_WRITE_TIMEOUT` no longer arms a timer per response.**  It defaults to
  `30.0`, so every write took `asyncio.wait_for` — exactly one `loop.call_at`
  per response, which the loop-touch instrument read as `call_at=1.00`/req.
  The bound now rides the per-process deadline scanner that already enforces
  `BB_HEADER_TIMEOUT`, `BB_BODY_TIMEOUT`, and `BB_KEEP_ALIVE_TIMEOUT`.  The
  defence is unchanged — a slow-read peer still gets its transport closed and
  a `ConnectionResetError` still surfaces to the sender's existing error path
  — but the timeout now fires within `BB_DEADLINE_TICK_MS` of the requested
  instant instead of exactly on it (~1 % slop at the 30 s default).  BlackBull
  falls from 3.06 to 2.06 event-loop touches per request, which is the bare
  `asyncio.start_server` floor: all remaining per-request loop exposure is now
  the streams layer itself.

### Added

- **`bench/loop_ab.py` and `bench/loop_touches.py`** — permanent forms of the
  two-arm event-loop A/B and the loop-touch counter.  `loop_ab.py` runs both
  `BB_UVLOOP` arms of the same build in one session on pinned disjoint cores
  and reports stock/uvloop/gap against the previous run of the same harness;
  `--repo` points it at a git worktree so a previous commit can be baselined
  with the identical harness.  `loop_touches.py` counts `call_soon` +
  `call_at` + `call_later` + `create_future` per request for HTTP/1.1,
  HTTP/2, and WebSocket against a per-protocol budget, and `--check` fails on
  a rise.  It emits a count, not a duration, so unlike req/s it is
  machine-independent and can gate CI.
- **Loop-touch budget lane in `test.yml`.**  `bench/loop_touches.py --check`
  now runs on every push/PR.  Current budgets: HTTP/1.1 2.20, HTTP/2 5.40,
  WebSocket 4.30 touches per request.

### Docs

- **Sprint numbers and private defect IDs removed from comments and
  docstrings** across 249 files.  A docstring is read by users, who cannot
  resolve `Sprint 79` or `bug 1.16` — and `git log`, this changelog and the
  sprint logs already own the timeline.  Migration narration was rewritten as
  present-tense fact ("Sprint 64 moved emission from the server layer" →
  "Emission is consolidated into `_dispatch`").  Externally resolvable
  references are kept: RFC and CVE citations, Http11Probe/Autobahn vector
  names, and GitHub issue numbers.  Excluded deliberately: `bench/results/**`,
  `bench/CHARACTERIZATION.md` and `docs/about/grpc-assessment.md`, which *are*
  the record.
- **`docs/about/internals.md` gains a §Parse-path invariant** documenting the
  delete-the-allowed-table idiom, the whole-block value pre-scan, and a table
  of three plausible optimisations that were measured and rejected, so they
  are not retried blind.
- **`KNOWN_LIMITATIONS.md` static-file section corrected.**  It claimed
  `StaticFiles` emits no `ETag` and had to be paired with the `Cache`
  middleware; `StaticFiles` has emitted a strong `ETag` + `Last-Modified` and
  answered `If-None-Match` / `If-Modified-Since` by default since
  `conditional=True` became the default.  The section now also states the
  route-dispatch consequence for a miss under the prefix.
- **`Headers` class docstring corrected** — it still said every accessor
  lowercases the requested name, which stopped being true with the
  probe-first lookup fast path.

## [0.65.0] — 2026-07-29

### Added

- **`BB_H1_PROTOCOL` — buffer-owning HTTP/1.1 read front end (experimental,
  default off).**  Phase 1a of the H/1.1 fast path.  The header block is
  located with a single `find(b'\r\n\r\n')` scan over an accumulated buffer
  instead of one `readuntil(b'\r\n')` per header line, and any bytes read past
  the delimiter are kept rather than discarded — so a keep-alive or pipelined
  peer's next head is usually already in hand, and an await that resolves from
  the buffer never yields to the loop.

  The buffer is not an optimisation detail, it is what makes the scan possible
  at all.  Two things block the obvious `readuntil(b'\r\n\r\n')` version: a
  stream `readuntil` cannot see the bytes protocol detection already consumed
  (a minimal `GET / HTTP/1.0\r\n\r\n` leaves only `\r\n` on the wire, and the
  search blocks until the peer closes — the deadlock the line-by-line loop was
  written to avoid), and any chunked scan over-reads into the body or the next
  pipelined request.  Owning the buffer answers both: the scan starts from the
  already-consumed prefix, and the surplus has somewhere to live.

  This is a **measurement gate, not a supported switch** — it exists so both
  read paths can be A/B'd on identical builds, and will become the default or
  be removed once that lands.  Both paths pass the same HTTP/1.1 conformance
  suite; the flag changes how bytes arrive, never what a request means.

  `BufferedH1Reader` is registered as an `AbstractReader`.  Without that,
  `RecipientFactory.http1` re-wrapped it in an `AsyncioReader` on **every**
  request — the front end whose whole purpose is to remove per-request work was
  adding an allocation to every request of every keep-alive connection.

### Removed

- **The `scope` simplified-handler parameter alias.**  Naming a simplified
  handler's parameter `scope` used to inject the native `Connection`.  That is
  backwards: `scope` means a genuine ASGI scope dict everywhere else in the
  codebase, so the alias handed back an object that answered to the wrong name
  and made `scope['headers']` look correct — it compiled, registered, and
  failed at request time with `AttributeError`.  That is precisely how the 33
  tests below rotted.  It is now a `TypeError` **at registration**:

  ```python
  @app.route(path='/x')
  async def h(scope):          # TypeError: parameter 'scope' is not supported
      ...

  @app.route(path='/x')
  async def h(conn):           # use this — conn.headers, conn.query_string, …
      ...
  ```

  `conn` and `connection` are unchanged.  **Full-form handlers and middleware
  are unaffected** — `async def h(scope, receive, send)` takes its arguments
  positionally, so the router never inspects those names; the 196 such handlers
  in the test corpus and every documented example keep working.  Rejecting the
  name rather than merely dropping the alias is deliberate: an unannotated
  `scope` would otherwise fall through to the query-param fallback and silently
  start receiving a query-string value instead of the request.

### Fixed

- **33 rotted integration/conformance tests.**  Every one shared a root cause:
  the test's handler was written against an ASGI scope dict while BlackBull's
  native dispatch hands it a `Connection`, so the first mapping access raised
  `AttributeError: 'Connection' object has no attribute 'get'`.  Ported to the
  typed attributes (`conn.headers`, `conn.query_string`, `conn.cookies`,
  `conn.state`, `conn.extensions`) across `test_http1_request_timeout.py` (13),
  `test_middleware_composition.py` (6), `test_http2_advanced.py` (5),
  `test_request_features.py` (4), `test_trusted_proxy.py` (3), and
  `test_cookies.py` (2).  No library code changed — the framework was right and
  the tests had drifted from it.

### Changed

- **HTTP/1.1 keep-alive builds one recipient and one `RequestActor` per
  connection, not per request.**  Both were rebuilt on every request even
  though everything they hold that is expensive — the reader, the body
  timeout, the connection deadline, the app, the aggregator — belongs to the
  connection, not the request.  They are now rebound (`HTTP1Recipient.bind`,
  `RequestActor.bind`), which is the trade the sender already made with
  `reset_per_request_state`.  Safe for HTTP/1.1 specifically because a
  connection dispatches one request at a time; HTTP/2 keeps building one
  `RequestActor` per stream, since concurrent streams sharing an instance
  would interleave their fields.

  Measured on a same-session A/B over 400 serialized keep-alive requests:
  **17.46 → 16.25 µs/req (−6.9%)** through `HTTP1Actor.run`, with every "after"
  minimum below every "before" minimum across five runs each.  Rebinding turns
  out to save more than the two constructors do, because it also skips
  `RecipientFactory.http1`'s dispatch and the function-level
  `from ..env import get_settings` that ran inside `__init__` on every request.
  The 213-vector Http11Probe suite returns an identical per-test verdict before
  and after (0 failed, 0 errors), which is the check that matters here: the
  framing state a rebind must reset is exactly what request smuggling exploits
  when it leaks between requests.

- **The integration tier now gates pull requests.**  `tests/integration` and
  `tests/conformance` run under `--run-integration` on every push and PR
  (~98s locally for 3526 tests).  Previously the only job passing
  `--run-integration` was the weekly Full tier, gated on
  `schedule`/`workflow_dispatch`, so it gated nothing: the 33 tests above sat
  red for weeks with no path to a human.
- **The fast tier runs the full supported matrix** — 3.11, 3.12, 3.13, and
  3.14, matching the classifiers in `pyproject.toml`.  Testing two of the four
  versions we advertise is what let a 3.13 `TaskGroup` behaviour change ship an
  HTTP/2 connection-window leak (fixed in v0.62.0) while CI stayed green.  Both
  new interpreters were verified green before widening — no triage was needed.
- **A failing scheduled Full tier now files an issue.**  It had run 4 times
  and failed 2 of those unattended; by the time anyone looked, the logs had
  aged out.  Reuses one open `ci-full-tier` issue rather than filing weekly
  duplicates.

### Tests

- **`tests/architecture/test_native_handler_contract.py`** — a source scan
  forbidding the shape that rotted: a registered handler or middleware using
  its `Connection` as a mapping.  Turns a request-time `AttributeError` into a
  collection-time failure, repo-wide, with no allowlist.  Verified against the
  pre-fix sources, where it catches 23 of the 25 offending handler definitions
  behind the 33 failures.  The two it misses passed the `Connection` to a
  helper that subscripts it (`parse_cookies(conn)`) — indirect use needs
  call-graph analysis, and the guard documents that gap rather than implying
  coverage it does not have.

## [0.64.0] — 2026-07-29

### Added

- **WebSocket handlers inject from their signature.**  Path params, query
  params, and `Depends` now resolve into a WebSocket handler the way they
  already did for HTTP, alongside the `WebSocket` object:

  ```python
  @app.route(path='/rooms/{room}', scheme=Scheme.websocket)
  async def chat(ws: WebSocket, room: str, since: int = 0,
                 db=Depends(get_db)):
      ...
  ```

  This completes injection parity across handler surfaces — no first-party
  handler form now requires the raw `(conn, receive, send)` triplet, which
  stays supported and undeprecated.  A lone `ws` parameter keeps the
  allocation-free Sprint 82 fast path; injection is paid for only by
  signatures that ask for it.

  Two deliberate differences from the HTTP form: a WebSocket query param
  **must carry its annotation** (a bare name stays a registration-time
  `TypeError`, because `chat(socket)` means the socket rather than a query
  param named `socket`), and there is no body parameter, a WebSocket having
  no request body.

  A parameter that cannot be bound at connect time — required query param
  missing, value that will not coerce — **refuses the handshake with close
  code 1008** and never runs the handler; no dependency is resolved for a
  connection that is about to be rejected.

  `Depends` resolves **once per connection** and tears down when the handler
  exits, by any route — clean close, `WebSocketDisconnect`, or exception.
  Because that means a socket holds its dependency for its whole lifetime,
  the guide documents the pool-exhaustion hazard and the app-scoped-pool
  pattern that avoids it.  Note that cleanup written after a bare `yield` is
  skipped on the exception paths (ordinary `@asynccontextmanager`
  semantics) — providers should use `try`/`finally`.

- **`Depends` providers are warned when cleanup would be silently skipped.**
  Code written after a bare `yield` never runs when the handler raises — the
  exception is re-raised *at* the `yield` — so the resource leaks exactly
  when something went wrong.  BlackBull now emits a `UserWarning` at
  registration naming the provider and showing the `try`/`finally` fix.

  The check is deliberately narrow, and stays quiet for every shape that is
  already correct: a `yield` inside any `try` (`finally` covers all paths;
  `except`/`else` means the author is deliberately telling success from
  failure), a provider with nothing after the `yield`, and a `yield` inside
  an `async with`.  It never fails registration — a provider whose source is
  unavailable is left alone.

  The semantics themselves are unchanged and intentionally so: the exception
  must reach the generator, or a commit-or-rollback provider would commit on
  error.  Applies to HTTP and WebSocket alike.

### Docs

- `docs/guide/dependency-injection.md` gains a **Write cleanup in a
  `finally`** section; the provider-forms table no longer implies that "code
  after `yield`" and `finally` are equivalent.
- `docs/guide/websockets.md` gains an **Injected parameters** section with a
  dependency-lifetime warning; `KNOWN_LIMITATIONS.md`'s "WebSocket handlers
  take no injected parameters" entry is replaced by the two narrower fences
  that remain.  `examples/websocket_object.py` reads `room` from the
  signature instead of `conn.path_params`.

- **`README.md` documents HTTP QUERY (RFC 10008)**, which shipped without ever
  being named there — a reader met its caveats in `KNOWN_LIMITATIONS.md`
  before meeting the feature.  The new section covers `from blackbull import
  QUERY` and why routes registered against the exported string need no
  migration when `http.HTTPMethod` eventually grows a `QUERY` member.

- **`KNOWN_LIMITATIONS.md` separates limitations from deliberate non-goals.**
  Absent capabilities that were never promised (HTTP/3, an ORM, a gRPC
  client, CDN glue) moved to a "not limitations" table, and operational
  how-to moved to where it is actionable: the worker-count ceiling and the
  worker-0 raw-protocol rule to `docs/deployment/workers.md`, HTTP/2 fronting
  to `docs/deployment/behind-nginx.md`, the single-broker-owner rationale
  (an MQTT 5.0 session-state requirement) to `docs/guide/mqtt.md`, and the
  nginx differential-corpus divergences to `docs/about/conformance.md`.
  Nothing was dropped; 17 entries became 11 genuine ones.

### Internal

- **The test suite runs on Python 3.14 again.**  CPython 3.14 made
  `forkserver` the default `multiprocessing` start method on POSIX, and the
  live-server fixtures bind their listening socket in the parent before
  starting a worker that serves on it — an inherited socket and an app of
  locally-defined closures, neither of which pickles.  Every such fixture
  became a setup error, and because the repo's pre-commit hook runs the
  suite, the hook could not pass at all on 3.14.  `tests/conftest.py` now
  pins the `fork` start method those fixtures were written against.  No
  change to shipped code, and a no-op on 3.11/3.12 where `fork` is already
  the default.

## [0.63.0] — 2026-07-29

### Added

- **High-level WebSocket API.**  WebSocket handlers can now take a typed
  `WebSocket` object instead of raw event dicts:

  ```python
  @app.route(path='/ws', scheme=Scheme.websocket)
  async def ws_handler(ws: WebSocket):
      await ws.accept()
      async for message in ws:
          await ws.send(message)
  ```

  `accept()` / `close()` drive the handshake (calling `close()` *before*
  `accept()` rejects the connection outright), `send_text` / `send_bytes` /
  `send_json` / `send` write one complete message, and `receive` and its
  typed variants return bare `str`/`bytes` rather than an event.  Iterating
  with `async for` ends at disconnect instead of raising; call `receive()`
  directly and catch `WebSocketDisconnect` when the close code matters.
  Connection facts are on the object (`ws.path`, `ws.headers`,
  `ws.path_params`, `ws.subprotocols`, `ws.client`, `ws.connection`), as is
  handshake state (`ws.accepted`, `ws.client_disconnected`, `ws.close_code`).

  The parameter is resolved by annotation first, so `ws: WebSocket` works
  under any name; un-annotated, `ws` and `websocket` are recognised, and a
  `Connection` can be injected alongside.  A parameter that is neither is a
  `TypeError` **at registration**, not on the first connection.

  **The raw `(conn, receive, send)` form is not deprecated** and keeps
  working unchanged — supported for at least a year past this release, with
  no removal planned.  A route is classified once, at registration, by
  whether its signature contains both `receive` and `send`, so the raw form
  reaches the router untouched and pays nothing.  The object's methods emit
  exactly the events the raw form sends by hand: framing, fragmentation, and
  close semantics are identical, and the object is built once per
  *connection*, not per message.

- **`blackbull.middleware.websocket` composes with the WebSocket object.**
  The middleware accepts the handshake before the handler runs, which would
  otherwise leave the object waiting for a `websocket.connect` that had
  already been consumed — and reading the client's first *message* as the
  handshake, silently dropping it.

  Handshake state is now recorded on the connection by whichever layer
  completes it, and read by the others.  A `WebSocket` built downstream of
  the middleware starts already-accepted; a bare `await ws.accept()` in that
  handler is a tolerated no-op, so the same handler body works with or
  without the middleware on the route.  `await ws.accept('chat')` (or extra
  headers) raises instead of silently dropping the request, since the 101
  has already gone out.  If the handler closes the connection itself, the
  middleware no longer appends a second close frame.

  Middleware that takes the handshake off the receive channel records it
  with one of two calls, which are **not** interchangeable:
  `blackbull.websocket.mark_handshake_accepted(conn)` when it also sent
  `websocket.accept`, or `mark_connect_consumed(conn)` when it only read the
  connect event and left accepting to the handler (the shape an auth
  middleware wants — `examples/ChatServer/chatserver.py`'s `auth_mw` does
  this).  Marking a merely-consumed connection as accepted would make the
  handler skip its own `accept()` and leave the client hanging on a
  handshake nobody completed.  Omit both and the object raises a
  `RuntimeError` naming each, rather than swallowing the message.

### Internal

- The pyright gate now also covers `blackbull/websocket.py`, the first
  framework module written *against* the ASGI message declarations rather
  than declaring them.  It caught a `client` property typed narrower than
  `Connection.client` actually is.  The scope-pin test was updated
  accordingly and now additionally refuses package *directories*, so the
  gate can grow by reviewed module but not drift into whole-repo checking.

- `blackbull.testing.WebSocketDisconnect` is re-exported from
  `blackbull.websocket` rather than defined separately.  Both meant "the
  other end closed", and two identically-named exception classes would mean
  an `except` written against one silently missing the other.  Existing
  `from blackbull.testing import WebSocketDisconnect` imports are unaffected.

## [0.62.0] — 2026-07-28

### Added

- **Typed `receive`/`send` message channel.**  The 19 ASGI 3.0 message
  shapes are now declared as `TypedDict`s keyed by a `Literal` `type` tag,
  with a union per direction — `ASGIReceiveEvent` (7 members) and
  `ASGISendEvent` (12) — and a callable alias per direction,
  `ASGIReceiveCallable` / `ASGISendCallable`.  All four are exported from
  `blackbull`; the individual shapes (`HTTPResponseStartEvent`,
  `WebSocketReceiveEvent`, …) are importable from `blackbull.asgi`.

  Because the unions are discriminated on `type`, comparing or `match`-ing
  against `event['type']` narrows to a single member, so a type checker can
  reject a wrong key, a wrong value type, or an event sent in the wrong
  direction.  It also catches the 0.43.2-class type-confusion bug — a
  `Response` object reaching a middleware `send` wrapper, which fails at
  runtime on `event['type']` — statically rather than at runtime.

  **This is a declaration-only change.**  The dicts on the wire are
  unchanged, nothing is validated or converted at runtime, and unannotated
  application code keeps working exactly as before.

- **Static type-check gate.**  `tests/architecture/test_typing_gate.py` runs
  pyright over a deliberately narrow scope (`blackbull/asgi.py` +
  `tests/typing/`) and asserts both directions: the narrowing proofs produce
  zero diagnostics, and every `# EXPECT-ERROR` line in the negative proof
  draws at least one.  Requires the `[testing]` extra (pyright is pinned
  exactly); skips cleanly when pyright is unavailable.

### Changed

- Project description and overview no longer call BlackBull an "ASGI 3.0
  framework".  Since Sprint 79/80 the internal representation is a native,
  typed `Connection` threaded end to end, and ASGI is an interop boundary
  (external ASGI hosts, `BB_FORCE_ASGI_SCOPE=1`) — the prose now says so.
  ASGI-prefixed *type names* are kept deliberately: they mark the boundary
  vocabulary, and the values they carry are spec-defined ASGI strings.

### Internal

- Per-request `send` wrapper closures are deliberately left unannotated.  A
  nested `async def` created inside a per-request factory rebuilds an
  `__annotate__` closure on every creation — measured at ~93 ns per closure
  on CPython 3.14, or ~0.23 µs/req (~3.4 % of in-process dispatch) across the
  three wrappers on the HTTP/1.1 path.  Annotations are only free on
  definitions evaluated once at import.  The accepted event shapes are
  documented in a comment at each site instead; `Compression`'s two wrappers
  are now covered by the same rule, making them slightly cheaper than in
  0.61.0.

### Fixed

- **HTTP/2 connection-window leak on Python 3.13+ (un-drained request
  bodies).**  When a handler responded without draining its request body —
  an early `401`/`413`, a validation reject, or simply ignoring the body —
  the un-consumed DATA bytes were meant to be credited back to the shared
  connection window as `WINDOW_UPDATE(0)` when the stream was released.  That
  replay is scheduled from a synchronous done-callback, preferring the
  connection `TaskGroup` and falling back to a bare loop task once the group
  is closing.  Both paths were handed the *same* coroutine object, and since
  CPython 3.13 `TaskGroup.create_task()` closes the coroutine before raising
  `RuntimeError` for an exiting/aborting group — so the fallback scheduled an
  already-closed coroutine, the task died with `cannot reuse already awaited
  coroutine`, and the credit was silently dropped on exactly the teardown
  path the fallback exists to serve.  The loss is cumulative: after 65535
  un-drained bytes the connection window is shut and every later request on
  that connection stalls.  Each `create_task` now gets its own coroutine.
  Unaffected on 3.11/3.12, where the interpreter does not close the
  coroutine — which is also why the repository's CI matrix (3.11 + 3.12)
  never saw it, despite 3.13 and 3.14 being declared-supported.

- `tests/integration/test_nginx.py` imported the optional `docker` /
  `testcontainers` extras at module scope, so a missing extra became a
  collection error that interrupted the whole pytest session instead of
  skipping one file.  Now guarded with `pytest.importorskip`, matching the
  sibling differential test.

## [0.61.0] — 2026-07-24

### Added

- **Native streaming request-body API — `Connection.stream()`.**
  `async for chunk in conn.stream()` async-iterates the request body
  chunk-by-chunk without ever buffering it, the non-accumulating
  counterpart to `conn.body()`/`.json()`/`.text()`. It measures identical
  to a raw-`receive` stream loop (zero framework overhead), so streaming
  handlers no longer have to drop out of the `Connection` idiom back to the
  raw ASGI triplet. Because the request body is a single drain, `stream()`
  is mutually exclusive with the buffering accessors — buffering after
  streaming (or vice versa) raises `RuntimeError` rather than silently
  returning a partial body; a mid-body disconnect raises
  `ClientDisconnected`. New `request.stream_body(receive)` underpins it.

### Fixed

- **v0.60.0 framework-overhead regression — resolved.** v0.60.0's native
  `Connection` refactor regressed the framework-overhead-bound HttpArena
  profiles ~18–25% on identical hardware (baseline / baseline-h2 /
  pipelined / limited-conn). Root cause was the per-request cost of the new
  object model (a second scope-dict representation built every request, a
  per-request reference cycle, and unconditionally-built access-log records
  and disconnect closures), not any single line. Recovered by: a
  direct-attribute dispatch-scope builder with a precomputed field list; a
  lazy scope view (`_LazyScope`) that serves ASGI keys straight from the
  backing `Connection` and never materializes a dict body on the
  self-hosted path; lazy `path_params`; eliminating the per-request
  reference cycle; and gating the per-request access-log record, the
  capturing-send wrapper, and the disconnect-detecting receive closure
  behind actual consumers (no listener → not built) on both the HTTP/1.1
  and HTTP/2 dispatch paths. Final same-instance EC2 A/B against v0.59.1
  came back mean +0.78% / median +0.52% — regression closed.
- **WebSocket-over-HTTP/2 access-log method.** RFC 8441 sessions now record
  their true `CONNECT` method in the access log (and expose it on
  `conn.method` to method-gating middleware) instead of a leftover `HEAD`
  placeholder, matching how HTTP/1.1 upgrades log their real `GET`. Routing
  and lifecycle events were already unaffected (they branch on
  `conn.type`).

### Internal

- **Full-native `Connection` dispatch (HTTP/1.1, HTTP/2, WebSocket).** The
  protocol actors now thread the typed `Connection` end-to-end; the ASGI
  `scope` dict is built only at a genuine ASGI boundary (external host,
  `BB_FORCE_ASGI_SCOPE`, or a handler/middleware that asks for it). The
  name "scope" is now reserved exclusively for real ASGI scope dicts —
  every internal `Connection` parameter formerly called `scope` was
  renamed. WebSocket is native too: no scope dict is threaded on the native
  WS path. No public API break — simplified and full `(conn, receive,
  send)` handler forms, the middleware contract, and lifecycle-event
  payloads are all preserved.
- **Connection allocation hygiene.** The HTTP/2 header parser
  (`parse_headers`) was restructured to construct the `Connection` once
  with the real `Headers` (removing a throwaway `Headers([])` built and
  discarded every request) under a uniform `None ⟺ malformed` contract,
  and the plain-HTTP branch now uses a lean `object.__new__` builder with a
  shared empty-extensions sentinel. `parse_headers()` itself is ~20–24%
  faster head-to-head; pinned by field-drift and sentinel-escape
  architecture tests. No throughput claim — shipped as hygiene.
- Examples, warm-up, and the WebSocket test session were migrated to the
  native `Connection` model.

## [0.60.0] — 2026-07-22

### Changed

- **Native `Connection` interface — the single internal request
  representation (Sprint 79).** A typed `Connection` dataclass
  (`blackbull.Connection`) is now BlackBull's one internal model of a
  request. The protocol actors (HTTP/1.1, HTTP/2, WebSocket) build it
  directly in their parsers, and the router, dispatcher, and handlers
  read it. The ASGI `scope` dict is demoted to a **derived** view,
  produced by `Connection.as_scope()` and consumed by
  `Connection.from_scope()` only where external compatibility needs it
  (uvicorn, `httpx.ASGITransport`/TestClient, third-party ASGI
  middleware). A `_CONNECTION_FIELDS` registry is the single source of
  truth for both conversions, so the two representations cannot drift;
  a `BB_FORCE_ASGI_SCOPE=1` dual-path lane forces the round-trip on
  every request even self-hosted, keeping the compat path honest. This
  is an **internal refactor with no public API break**: the middleware
  `(scope, receive, send, call_next)` contract, event `detail['scope']`
  payloads, `__call__` signature, and handler signatures are all
  preserved. On the self-hosted path the actor stashes the `Connection`
  it already built, so consumers reuse it with no re-conversion (no B1/B3
  regression).

### Deprecated

- **`blackbull.Request` → use `blackbull.Connection`.** The opt-in
  handler context object was renamed. `blackbull.Request` is now a
  deprecated alias of `Connection` that emits a `DeprecationWarning` on
  first attribute access and will be **removed no earlier than
  2027-08-01** (≥1-year migration window). Migrate by renaming the
  import and the parameter annotation — the members are identical, with
  one rename: the raw-scope escape hatch `request.scope` (a stored dict)
  becomes `conn.as_scope()` (a freshly-derived dict). Docs and examples
  now use `Connection` throughout; `examples/request_object.py` is
  renamed to `examples/connection_object.py`.

## [0.59.1] — 2026-07-20

### Fixed

- **gRPC server-streaming Watch flakiness (DEADLINE_EXCEEDED).** The
  H2 sender's trailers-coalescing optimisation buffered the first DATA
  frame when `trailers: True` was set on the response start, deferring
  the write until trailers arrived (the unary-gRPC fast path).  For
  server-streaming handlers that park after the first yield (health
  Watch, chat streams), the buffered frame was never flushed, starving
  the client.  Added a `call_soon` auto-flush: if trailers (or a
  second body chunk) don't arrive within the same event-loop iteration,
  the buffered body is flushed immediately.  The unary coalescing path
  is preserved — synchronous trailers still combine into a single
  write.  (#173)

## [0.59.0] — 2026-07-19

### Added

- **QUERY normative response semantics (RFC 10008, Sprint 78 P2).** New
  `accept_query=[...]` route option declaring the request media types a
  QUERY route accepts. It drives an **`Accept-Query`** response header (an
  RFC 9651 Structured Field list, serialized once at registration) on the
  route's responses, and **Content-Type enforcement** on QUERY requests:
  **400** when the media type is missing, **415** when unaccepted (the 415
  carries `Accept-Query` so the client can correct). Media-type matching
  ignores parameters and is case-insensitive. New `blackbull.UnprocessableQuery`
  exception (an `HTTPException` subclass) for handlers to raise → **422**.
  All three statuses flow through the normal error-router path; enforcement
  targets QUERY only, while other methods on the route still receive the
  header. Guide section extended; 16 new tests (unit + HTTP/1.1 wire).
- **HTTP QUERY method support (RFC 10008, Sprint 78 P1).** First-mover
  support for the first new standard HTTP method since PATCH — safe,
  idempotent, cacheable, with a request body.  New `blackbull.QUERY`
  plain-string constant (`http.HTTPMethod` has no member before Python
  3.16; `StrEnum` equality keeps string-registered routes matching a
  future enum member with no migration).  Routing, body access
  (`body: bytes` / `Request.json()`), 405 `Allow` advertisement, and the
  four request-lifecycle events are pinned by tests over HTTP/1.1,
  HTTP/2, and `TestClient`; the experimental clients round-trip
  `request('QUERY', ...)` on both protocols.  New guide section
  *The QUERY method (RFC 10008)* in [Routing](docs/guide/routing.md).
  QUERY routes stay out of the generated OpenAPI document — 3.1 has no
  `query` operation (3.2 does; revisit when the emitter moves) — and are
  never faked as another operation.

### Fixed

- **HTTP/2: spurious `http.disconnect` on body-less requests.** When
  HEADERS carried END_STREAM and the connection closed before the
  handler's first `receive()`, `HTTP2Recipient.put_disconnect()`
  allocated the event queue and thereby disabled the synthetic
  empty-body fast path — a body-reading handler (QUERY/POST with an
  empty body) saw `http.disconnect` instead of the complete empty
  `http.request`.  The synthetic event is now delivered first; the
  disconnect follows it.  Invisible for GET handlers, which never read
  the body.

### Internal

- **Generic per-route hooks replace the QUERY dispatch special-case.**
  `accept_query` enforcement is now implemented through two
  method-agnostic handler hooks — `_bb_response_headers` (extra headers
  injected on every response, success and central error alike) and
  `_bb_request_guard` (a pre-dispatch callable that may reject with an
  `HTTPException`) — which `BlackBull._dispatch` applies uniformly. The
  dispatcher carries no method-specific branch; all QUERY-specific logic
  lives in the guard built at registration. Reusable by any future
  per-route response-header or request-guard feature. No behaviour change.

## [0.58.0] — 2026-07-19

### Added

- **Edge inference API server positioning (Sprint 76).** New guide page
  [Edge inference serving](docs/guide/edge-inference.md): the one-process
  edge serving shape — SSE token streaming with HTTP/2 multiplexing for
  interactive clients beside MQTT device ingest and `$share/…` work queues
  for devices — including how to run a real (CPU-bound) model off-thread,
  and an explicit when-this-fits / when-it-doesn't section. Ships with the
  runnable, dependency-free `examples/edge_inference.py` (fake token model,
  browser `EventSource` demo, tap-fed `/devices` + `/status`). README,
  `docs/index.md`, the guide overview, and *Why BlackBull?* carry the
  positioning hooks.

### Changed

- **Router trie fast path (Sprint 77).** A static-path map short-circuits
  literal routes, method matching is a `frozenset` membership test, and the
  trie walk is iterative with the regex-fallback skipped when a route has no
  regex segments. Parameterised `_resolve` dropped ~1555→959 ns and static
  routes ~1244→232 ns, with no throughput regression on any profile (EC2).

### Removed

- **`ConnCoalescer` / `BB_H2_CONN_BUFFER_US` (Sprint 77).** The opt-in
  connection-level TCP-segment coalescer (shipped default-off in v0.48.0) is
  removed. Given its designed killer case — gRPC unary fan-out, one connection
  × 200 concurrent RPCs, on a real network path — the mechanism fired (~20%
  fewer TCP segments) but produced no throughput or tail-latency gain (RPS
  +2.8%, p99 −4%, both inside noise): gRPC/HTTP-2 runs with `TCP_NODELAY`, so
  there is no delayed-ACK stall to eliminate, and natural HTTP/2+TCP batching
  already packs most responses per segment. With no effective occasion on any
  measured workload, it is removed rather than kept as dormant opt-in weight.
  **Breaking**: setting `BB_H2_CONN_BUFFER_US` now has no effect (it defaulted
  to off, so no default behaviour changes).

### Fixed

- **Python 3.11 compatibility restored (Sprint 77).** `HTTPStatus.is_client_error`
  and `.is_server_error` are Python 3.12-only, so importing BlackBull actually
  raised on 3.11 (regressed in 0.48.1, present through 0.57.0). Replaced with
  integer-range helpers; added a 3.11 CI lane and a source grep-guard so the
  floor can't silently break again. PyPy (3.11-compat) is now usable.
- **MQTT read-loop cancellation hang on Python 3.11 (Sprint 77).** On 3.11,
  `asyncio.wait_for` can swallow an external `CancelledError` when the wrapped
  read completes in the same event-loop iteration the cancel arrives, wedging
  the connection task. Switched the keep-alive-bounded read to `asyncio.timeout`,
  which re-raises the outer cancel. 3.12 was unaffected.

## [0.57.0] — 2026-07-17

### Added

- **MQTT 5 shared subscriptions** (§4.8.2, Sprint 75). Subscribers naming the
  same `$share/{ShareName}/{filter}` pair form a share group; each matching
  message is delivered to exactly **one** connected member, round-robin per
  group, at that member's granted QoS. Non-shared subscriptions keep broadcast
  semantics; disconnected members are skipped while any member is connected
  (§4.8.2.3); shared subscriptions never receive retained messages; `No Local`
  on a shared subscription is a Protocol Error (DISCONNECT `0x82`, §3.8.3.1).
  This also resolves the standing CONNACK wrinkle: the
  `shared_subscription_available` property (0x2A) stays absent, which per
  MQTT 5 means *supported* — now true.
- **Retain Handling 1** (§3.3.1.3, Sprint 75). `retain_handling=1` now delivers
  retained messages on SUBSCRIBE only when the subscription did not previously
  exist (it previously behaved like 0).
- **TLS for raw protocol bindings** (Sprint 75).
  `app.raw_handler(name, port=…, tls=True)` /
  `MQTTExtension(port=8883, tls=True)` serve the binding's port through the
  server's TLS machinery (`mqtts://` without a fronting proxy). Cleartext
  remains the default; `tls=True` with no certificate configured fails fast at
  startup.

### Changed

- **Spec change**: a well-formed `$share/…` SUBSCRIBE is now granted instead of
  rejected with `0x9E SHARED_SUBSCRIPTIONS_NOT_SUPPORTED` (the deliberate
  Sprint 70 fence). Malformed `$share` forms (missing/empty ShareName or filter
  portion, wildcard ShareName) are rejected per-entry with `0x8F`;
  `validate_topic_filter` now rejects an empty filter portion (`$share/g/`).
- Several raw bindings registered with `port=0` (OS-assigned) each now get
  their own listener; previously the port-keyed view collapsed them to one.

## [0.56.0] — 2026-07-16

### Added

- **Query params in the simplified handler model** (Sprint 74). A handler
  parameter that is neither a path param, `body`, `scope`, `Request`, a
  dataclass body, nor `Depends(...)` now resolves from the query string,
  coerced to its annotation (`str`/`int`/`float`/`bool`, optionally
  `| None`; unannotated → `str`): `async def search(q: str, page: int = 1)`
  handles `/search?q=bull&page=2` directly. A default makes the param
  optional; a missing required key or a failed coercion is answered with
  **400**, never a 500. Repeated keys: last occurrence wins. Classification
  happens once at registration — handlers that declare no query params keep
  the exact wrapper they had before. Query params are emitted in the
  generated OpenAPI spec (`in: query`, schema from the annotation,
  `required` from default-presence). Deferred since Sprint 41.
- **`Depends` — per-request provider injection** (Sprint 74). New module
  `blackbull.di`; `db=Depends(get_db)` as a parameter default injects the
  provider's value per request. Async-generator providers yield the value
  and tear down **after the response is sent** (`AsyncExitStack`-backed,
  LIFO across providers, exception-safe); plain async/sync callables
  inject a value with no cleanup. `use_cache=True` (default) shares one
  instance per request across parameters naming the same provider. v1
  fences: providers take no parameters, nested `Depends` is a
  registration-time `TypeError`, simplified handlers only. The
  anti-FastAPI design point: everything resolves at registration time, so
  handlers without `Depends` gain zero per-request cost (FastAPI runs
  `solve_dependencies()` + two `AsyncExitStack`s per request even with no
  dependencies declared). New guide page *Dependency injection*.

### Changed

- **Dev-mode error page: no traceback for 4xx `HTTPException`s**
  (Sprint 74). A client fault the framework itself diagnosed — missing
  required query parameter, malformed JSON body — now renders as status +
  detail line in development mode, without the Python traceback (the
  frames only showed framework internals; the detail line is the
  actionable part). 5xx `HTTPException`s and unexpected exceptions keep
  the full traceback. Mirrors the dispatcher's existing quiet-log rule
  for the same errors.
- **Simplified-handler registration contract** (Sprint 74). A scalar
  parameter that matches no other category is now a query param instead of
  a registration-time `TypeError`; the fail-fast `TypeError` remains for
  unsupported annotations (containers, arbitrary classes). A path param
  declaring a default now draws a registration-time `UserWarning` (the
  path value always wins; the default suggests a query param was
  intended).

## [0.55.0] — 2026-07-16

### Added

- **RFC 9651 Structured Field Values** (Sprint 73). New module
  `blackbull.protocol.structured_fields` implements the full RFC 9651 §4
  parse/serialise algorithms — Items, Lists, Dictionaries, Inner Lists,
  Parameters, and all eight bare-item types (Integer, Decimal, String,
  Token, Byte Sequence, Boolean, Date, Display String) — with zero new
  dependencies, verified against all 2,135 cases of the
  [httpwg/structured-field-tests](https://github.com/httpwg/structured-field-tests)
  conformance suite (vendored under `tests/conformance/structured_fields/`).
  New wrapper types `Token`, `DisplayString`, and `Date` preserve wire-form
  distinctions RFC 9651 requires.
- **`Headers.get_sf_item` / `get_sf_list` / `get_sf_dict`** — parse a header
  as a Structured Field of the given type, combining multiple field lines
  per RFC 9651 §4.2 and returning `None` for absent or malformed fields
  (strict parsing: a malformed field is ignored in its entirety).
  Documented in the new guide page *Structured Fields*.

- **Protocol translation hub showcase** (Sprint 73, C4). New example
  `examples/translation_hub.py` and guide page *Protocol translation hub*:
  MQTT → WebSocket, MQTT → SSE, and REST → gRPC translation in a single
  BlackBull process — devices publish over MQTT :1883, browsers watch the
  same feed over WS and SSE on :8000, and one gRPC service answers both
  native gRPC clients and a REST route that maps `GrpcStatus` to HTTP
  statuses in-process. No gateway, no sidecar, no config file.

### Fixed

- **MQTT `@mqtt.on_message` taps were dead in actor mode** (the default).
  `MQTTExtension` builds its `TapActor` at construction, but the actor
  compiled the tap table immediately — before any `@mqtt.on_message`
  decorator had run — so in `tap_mode='actor'` every tap registered
  afterwards (i.e. all of them, in normal usage) silently never fired.
  `TapActor` now tracks the live handler list and recompiles when it
  grows, matching the at-call-time semantics `iter_subscriptions`
  documents. Found while verifying the translation-hub showcase
  end-to-end; regression tests cover both registered-before-start and
  registered-while-running taps.
- **RFC 9218 priority parsing is now spec-strict** (`parse_priority_field`,
  used for both the `Priority` request header and the HTTP/2
  `PRIORITY_UPDATE` frame payload, which was previously handled by an
  ad-hoc splitter). Out-of-range or mistyped members are ignored per
  RFC 9218 §4 — `u=9` now falls back to the default urgency 3 instead of
  being clamped to 7 — explicit `i=?1` / `i=?0` booleans are honoured, and
  a field value that fails RFC 9651 parsing yields the defaults.

## [0.54.0] — 2026-07-16

### Fixed

- **HTTP/2 `:authority` is now validated and surfaced as the `host` header**
  (#150, Sprint 72; RFC 9113 §8.3.1). Previously the pseudo-header was parsed
  but never checked nor mapped into the ASGI scope, so
  `request.headers.get(b'host')` returned `None` for virtually every HTTP/2
  client (grpcio, browsers, and `curl --http2` send `:authority` without a
  literal `Host`), and the server-push path silently synthesised
  `:authority = localhost`. Now: an `http(s)` request carrying neither
  `:authority` nor `Host` is rejected as malformed (`RST_STREAM
  PROTOCOL_ERROR`), as are authorities containing userinfo, RFC 3986 §3.2
  delimiters, or whitespace, and multiple `Host` fields — the same grammar
  HTTP/1.1 has enforced since 0.49.3. A valid `:authority` replaces any
  literal `Host` in `scope['headers']` (mirroring H1's
  absolute-form-overrides-Host semantics) on both the request path and the
  RFC 8441 WebSocket path, so the same handler sees the same headers under
  either transport. Plain CONNECT is untouched (§8.5 tunnel semantics).
- **Experimental H2 client: send windows now honour the server's
  `SETTINGS_INITIAL_WINDOW_SIZE` for late streams** (#151, audit 1.20a).
  A sender created after the SETTINGS exchange started at the RFC-default
  65535 and could stall or overrun under a non-default window; both server
  and client now seed the per-stream window at `HTTP2Sender` construction
  (the shared helper audit item 2.11 called for).
- **Experimental WS-over-H2 client: third divergent frame parser deleted**
  (#151, audit 1.20b/F.2). `WebSocketH2Session` now reuses the same
  `WebSocketRecipient`/`FragmentAssembler` stack as the server and the H1
  client, gaining fragmentation reassembly, transparent PING→PONG (masked,
  as a client's must be), UTF-8 validation, FIN/RSV/MASK enforcement, and
  the frame-size cap. The `(opcode, payload)` `receive()` API is unchanged.
- **Experimental WS clients: `close()` completes the closing handshake**
  (#151, audit 1.20c). Both `WebSocketSession.close()` and
  `WebSocketH2Session.close()` now drain the peer's echoed CLOSE (bounded
  by a new `drain_timeout` parameter, default 5 s — a silent peer cannot
  hang `close()`) and cancel the background reader via the new public
  `WebSocketRecipient.shutdown()`, so no task outlives the session.

### Removed

- **The deprecated in-tree `Session` middleware** (`blackbull.middleware.Session`
  and its `SessionMiddleware` alias). Deprecated since 0.38; both removal
  floors (release count ≥ v0.41.0, calendar ≥ 2026-07-14) have passed.
  **Migration**: install
  [`blackbull-session`](https://github.com/TOKUJI/blackbull-session) and swap
  `app.use(Session(...))` for `SessionExtension(app, ...)` — handlers keep
  reading and writing `scope['session']` unchanged, and `BB_SESSION_SECRET`
  is still honoured by the replacement package.

### Internal

- `bench/conformance/autobahn_run.sh` — `CASES='1.*'` (comma-separated
  patterns accepted) now actually subsets the Autobahn run instead of being
  silently ignored; a per-run config is rendered with only `"cases"`
  substituted, so the unset-`CASES` CI job still runs the full 517-case
  suite (#152, audit P.2).

### Docs

- `docs/about/rfc9113-implementation.md` §8.3 — documents the new
  `:authority` validation and ASGI `host` mapping.

## [0.53.4] — 2026-07-15

### Fixed

- **`request_completed` now reports the real status / byte count when a
  global middleware buffers the response** (issue #145). The event was
  emitted from `BlackBull._dispatch`, but an `app.use` middleware wraps
  *outside* `_dispatch` — so when `Compression` buffered the body and only
  sent the response after `call_next` returned, the event fired first and
  carried the `AccessLogRecord` placeholders (`status='-'` → `0`,
  `response_bytes=0`). Any request whose `Accept-Encoding` selected an
  installed codec (e.g. every browser visit, via `gzip`) was affected;
  this was present in every release with global-middleware support, not a
  0.53.3 regression. Emission moved to `BlackBull.__call__`, after the
  global middleware chain returns (still exactly once per request, still
  before `scope_completed`). A consequence: responses short-circuited by a
  global middleware (e.g. a cache hit answered without calling
  `call_next`) now fire `request_completed` too — previously they were
  invisible to it.

## [0.53.3] — 2026-07-14

Sprint 70 — MQTT 5.0 broker hardening: the `1.19` correctness cluster (eight
RFC-conformance bugs). All localised to the broker/connection actors on the MQTT
port; the HTTP request path is untouched. No API change.

### Fixed

- **Keep-alive is now enforced** (§3.1.2.10, bug 1.19a). A connection that
  sends no packet within 1.5× its negotiated Keep Alive is treated as an
  abnormal disconnect: its Will fires and the connection closes, so a dead peer
  (crashed client, half-open NAT) no longer holds its connection, session and
  Will indefinitely. Keep Alive `0` disables the check.
- **Session takeover** (§3.1.4, bug 1.19b). A second `CONNECT` for a
  `client_id` that is already connected now disconnects the previous connection
  with `DISCONNECT` reason `0x8E` (Session taken over) and closes it, instead of
  leaving the old connection live with its socket open.
- **QoS 2 inbound de-duplication** (§4.3.3, bug 1.19c). A retransmitted (`DUP`)
  `PUBLISH` whose Packet Identifier is already in the PUBREC-sent state is
  acknowledged with `PUBREC` but no longer re-delivered or re-retained.
- **Topic validation on `PUBLISH` / `SUBSCRIBE`** (§3.3.2.1 / §4.7, bug 1.19d).
  A `PUBLISH` with a wildcard/null Topic Name is rejected (`0x90`, or a
  `DISCONNECT` for QoS 0) and neither routed nor retained; a malformed Topic
  Filter in `SUBSCRIBE` is rejected per-entry with `0x8F`. (Wires in the
  previously-dead `validate_topic_name` / `validate_topic_filter`.)
- **`No Local` and `Retain As Published` subscription options are honoured**
  (§3.8.3.1 / §3.3.1.3, bug 1.19e). A client's own message is no longer echoed
  back to it on a `No Local` subscription, and the publisher's `RETAIN` flag is
  forwarded only when a matching subscription set Retain As Published.
- **Shared subscriptions are rejected explicitly** (§4.8, part of 1.19e). A
  `$share/...` filter now gets `0x9E` (Shared Subscriptions not supported)
  rather than being silently broadcast to every group member (which violated
  the §4.8.2 load-balancing contract).
- **QoS 2 outbound + `DUP` replay on reconnect** (§4.4 / §3.3.1.1, bug 1.19f).
  A session-present reconnect now retransmits unacknowledged outbound QoS 1 and
  QoS 2 `PUBLISH` frames with `DUP=1`, and re-drives a `PUBREL` still awaiting
  `PUBCOMP`.
- **Malformed `CONNECT` no longer drops the connection on `IndexError`**
  (§1.5.5 / §4.13, bug 1.19g). A `CONNECT` truncated before its fixed header
  fields — and any packet whose body is inconsistent with its declared
  Remaining Length — now decodes to `MQTTDecodeError`, which the framer resyncs
  on, instead of an `IndexError` unwinding the read loop or an `IncompletePacket`
  stalling it forever.
- **Packet-identifier allocation skips in-flight ids** (§2.2.1, bug 1.19h).
  `_alloc_pid` no longer hands out an identifier still awaiting acknowledgement
  in either QoS>0 outbound bucket.

### Changed

- MQTT QoS-2 flow state is now modelled with named constants and helpers
  (`_qos2_accept_inbound`, `_replay_pending`); outbound QoS-2 entries keep the
  `PUBLISH` packet (not just a state string) so §4.4 replay can retransmit it
  (refactor 2.9).
- `docs/guide/mqtt.md` capability table updated for the above (shared
  subscriptions now listed as rejected; keep-alive/session-takeover/QoS-2
  behaviours documented).

## [0.53.2] — 2026-07-14

Sprint 69 follow-on — two caching-correctness bugs found reviewing the v0.53.1
`1.21` cluster. Both are shared-cache poisoning gaps in the `Compression` +
`Cache` interaction; localised, no API change.

### Fixed

- **`Compression` now emits `Vary: Accept-Encoding` on compressible responses
  it does not actually compress** (bug 1.21f). v0.53.1 added the header only on
  the *successful-compression* path; a first request that hit one of the other
  exit paths — no codec the client accepts (a `curl`/health-check/bot sending no
  `Accept-Encoding`), body under `min_size`, or the executor-offload cap — served
  a compressible body with no `Vary`, which a downstream `Cache` then stored
  under the bare key and replayed to a later client that *did* accept an
  encoding. The `Vary` decision is now made once at the `ResponseStart` decision
  point (compressible Content-Type and no pre-existing `Content-Encoding`), so
  every exit path carries it; the no-matching-codec path gets the same via a
  lightweight send wrapper. Uncompressible types and already-encoded responses
  still get no `Vary`.
- **`Cache` no longer orphans variant entries after LRU eviction** (bug 1.21g).
  The variant bookkeeping (a response's `Vary` field names) and the cached
  entries were two independent `OrderedDict` LRUs; evicting the bookkeeping
  first left the entries unreachable (lookups rebuilt the key with empty vary
  fields and never matched), degrading to spurious misses. The store is now a
  single LRU of per-URL buckets, each holding its `Vary` fields *beside* its
  per-variant entries — so the fields can never outlive the entries they key.
  `max_entries` now bounds distinct URLs; each bucket bounds its own variants
  (16) against a hostile `Accept-*`-varying peer.

## [0.53.1] — 2026-07-13

Sprint 69 (first half) — the `1.21` middleware-correctness cluster: five
localised RFC-conformance fixes across the shipped middleware, plus native
conditional-request support in `StaticFiles`. (The in-tree `Session` removal,
the sprint's other half, is calendar-gated and lands separately as `v0.54.0`.)

### Added

- **`StaticFiles` conditional requests.** `StaticFiles` now emits a strong
  `ETag` (from the file's mtime + size) and a `Last-Modified` header on every
  response, and honours `If-None-Match` / `If-Modified-Since` with a
  `304 Not Modified` — answered before the body is read, so revalidating a
  large asset costs no disk I/O. `If-None-Match` takes precedence over
  `If-Modified-Since` (RFC 9110 §13). A new `conditional=` argument on
  `app.static(...)` / `StaticFiles(...)` (default `True`) disables it; the
  `blackbull serve --no-etag` flag now drives that argument directly and no
  longer needs the `Cache` middleware to provide validators (bug 1.21d).

### Fixed

- **`Compression` now emits `Vary: Accept-Encoding` on compressed responses**
  (RFC 9110 §12.5.5, bug 1.21a). Without it a shared cache could replay a
  brotli/gzip body to a client that sent `identity` / no `Accept-Encoding`.
  Folds into any existing `Vary`; a pre-existing `Vary: *` is left untouched.
- **`Cache` is now variant-aware** (bug 1.21b). The response `Vary` fields are
  folded into the cache key so a negotiated variant (e.g. an encoding behind
  `Compression`) is no longer served to a client that asked for a different
  one; a `Vary: *` response is passed through unstored.
- **`StaticFiles` no longer returns `500` on a malformed `Range` header**
  (bug 1.21c). A bad byte spec (`bytes=abc-def`), a non-`bytes` unit, or a
  multi-range set is ignored and the full file served `200` (RFC 9110 §14.2);
  a well-formed but unsatisfiable range still returns `416`.
- **`TrustedProxy` parses multi-element RFC 7239 `Forwarded` headers
  correctly** (bug 1.21e). Elements are split on `,` first (leftmost element
  honoured); previously a chained `for=a, for=b` folded the second element's
  `for=` into `scope['client']`.

### Docs

- Static-files and middleware guides document the new conditional-request
  support, the malformed-`Range` handling, and the now variant-aware `Cache`.

## [0.53.0] — 2026-07-13

Sprint 68 — ASGI path-decoding conformance (percent-decoding + RFC 3986
`;` sub-delimiter preservation), a router lookup-cache refactor, and gRPC
server-reflection `v1` (shipped in the sibling `blackbull-protobuf 0.2.0`).

### Added

- **`Router(cache_max=…)`** — the route lookup-cache bound is now a
  constructor argument (default 2048; `0` disables caching). Internally the
  cache get/set mechanics moved into a swappable `_cache_get` / `_cache_set`
  method pair so a future cache strategy can be dropped in without touching
  `__getitem__` or `_resolve`. No behaviour change at the default.

### Fixed

- **ASGI conformance: `scope['path']` is now percent-decoded; `raw_path`
  no longer includes the query string** (Sprint 68 W1). Neither transport
  decoded percent-escapes, so `/a/%6A/b` routed and echoed as `/a/%6A/b`
  instead of `/a/j/b`, and RFC 3986 §2.3-equivalent URIs (`%41` vs `A`)
  were treated as distinct routes. `scope['path']` is now decoded once at
  scope construction on every transport (HTTP/1.1, HTTP/2, WebSocket
  upgrade, RFC 8441 extended CONNECT, and HTTP/2 push scopes), gated on a
  `%` scan so escape-free targets keep the previous fast path. Decode
  semantics match uvicorn: UTF-8 `unquote` with `errors='replace'`, `+`
  stays literal, malformed escapes (`%ZZ`) pass through unchanged.
  **Migration**: an encoded `%2F` now decodes to a real `/` before
  routing, so it participates in path segmentation (uvicorn/Starlette
  behaviour) — applications that must distinguish `a%2Fb` from `a/b`, or
  that match encoded paths literally, should read `scope['raw_path']`.
  On HTTP/1.1, `raw_path` is now the undecoded **path component only**
  (query string excluded, per the ASGI spec) — previously it carried the
  full request target including `?query`.
- **ASGI conformance: an RFC 3986 `;` path sub-delimiter is now preserved
  in `path` and `raw_path` on both transports** (Sprint 68). The obsolete
  RFC 2396 `;params` grammar was being split off — on HTTP/1.1 from `path`
  only, and on HTTP/2 from *both* `path` and `raw_path` (`urlparse` strips
  `;params` from a scheme-less path; `raw_path` was derived from it). So
  `/cart;sid=abc` arrived as `path='/cart'` on both, and H2 `raw_path`
  wrongly lost the sub-delimiter too — leaving `path` and `raw_path`
  describing different resources. Both now keep it (`path='/cart;sid=abc'`,
  `raw_path=b'/cart;sid=abc'`), matching uvicorn and RFC 3986, which treats
  `;` as an ordinary path character. **Migration**: applications that
  relied on the old `;params` stripping for path-segment delimiting must
  split on `;` themselves. (H2 now uses `urlsplit` instead of `urlparse`;
  both changes are also a small speed-up.)
- **WebSocket test client (`WebSocketTestSession`) now decodes `scope['path']`
  and encodes `raw_path` as UTF-8** (Sprint 68), matching what the real
  server produces — previously the test client left `path` percent-encoded
  and encoded `raw_path` as latin-1, so tests could pass against scope
  values the server would never emit.

## [0.52.0] — 2026-07-12

Sprint 67 — gRPC bidi correctness closeout plus an inter-sprint perf fix
that resolves the long-standing v0.33.1 → v0.51.0 HttpArena regression.

### Fixed

- **Send-path size gate — `writelines` regression** (inter-sprint, releases with
  Sprint 67).  `BaseSender._write_many` now joins parts totalling ≤ 32 KiB and
  sends them via a single `write()`; only larger payloads use vectored
  `transport.writelines`.  Root cause of the v0.33.1 → v0.51.0 HttpArena
  regression (echo-ws −8~−20 %, plaintext HTTP/1.1 −4~−8 %): on CPython's
  selector transport, `writelines` costs more than the small memcpy it avoids
  (per-part `memoryview` allocations + `sendmsg` setup), and under backpressure
  it attempts a send and re-registers the writer on **every** call.  The
  transport strategy now lives in one place (`BaseSender`); protocol senders
  keep expressing *what* they have via `_write_many((head, body))`.  Breakeven
  measured at 16–64 KiB (join wins below, vectored wins above); local A/B
  recovers the full HTTP/1.1 baseline regression (−10 % CPU/request vs
  v0.51.0).  See `.claude/planning/recommendations/protocol-layer-audit-2026-07-12.md`.
- **HTTP/2 bidi stream state: client END_STREAM now half-closes, not
  closes** (RFC 9113 §5.1). `Stream.on_data_received(end_stream=True)`
  transitioned straight to CLOSED, so a legitimate `WINDOW_UPDATE` sent by
  the client after ending its request body — routine for gRPC bidi
  streaming, where the client keeps crediting the server's in-flight
  response DATA — was answered with `RST_STREAM(STREAM_CLOSED)`, tearing
  down the live stream (the `test_echo_each_message` RST(5) flake). The
  stream now enters HALF_CLOSED_REMOTE, from which WINDOW_UPDATE /
  PRIORITY / RST_STREAM remain legal; full CLOSED is still reached when
  the response completes (done-callback prune) or via RST_STREAM. Also
  removed the dead `Stream.mark_locally_closed` (never called; its
  docstring claimed otherwise).

### Changed

- **Connection-accept path trims** (inter-sprint, releases with Sprint 67;
  follow-up to the `limited-conn` churn analysis).  (1) The cleartext
  protocol-detection order is now cached on `ProtocolRegistry`
  (`detection_order`, rebuilt on `register()`) instead of being reallocated
  per accepted connection — HTTP-only apps pay nothing per connection for the
  raw-protocol machinery.  (2) Per-connection ids are generated by
  `blackbull.server.conn_id.new_connection_id()` — a 12-hex per-process
  random prefix plus an 8-hex monotonic sequence — replacing per-connection
  `uuid.uuid4()` on the accept path and the 4-byte `os.urandom` fallback in
  `cap_log`, whose birthday-bound collision odds were real at churn scale
  (~1.2 % at 10 k concurrent connections).  Ids remain opaque hex strings;
  width changes from 32/8 to 20 characters.
- **One connection id per connection.**  Previously the same TCP connection
  could carry up to three unrelated ids: the accept-time id
  (`ProtocolContext`), a second minted by the cap-hit counter, and a third
  uuid4 minted at WebSocket upgrade — cap-hit records could not be
  correlated with lifecycle events.  The accept-time id now flows into the
  cap-hit counter (`ConnectionActor`) and into `scope['_connection_id']` on
  both the HTTP/1.1 upgrade and RFC 8441 paths, so `websocket_connected` /
  `websocket_disconnected`, cap-hit records, and `ProtocolContext` all
  report the same id.  WS event `connection_id` format changes from uuid4
  to the 20-hex unified form (docs updated; it was always documented as
  opaque/correlation-only).

## [0.51.0] — 2026-07-12

Sprint 66 — the protobuf side of the gRPC story, shipped as the new
optional [`blackbull-protobuf`](https://github.com/TOKUJI/blackbull-protobuf)
package (`pip install 'blackbull[protobuf]'`) plus the core hooks it plugs
into. Core `blackbull.grpc` stays protobuf-free; the raw-bytes handler path
is untouched.

### Added

- **`blackbull[protobuf]` extra** → the new `blackbull-protobuf` 0.1.0
  package: `add_servicer` (object-typed handlers from generated `*_pb2`
  modules, all four RPC shapes), `enable_reflection`
  (`grpc.reflection.v1alpha` — grpcurl/Postman work with no local
  `.proto`), `enable_health` (`grpc.health.v1` `Check` + `Watch` behind a
  settable status map), and `abort_with_details` (`google.rpc.Status`
  details in the `grpc-status-details-bin` trailer).
- **`GrpcContext.trailing_metadata()`** — public getter for the trailing
  metadata set so far, so helper packages compose with (rather than
  clobber) what the handler already set.
- **Protobuf-layer interop conformance**
  (`tests/conformance/grpc/test_grpc_protobuf_interop.py`): real grpcio
  client packages — `ProtoReflectionDescriptorDatabase` (reflection-only
  dynamic invocation), the official health gencode stub, and
  `grpc_status.rpc_status.from_call` — drive BlackBull over a real h2c
  socket in the `grpc-interop` CI job.

### Fixed

- **gRPC error paths now deliver `set_trailing_metadata`.** Previously the
  error writers emitted only `grpc-status`/`grpc-message` and dropped the
  context's trailing metadata on every non-OK shape (Trailers-Only,
  after-HEADERS, mid-stream, unhandled exception, deadline). grpcio
  delivers it regardless of outcome — and the rich error model's
  `grpc-status-details-bin` trailer depends on that.
- **Interactive server-streaming no longer withholds messages.** The
  write-coalescing batcher (v0.49.x streaming-collapse fix) flushed a
  buffered message only when the *next* message completed, so a producer
  parked indefinitely between yields — a `grpc.health.v1` `Watch`, a chat
  stream — never delivered its first message (caught by the new
  health-Watch interop test). The pull-timing heuristic is replaced by a
  loop-idle flusher task that runs exactly when the producer suspends;
  synchronous bursts keep batching into single DATA frames (pinned:
  ≤5 DATA events for a 1000-message burst), and flushes are
  lock-serialised so wire order matches yield order.

### Docs

- gRPC guide: new "Protobuf integration: `blackbull-protobuf`" section
  (servicers, grpcurl reflection flow, health map, rich errors).
- `KNOWN_LIMITATIONS.md`: "no protobuf codegen toolchain" resolved;
  remaining gap narrowed to reflection `v1alpha`-only + server-side-only.
- `SECURITY.md`: `blackbull/grpc/` and `blackbull/mqtt/` explicitly listed
  in scope; `blackbull-protobuf` reports accepted through either repo.

## [0.50.0] — 2026-07-11

Sprint 65 — a first-class, opt-in `Request` context object for HTTP
handlers, matching the convention gRPC (`GrpcContext`) and non-ASGI
protocol handlers (`ProtocolContext`) already follow. Perf-neutral
(EC2 HttpArena A/B, same instance, full 20 profiles: mean +0.13%
across 36 cells; gate cells baseline/512 −0.57%, baseline/4096
+0.59%, json/4096 +1.29%).

### Added

- **`Request` context object for simplified handlers** (`from blackbull
  import Request`). Declare `request: Request` under any parameter name —
  or the bare name `request` unannotated — and the router injects a
  per-request object exposing `method`, `path`, `headers`, `cookies`,
  `client`, `scheme`, and the awaitables `body()` / `json()` / `text()`.
  Phase 1 is the read surface, wrapping `scope` + `receive` over the
  existing `blackbull/request.py` free functions.
  - Detected by signature at registration time in `_adapt_handler` —
    no per-request reflection; handlers that don't declare it pay nothing.
  - `body()`/`json()`/`text()` cache a single drain of `receive`, shared
    with a coexisting `body: bytes` parameter — never double-drains.
  - New guide page, and `examples/request_object.py`.

### Fixed

- **`Router.validate()` no longer rejects the documented bare-`{param}`
  pattern.** A route like `{task_id}` with a typed handler annotation
  (`task_id: int`) is captured as `str` by the router and re-coerced to
  the annotation at call time by `_adapt_handler` — the tutorial pattern
  in `docs/getting-started/first-app.md` — but boot-time validation
  raised `ConfigurationError` on the spec/annotation mismatch under
  `app.run()`/`app.serve()`. The converter/annotation type-match check
  now applies only to explicit `{param:converter}` segments, where the
  router itself promises the converted type; those still fail fast at
  boot on a mismatch.
- **`blackbull.fault_injection` API reference link** — a docstring
  pointed at a git-ignored `.claude/` path and broke on the published
  API reference; it now points at the shipped docs.

### Removed (internal, no public API impact)

- **`BaseRouter`** (CodeQL alert #426) — an HTTP-shaped abstract stub
  with no consumer beyond `Router`'s inheritance clause and its own
  tests; MQTT's router shipped without it. `Router` stands alone.

## [0.49.4] — 2026-07-10

Sprint 64 — event-emission consolidation and dead-code purge. Perf-neutral
(EC2 HttpArena A/B, same instance: mean +1.0%, dispatch-path lanes
+3–4.6%). No new public API surface.

### Fixed

- **Request-lifecycle events fire exactly once per request**, under any
  transport (BlackBull's own HTTP/1.1 + HTTP/2 actors, uvicorn/hypercorn,
  `TestClient`). `request_received`, `before_handler`, `after_handler`, and
  `request_completed` are now emitted from a single choke point,
  `BlackBull._dispatch`, replacing per-actor emitters that double-fired
  `before_handler` on the production-server path and never fired
  `request_received` under `TestClient`. `test_extension_event_handler_is_fired`
  (a strict xfail since Sprint 40) now passes.
- **HTTP/2 `request_completed` details carry real wire fields.** HTTP/2 now
  publishes its access-log record the same way HTTP/1.1 does, so `status` /
  `response_bytes` / `duration_ms` are no longer `'-'`/`0` on that path.
- **`@app.route(path=re.compile(...))`** — the documented custom-regex form —
  no longer crashes at registration; route paths now accept `str | re.Pattern`.
- **A raising `app_shutdown` hook** now emits `lifespan.shutdown.failed`
  (previously only startup failures were reported).
- **gRPC integration tests migrated off `httpx.ASGITransport`**, which has no
  `http.response.trailers` support and can't observe gRPC's trailer-carried
  `grpc-status` (every gRPC response has reported status in trailing headers
  since Sprint 58). Tests now drive a real h2c socket via BlackBull's own
  `HTTP2Client`, exercising the full `__call__ → _dispatch → serve_grpc` path.

### Removed (internal, no public API impact)

Net −614 lines. Removed dead code flagged by the 2026-07-07 comprehensive
audit: the orphaned `EventEmitter` utility, ~40 pre-registered identical
`ErrorRouter` fallback entries (replaced by a single `default=` miss
handler), the racy TOCTOU `check_port` connect-probe, `parse_post_data`,
and several other unused helpers and orphaned tests. The router now stores
only string paths in its trie; a route registered with a regex-*source
string* (as opposed to a compiled `re.Pattern`) is rejected at registration
with a pointed `ValueError` instead of silently mis-routing.

## [0.49.3] — 2026-07-09

### Security

- **HTTP/1.1 — chunk-framing line length bound (audit bug 1.24,
  CVE-2023-39326 class)**: the chunk-size+extension line and every trailer
  line are now capped at 8 KiB; an oversized line (probe
  `MAL-CHUNK-EXT-64K`) answers 400 instead of escaping as a
  `LimitOverrunError`-backed 500.  A bare-LF-terminated trailer section
  (`SMUG-CHUNK-LF-TRAILER`) is rejected 400 instead of hanging until the
  client gives up.
- **HTTP/1.1 — prohibited trailer fields rejected (RFC 9110 §6.5.1)**:
  framing / routing / authentication / content-handling fields
  (`Transfer-Encoding`, `Content-Length`, `Host`, `Authorization`,
  `Content-Type`, …) in a chunked trailer section now answer 400.
- **HTTP/1.1 — strict Content-Length (RFC 9110 §8.6)**: leading zeros,
  doubled/tab/trailing OWS around the value (probe `SMUG-CL-*`,
  `MAL-CL-TAB-BEFORE-VALUE`) are rejected 400 before the generic OWS strip
  hides them.
- **HTTP/1.1 — underscore framing confusables**: `Content_Length` /
  `Transfer_Encoding` header names (probe `NORM-UNDERSCORE-*`) are
  rejected 400.

### Fixed

- **HTTP/1.1 — missing `Host` on an HTTP/1.1 request now 400**
  (RFC 9112 §3.2, audit bug 1.25); HTTP/1.0 requests may still omit it.
- **HTTP/1.1 — unsupported HTTP major version now 505** (RFC 9110
  §15.6.6, audit bug 1.25): `GET / HTTP/9.9` was served as if 1.1.
  `HTTP/1.x` minors above 1.1 remain accepted as 1.x-compatible.
- **HTTP/1.1 — no `100 Continue` to HTTP/1.0 clients** (RFC 9110 §15.2,
  probe `COMP-NO-1XX-HTTP10`): `Expect: 100-continue` from a 1.0 client
  is ignored and the body read normally.
- **HTTP/2 — refused multi-frame HEADERS no longer kills the connection**
  (audit bug 1.14 #2): a HEADERS refused at `MAX_CONCURRENT_STREAMS` with
  `END_HEADERS` unset now keeps consuming the header block; the refusal
  (RST_STREAM `REFUSED_STREAM`) happens at `END_HEADERS`, after the HPACK
  decode that keeps the dynamic table in sync, instead of the peer's
  legal CONTINUATION tripping a bogus GOAWAY(PROTOCOL_ERROR).

With these fixes the authoritative Http11Probe re-score reaches
**161/161 (0 failed, 5 warnings)** — up from 156/161 (5 failed, 13
warnings) at v0.49.2.

### Docs

- `bench/conformance/README.md` — CI status badge + per-job coverage
  table; removed the stale "work in progress / h2spec only" framing.
- `bench/peers/NOTES.private.md` — AI-agent stale-data note on the
  2026-05-18 h2spec 51 % calibration section.
- `KNOWN_LIMITATIONS.md` — corrected the swapped nginx/BlackBull columns
  on the HTTP/9.9 differential-corpus row.

## [0.49.2] — 2026-07-08

Sprint 63 — Http11Probe hardening (RFC 9112 §3.2 / §7.1) + audit bug 1.16 —
**plus** the two Sprint 62 HTTP/2 flow-control deferrals from the
2026-07-07 comprehensive audit: consume-based inbound flow control
(`proposals/consume-based-inbound-flow-control.md`) and the strict-peer
multi-stream concurrency gate for the shared connection send window (audit
bug 1.2). HTTP/1.1 request framing and request-target parsing are tightened
to reject the smuggling / malformed-input vectors the Http11Probe baseline
flagged; malformed chunked framing now answers a clean `400` instead of a
`500` or a silent `200`. No public-API changes.

### Fixed

- **Consume-based inbound HTTP/2 flow control (Sprint 62)** —
  `WINDOW_UPDATE` credit for an inbound DATA frame is now replayed when the
  application *consumes* the event off the stream's recipient queue, not
  when the frame is enqueued (`HTTP2Recipient` gained a `credit_callback`,
  mirroring `HTTP2WSReader`'s credit-replay shape). A handler that stalls
  reading (e.g. a bidi gRPC handler blocked on `yield` under response
  back-pressure, or a client-streaming handler starved of CPU) now closes
  the inbound window and back-pressures the peer instead of overflowing the
  64-deep recipient queue into `RST_STREAM(ENHANCE_YOUR_CALM)` — grpcio no
  longer sees intermittent `RESOURCE_EXHAUSTED` on over-window request
  streams. The recipient queue is bounded by the advertised inbound window
  in *bytes* (plus a generous frame-count cap against zero/tiny-frame
  floods), so the queue-full RST is now strictly an abuse backstop for
  peers that ignore the closed window. A stream released without draining
  its body (handler ignored `receive`, or was cancelled by RST_STREAM)
  replays the un-consumed balance to the *connection* window so the shared
  stream-0 budget cannot leak shut. The two Sprint 60
  `xfail(strict=False)` interop tests (`test_large_both_directions_over_window`,
  `test_large_request_stream_over_window`) are now hard gates in the
  `grpc-interop` CI job.

- **Chunked request framing (RFC 9112 §7.1)** — the chunk-size token is
  validated against the strict `1*HEXDIG` grammar *before* `int()` (rejecting
  `-1`, `0x5`, `+0`, `1_0`, leading/trailing whitespace), the `chunk-ext`
  grammar is validated (bare `;`, non-token names/values, and control
  characters rejected), the size line must be CRLF-terminated (bare-LF
  rejected), and the chunk-data terminator is checked as exactly `CRLF`
  (chunk-data spill and bare CR/LF terminators rejected). Violations raise a
  `400 Bad Request` and close the connection instead of surfacing as a
  fabricated `500` or being silently accepted.
- **Request-target forms (RFC 9112 §3.2)** — absolute-form
  (`GET http://host/path`) is rewritten to origin-form for routing with the
  request's authority overriding a spoofed/mismatched `Host`; asterisk-form
  (`OPTIONS *`) is answered server-wide (`204` + `Allow`) rather than routed
  to a 404, and is rejected (`400`) for any method other than OPTIONS;
  `CONNECT` returns `501`; a raw non-ASCII byte in the request-target is
  rejected (`400`).
- **Header validation** — userinfo in the `Host` header (`user@host`) is
  rejected (`400`, RFC 3986 §3.2); a duplicate `Content-Type` is rejected;
  a `Transfer-Encoding` where `chunked` is not the sole final coding
  (`chunked, gzip`, `chunked, chunked`) is `400` (undeterminable length),
  distinct from an unimplemented coding (`gzip`) which stays `501`.
- **Bug 1.16 — `X-Forwarded-Prefix` no longer trusted off the wire.** The
  HTTP/1.1 and HTTP/2 parsers no longer set `scope['root_path']` from the
  client-controlled `X-Forwarded-Prefix` header; only the `TrustedProxy`
  middleware sets it, after verifying the direct peer — mirroring the
  existing `X-Forwarded-For` / `X-Forwarded-Proto` trust model. A client
  could previously spoof the application's mount prefix.

### Added

- `docs/about/architecture.md` — protocol ownership, the Actor model,
  fault injection, conformance, and performance, with the reasoning
  behind each design bet.
- `docs/getting-started/why-blackbull.md` — a scenario-based guide for
  deciding whether BlackBull fits a given project, plus the honest
  trade-off table.
- **Strict-peer multi-stream flow-control gate (Sprint 62, audit bug 1.2)** —
  `test_concurrent_large_responses_share_connection_window`: 10 concurrent
  unary calls multiplexed on ONE grpcio channel × 100 KB responses, so
  cumulative response bytes far exceed the 65535-byte connection window.
  Guards the shared connection send window on the wire: per-stream window
  copies would over-emit and a strict peer kills the connection with
  `FLOW_CONTROL_ERROR`. Runs in the `grpc-interop` CI job on every push/PR.
- `h2_inbound_window_budget` cap-hit log site (`log_cap_hit`) — emitted when
  a peer overruns the advertised inbound stream window (the consume-based
  crediting abuse backstop above); registered in the cap inventory audit.

### Changed

- The four enqueue-time crediting tests in
  `tests/conformance/http2/test_http2_dispatch.py::TestHTTP2FlowControl`
  now assert the consume-time contract (spec change, Sprint 62): crediting
  tests use a body-draining app, and the >65535-byte cumulative-inbound test
  models a window-respecting (credit-paced) peer.

### Docs

- `docs/guide/grpc.md` and `KNOWN_LIMITATIONS.md` corrected — both
  claimed client-streaming and bidirectional gRPC were unsupported and
  that message compression was absent; both shipped in v0.49.0 (all
  four RPC shapes + `gzip`).
- `SECURITY.md` supported-versions table updated (`0.49.x` / `0.48.x`)
  — it had not shifted when v0.49.0 (a MINOR release) shipped.
- `README.md` gained an Actor-model bullet, a cross-reference line to
  the two new docs pages, and an updated Architecture doc link.
  `docs/index.md` now mentions gRPC and MQTT alongside HTTP/1.1, HTTP/2,
  and WebSocket, and links the two new pages.
- `docs/about/rfc9113-implementation.md` §5.2/§6.1/§6.9.1 updated to
  describe consume-time inbound crediting (Sprint 62); a pre-existing
  staleness describing the pre-bug-1.2 per-sender window scalars was
  fixed alongside.

## [0.49.1] — 2026-07-07

Correctness patch — the HTTP/1.1 and HTTP/2 bug fixes from the 2026-07-07
comprehensive audit (Sprints 61 + 62). No new features and no public-API
removals; two small additions to the public API are noted below. Every fix
ships with a regression test (`tests/unit/test_audit_sprint61.py`,
`tests/conformance/http2/test_audit_sprint62.py`).

### Fixed

HTTP/1.1 + request handling:

- **Chunked bodies split across TCP segments** are now reassembled whole — the
  chunked path reads chunk-data with `readexactly`, not an up-to-`n` read
  (RFC 9112 §7.1). *(audit 1.1)*
- **A raising `app_startup` hook** now emits `lifespan.startup.failed` instead
  of silently killing the lifespan task; `LifespanManager.__aenter__` races the
  startup ack against the task so a failed startup can no longer hang the server
  forever. *(audit 1.3)*
- **Double-response splice**: `HTTP1Sender` drops response events once a
  response has completed, so a handler that raises *after* completing can no
  longer splice a second response onto the connection (mirrors the H2 sender's
  post-`END_STREAM` drop). *(audit 1.4)*
- **Non-WebSocket `Upgrade:` tokens** (e.g. curl's `Upgrade: h2c`) are ignored
  rather than crashing dispatch and closing with no reply (RFC 9110 §7.8).
  *(audit 1.5)*
- **Keep-alive framing desync**: an unread request body is now drained (bounded)
  or the connection is closed before the next pipelined request. *(audit 1.6)*
- **Truncated uploads**: `read_body` raises the new `ClientDisconnected` on a
  mid-body disconnect instead of returning a partial upload as if whole; the
  gRPC bridge maps it to `CANCELLED`. *(audit 1.11)*
- **Malformed JSON / dataclass request bodies** raise `HTTPException(400)`
  instead of surfacing as a 500. *(audit 1.12)*
- **A mid-path `{name:path}` wildcard** is rejected at registration time rather
  than silently mis-routing. *(audit 1.13)*
- **The WebSocket handshake** rejects an absent/malformed `Sec-WebSocket-Key`
  with 400 (RFC 6455 §4.2.1). *(audit 1.15)*

HTTP/2 flow control + lifecycle:

- **Connection-level flow control** now uses one shared send window: every
  stream sender references a single `ConnectionWindow`, so N concurrent streams
  debit one stream-0 budget instead of each spending a full 65535-byte window.
  A strict peer (nghttp2, grpc-go) no longer sees a connection
  `FLOW_CONTROL_ERROR` + GOAWAY under concurrency (RFC 9113 §6.9.1). The
  connection `WINDOW_UPDATE` handler credits the shared window once and wakes
  all senders. *(audit 1.2; `stream_window_size` is now a plain int — refactor
  2.5)*
- **GOAWAY early-return** now signals recipients, so stream tasks blocked in
  `receive()` get `http.disconnect` and the connection can drain instead of
  wedging. *(audit 1.8)*
- **Closed-stream tracking is bounded** (LRU cap + high-water mark), so a
  long-lived connection cycling millions of streams no longer leaks memory.
  *(audit 1.9)*
- **A PRIORITY frame** naming an unknown dependency parents under the root
  instead of crashing the frame loop. *(audit 1.10)*
- **A single-frame HEADERS on an already-open stream** is treated as trailers
  (clean end-of-stream), not respawned as a second request over the live
  recipient/task. *(audit 1.14 #1)*
- **Oversized-frame guard**: `receive()` rejects a frame whose declared payload
  exceeds `SETTINGS_MAX_FRAME_SIZE` before buffering it — no 16 MiB allocation
  on an attacker-declared length. *(audit 1.14 #3)*

### Security

- **`H2FaultServer`'s production guard** now checks the real signal
  (`BLACKBULL_ENV=production`, with the `BB_PRODUCTION` override retained); the
  previous `BB_PRODUCTION`-only check was a no-op in production. *(audit 1.22a)*
- **`make_self_signed_h2_context`** registers a finalizer that removes its
  tempdir, so the unencrypted private key no longer accumulates in `/tmp`.
  *(audit 1.22b)*

### Added (public API)

- `ClientDisconnected` — raised by `read_body` on a mid-body client disconnect
  (carries the `.partial` bytes read so far).
- `HTTPException` — a status-carrying exception (`.status` / `.detail`) that the
  dispatcher turns into the corresponding HTTP response.

### Deferred (documented, not in this release)

- **Refused multi-frame HEADERS / multi-frame trailers** (audit 1.14 #2) — needs
  a CONTINUATION-accumulation restructure to keep HPACK decoder state coherent;
  left for a focused follow-up rather than risking the H2 core.
- **Consume-based inbound flow control**
  (`proposals/consume-based-inbound-flow-control.md`) — credits the inbound
  window on app consumption rather than enqueue, so a slow handler back-pressures
  instead of triggering `RST_STREAM(ENHANCE_YOUR_CALM)`. Its strict-xfail gates
  remain deferred; the large-over-window interop test stays a non-strict xfail.

## [0.49.0] — 2026-07-07

Sprint 60 — completing the gRPC **transport** (the dependency-free gaps; no
protobuf on the wire).

### Added
- **Client-streaming and bidirectional-streaming gRPC** — all four RPC kinds are
  now served over the ASGI bridge (no dedicated actor). A request-streaming
  handler takes an async iterator of messages (`request_iter`); the request axis
  is auto-detected from the first parameter name (or set explicitly via
  `client_streaming=`). An incremental Length-Prefixed-Message de-framer
  reassembles messages across `http.request` events. Real grpcio interop tests
  for `stream_unary` and `stream_stream`.
- **gRPC gzip message compression** (`blackbull/grpc/compression.py`) — compressed
  requests (`grpc-encoding: gzip`, per-message Compressed-Flag) are decompressed
  with a decompression-bomb guard (bounded output); the server advertises
  `grpc-accept-encoding: identity,gzip` and gzip-compresses responses over a
  threshold (`BB_GRPC_COMPRESS_MIN_BYTES`, default 1 KiB) when the client accepts
  gzip and it shrinks the message. An unsupported encoding → `UNIMPLEMENTED`.
- **Fuller `GrpcContext`** — `time_remaining()` (from `grpc-timeout`), `peer()`
  (grpc-style `ipv4:host:port` / `ipv6:[host]:port`), `invocation_metadata()`, and
  `send_initial_metadata()` to flush leading response metadata (initial HEADERS)
  before the first message.

### Fixed
- **WINDOW_UPDATE(0) on empty END_STREAM DATA** (RFC 9113 §6.9) — a zero-length
  DATA frame (grpcio closes a client-streaming request this way) no longer credits
  a 0-byte window increment, which strict clients treat as a protocol error and
  drop the connection.

## [0.48.1] — 2026-07-05

### Fixed
- **IPv6 requests returned an empty reply** (RFC 3986 §3.2.2) — parsing a
  bracketed IPv6 Host header (`[::1]:8100`) with a naive `split(b':')` produced
  `int(b'')` → `ValueError`, which propagated past `HTTP1Actor.run` and closed
  the transport with no response bytes. Every IPv6 request saw "empty reply from
  server" even though the TCP handshake succeeded; IPv4 on the same host worked.
  Host parsing now understands the bracket form and falls back to the default
  port on a missing/invalid port. This unblocks deployment behind IPv6-only
  reverse proxies (e.g. Alwaysdata, whose proxy connects over `::`).
- **IPv6 `scope['client']` / `scope['server']` were 4-tuples** — `AF_INET6`
  `getpeername`/`getsockname` return `(host, port, flowinfo, scope_id)`; these
  are now truncated to the ASGI-required `(host, port)` 2-tuple.

## [0.48.0] — 2026-07-04

### Added
- **Async logging is now batch logging** (`BB_LOG_BATCH_SIZE`, default `64`) — the
  async-logging stream/file sink *always* coalesces records into one
  `write()`+`flush()` per batch (via one flusher thread, flushed when the batch
  fills or after `BB_LOG_BATCH_TIMEOUT_MS`, default 5 ms). A per-record `flush()`
  is the dominant cost of access logging — py-spy showed one flush syscall per
  request churning the GIL against the event loop for ~16% of CPU and a −44%
  throughput hit; coalescing removes it (single-process re-profile: −44% → −31%
  and rising with width). `BB_LOG_BATCH_SIZE` is now the coalescing width (floored
  at 2), not an on/off switch; to force per-record flush, disable async logging
  (`BB_ASYNC_LOGGING=0`). Drained at teardown so no trailing batch is lost; not
  applied to the syslog sink. (Logging optimization O2 / approach 4.)
- **Structured JSON logging** (`BB_LOG_FORMAT=json`) — the async-logging sink can
  emit one JSON object per line instead of plain text. Access-log records expose
  `client_ip`, `method`, `path`, `http_version`, `status`, `response_bytes`,
  `duration_ms` (and `close_code` on WebSocket disconnect) as top-level keys;
  every record carries `timestamp`, `level`, `logger`, `message`. Formatting runs
  on the listener thread, so the access record's string build still happens off
  the event loop. Opt-in; plain text stays the default. (Logging approach 3.)
- **Syslog / UDP log shipping** (`BB_SYSLOG_ADDR=host:port`) — when set, the
  async-logging sink ships records via a UDP `SysLogHandler` instead of `stderr`;
  composes with `BB_LOG_FORMAT=json` (JSON lines over syslog). An unparseable
  address falls back to `stderr` with a warning. (Logging approach 6.)
- **Access-log fast path — direct enqueue, bypassing `logging.Logger._log`**
  (logging optimization O4). When async logging is active, `emit_access_log`
  builds the `LogRecord` and puts it straight on the listener queue via
  `enqueue_access_log`, skipping `Logger._log`'s `findCaller` stack walk, filter
  chain, and `callHandlers` dispatch — py-spy attributed ~93% of the loop-side
  emit cost to that stdlib machinery. Structured fields (`as_extra()`) are merged
  onto the record, so JSON/structured sinks are unchanged; the self-formatting
  message still renders on the listener thread (deferred format preserved). Falls
  back to the synchronous `logger.info` path when async logging is off. Producer
  microbench: **~7.5µs → ~5.7µs per emit (−24%)**; single-process server penalty
  −33% → −24%. Transparent: the fast path runs only when `blackbull.access` has
  no user-attached handlers or filters — if it does, the standard `logger.info`
  path is used, so the documented custom-handler/filter access-log extension
  keeps working.
- **File log sink** (`BB_LOG_FILE=path`) — the async-logging sink can write to a
  file (append mode) instead of `stderr`, composing with `BB_LOG_FORMAT=json` and
  `BB_LOG_BATCH_SIZE`. The stream is opened on the listener side (post-fork) so a
  multi-worker server never inherits a writer thread across `fork()`; access-log
  lines (< `PIPE_BUF`) interleave atomically under `O_APPEND`. Ignored for the
  syslog sink; an unopenable path falls back to `stderr` with a warning. (Logging
  approach 2.)
- **Connection-level TCP segment coalescing** (`BB_H2_CONN_BUFFER_US`, default
  `0` = off) — response frames from HTTP/2 streams that complete within a short
  window on one connection can be flushed as a single TCP segment instead of one
  per stream, removing the per-response delayed-ACK stall that dominates at low
  connection counts / high multiplexing (e.g. a gRPC fan-out of many RPCs over
  one connection). The first frame of an idle window writes immediately (no
  added latency for an isolated response); control frames
  (`SETTINGS`/`PING`/`WINDOW_UPDATE`/`GOAWAY`/`RST_STREAM`) always bypass the
  buffer, and wire/HPACK order is preserved by FIFO flushing. Opt-in — the
  single-segment shape can regress at higher connection counts, so it is off by
  default. See `docs/reference/env-vars.md`.

### Changed
- **WebSocket send hot path (fewer allocations, no behaviour change)** — outbound
  data frames are now written vectored: the 2-to-10-byte frame header
  (`encode_frame_header`) and the payload go to the transport as
  `writelines((header, payload))`, so the payload is no longer copied into a
  concatenated frame buffer on every send. `encode_frame` shares the same header
  builder for its unmasked path (two allocations instead of three). The
  per-message `websocket_message` event emit is now skipped entirely (no `Event`
  or detail-dict build) when no handler is registered, via a generation-cached
  `has_websocket_message_listeners()` guard — matching the existing
  request-lifecycle fast path. Wire bytes are byte-for-byte identical.
- **Internal refactors (no behaviour change)** — replaced mechanical repetition
  in the frame, MQTT, sender, HTTP/1.1, HTTP/2, app, and router layers with
  module-level dispatch tables and shared helpers (SETTINGS parsing, MQTT
  encode/decode + property codecs, sender drain/guarded-write/writer helpers,
  the HTTP/1.1 error-response path, HTTP/2 priority-extension setup, and app
  lifecycle registration). Net −54 effective code lines; full suite unchanged.

## [0.47.0] — 2026-07-03

### Added
- **Server-streaming gRPC** — a gRPC handler may now be an async generator that
  `yield`s response messages (`async def m(request, context): yield ...`); the
  registry auto-detects the streaming form (override with
  `grpc.method(path, streaming=True)`). The status rides the trailing HEADERS
  frame after the last message; a failure before the first message is a clean
  Trailers-Only error, one after is reported in trailers. The generator is
  finalized (its `finally` runs) on client cancellation, and `grpc-timeout`
  bounds the whole stream. Unary handlers are unchanged. Verified with a real
  `grpcio` `unary_stream` client, including the 5000-message flow-control shape.
  Client-/bidi-streaming remain unsupported (they need a streamed request).
- **`scope_completed` event** — a guaranteed, cross-protocol terminal event
  emitted once per ASGI scope (HTTP request, WebSocket connection, or gRPC
  call), on success or error, under any server. It is the application-level
  completion event (distinct from the server-level `request_completed`
  telemetry), and the home of the resource-cleanup hook.
- **Blocking observers** — `@app.on(name, blocking=True)` adds a third event
  delivery mode: awaited in registration order (so cleanup completes within the
  event's lifetime) yet isolated (a failing handler cannot break the emitter or
  siblings). Pair with `scope_completed` to close a per-request DB session or
  delete a temp file.
- **`app.register_converter(type, fn)`** — extend simplified-handler return
  coercion so a handler can `return my_orm_object`; the registry is empty by
  default so the common return paths pay nothing. Direct and decorator forms.
- **Real-client gRPC interop conformance** — a new
  `tests/conformance/grpc/test_grpc_real_client_h2c.py` suite drives BlackBull's
  unary gRPC over a real h2c socket with an actual `grpcio` client (success,
  every error status, large-response flow control, and concurrent multiplexed
  calls). It is the only test that puts a spec-strict external gRPC client on
  the wire; a dedicated docker-free `grpc-interop` CI job runs it on every
  push/PR (install with `pip install 'blackbull[grpc-interop]'`).
- **Pre-fork warm-up hooks** — `@app.on_warmup` registers a coroutine that runs
  **once in the master, before the listening socket is created and before
  workers fork**, so every worker inherits the warmed heap (PEP 659
  specialization, primed codecs/TLS) via copy-on-write. `app.drive_asgi(scope,
  body=, n=)` drives the ASGI dispatch path in-process (no socket) to fault in
  code pages, and `blackbull.server.warmup.warm_tls` primes the TLS handshake.
  A no-op with no hooks registered (off by default); `BB_WARMUP_BUDGET_S` caps
  total warm-up time and `BB_WARMUP_TLS_N` the number of in-memory TLS
  handshakes.

### Changed
- **Default `listen()` backlog raised from 128 to 1024** (`BB_SOCKET_BACKLOG`).
  128 (the traditional `SOMAXCONN`) is shallow next to peers like nginx (511);
  1024 reduces silent connection drops during burst arrivals. The kernel still
  caps the effective queue at `net.core.somaxconn`.
- **`Response(headers=...)` accepts a `dict`** (matching the
  FastAPI/Starlette/httpx convention) as well as a list of `(name, value)`
  pairs; names/values may be `str` or `bytes`. Malformed shapes now raise
  `TypeError` at construction instead of silently corrupting the response
  (the old loop iterated a dict's *keys*).
- **Access-log hot-path cost cut (~28–30% less event-loop work per emit** in a
  200k-emit producer microbenchmark, ~9.3µs → ~6.6µs). The access record is now
  self-formatting and handed to the logger *as the message*, so the `format()`
  string build (and the stdlib `QueueHandler`'s eager format + record copy)
  moves off the event loop to the logging listener thread via a new
  deferred-format `QueueHandler`. The request duration is snapshotted at emit so
  the deferred format still reports real request duration, not duration + queue
  latency. Structured `extra` fields (the documented access-log API) stay eager
  and unchanged; ordinary debug/warning logs keep the stdlib's eager,
  mutation-safe formatting.
- **gRPC handler isolation now catches `Exception`, not `BaseException`.** A
  handler bug (any `Exception`) is still isolated as `INTERNAL`, but a
  non-`Exception` throwable — `CancelledError`, `KeyboardInterrupt`,
  `SystemExit`, `GeneratorExit`, or a raw `BaseException` — now propagates
  instead of being masked into a status, so task cancellation and interpreter
  shutdown are honoured (and a server-streaming generator's `GeneratorExit`
  cleanup is no longer swallowed). Each call runs in its own stream task, so a
  propagating throwable unwinds only that stream.

### Fixed
- **gRPC Trailers-Only framing (real-client interop)** — unary gRPC error
  responses now carry `grpc-status` in a *trailing* HEADERS frame instead of a
  non-terminal HEADERS frame followed by an empty `END_STREAM` DATA frame. A
  spec-strict third-party client (grpcio, grpc-go) reads the status only from a
  HEADERS frame with `END_STREAM` or a trailing HEADERS frame, so the old shape
  decoded **every** error — `INTERNAL`, `PERMISSION_DENIED`, `UNIMPLEMENTED`, …
  — as `UNKNOWN` ("Stream removed (Data frame with END_STREAM flag received)").
  BlackBull's own `HTTP2Client` was lenient about the framing, which hid the bug
  until a real gRPC client (`ghz`/grpcio) exercised the wire path. The success
  path was already correct; only the error/Trailers-Only path changed.

### Internal
- **Reload watcher unit tests pinned to polling** — `test_watcher_fires_callback_on_py_change`
  / `test_watcher_ignores_non_py` now force watchfiles into polling mode (as the
  reload *integration* test already did in its subprocess), removing the inotify
  startup-race flake where the first `.py` write was silently dropped under
  suite contention. These run in the fast tier on every PR, so the flake blocked
  merges; the watcher logic under test is identical either way.
- **H2 flow-control deadlock gate now covers a large bidirectional payload** — a
  4th subprocess-isolated scenario echoes 128 KiB of gRPC (up *and* down),
  forcing multiple `WINDOW_UPDATE` refills in both directions at once rather than
  the single-refill window boundary of the existing steps. A regression in
  client crediting or sender resume that survives the boundary cases deadlocks
  here. Rides the existing `h2-flow-control` conformance CI job (every push/PR).

## [0.46.0] — 2026-06-30

Sprint 57 — **gRPC** (the next protocol after MQTT) plus three supporting
HTTP/2 hot-path items.

### Added
- **Unary gRPC over HTTP/2** (`blackbull.grpc`): `GrpcServiceRegistry`,
  `GrpcStatus` / `GrpcError`, the Length-Prefixed-Message codec
  (`encode_message` / `decode_messages`), and the ASGI bridge
  (`serve_grpc` + `GrpcContext`). Enable with `app.enable_grpc(registry)`;
  gRPC requests (`content-type: application/grpc`) multiplex onto the same
  HTTP/2 port as REST and WebSocket. Served through the existing ASGI bridge
  (reusing the `http.response.trailers` emit path for `grpc-status`) — no new
  protocol Actor. Protobuf is not a dependency; handlers exchange raw message
  bytes. Optional `blackbull[grpc]` extra. Docs: `docs/guide/grpc.md`;
  example: `examples/grpc_server.py`.
- **`app.get_routes()` + `RouteInfo`** — public, stable route introspection
  (replaces reaching into `app._router._route_info`).

### Changed / Performance
- **`frame-assembly-fast-path` Tier 2**: `build_response_headers` /
  `build_trailers` in `server/sender.py` encode response HEADERS straight to
  wire bytes, bypassing the receive-oriented `Headers` object on the send
  path. All four response-HEADERS emitters use them; byte-for-byte equivalent
  to the prior `Headers.save()` path. `build_trailers` is the gRPC
  `grpc-status` trailers basis.
- **`copy-reduction-http2`**: non-padded DATA frames skip the BytesIO
  read-copy in `Data.__init__` (P2); CONTINUATION reassembly uses an in-place
  bytearray extend instead of O(n²) `bytes +=` (P3).

### Fixed
- **HTTP/2 large-payload flow-control deadlock** (three pre-existing
  transport bugs surfaced by gRPC conformance, affecting any bidirectional
  exchange over the 65535-byte initial window): the `HTTP2Client` now emits
  `WINDOW_UPDATE` for received DATA so the server's send window is replenished;
  `HTTP2Sender._write_data` no longer loses a `WINDOW_UPDATE` that arrives
  between the window check and `Event.clear()` (lost-wakeup race); and a
  `WINDOW_UPDATE` / `RST_STREAM` arriving on a stream we already closed with
  END_STREAM is now silently ignored per RFC 9113 §5.1 instead of being
  answered with `RST_STREAM` (which tore the client's stream down early).
- **Lifespan shutdown teardown race**: `LifespanManager.__aexit__` no longer
  hangs when the lifespan task is cancelled out from under it during
  `asyncio.run`'s interpreter teardown — it races the shutdown acknowledgement
  against task completion and drains the task's `finally` blocks.
- **RFC 9113 §8.2.1 field validation** (header-injection hardening): HTTP/2
  field names containing a control octet, uppercase letter, `DEL`, or an
  interior colon, and field values containing `NUL` / `CR` / `LF`, are now
  rejected as malformed instead of being forwarded. New `field_name_is_valid`
  / `field_value_is_valid` helpers in `protocol/frame_types.py`.

---

## [0.45.0] — 2026-06-27

Sprint 56 close — **DX consolidation** (no new protocol; gRPC stays queued).
A MINOR of additive developer-experience and perf items: `BLACKBULL_*` env-var /
`.env` resolution for `run()`, `RedirectResponse`, `read_json` / `read_text`
body helpers, HTTP/1.1 `Content-Length` body streaming, and two pay-for-what-you-use
hot-path wins (cached request-listener check, lazy per-connection cap counter).

### Added
- **`BLACKBULL_*` environment-variable + `.env` resolution for `app.run()`.**
  The deploy-time settings — `BLACKBULL_PORT` / `CERT` / `KEY` / `UNIX_PATH` /
  `RELOAD` — now resolve with documented precedence: explicit `run(...)` argument
  → `BLACKBULL_*` env var → `.env` file → bound `AppConfig` → built-in default
  (see `blackbull.config.resolve_run_config`). `.env` loading is gated behind a
  new optional extra `blackbull[dotenv]` (no new hard dependency); without it,
  resolution from the real process environment still works. One INFO line per
  non-default deploy setting is logged at startup on the `blackbull.config`
  logger, naming each value's source (key paths are logged, never contents).
  `BLACKBULL_*` is the deployment namespace; `BB_*` remains the tuning namespace.
- **`RedirectResponse`** — `Response` convenience subclass that sets a `Location`
  header and a 3xx status (default `302 Found`), completing the
  `JSONResponse` / `StreamingResponse` family. Exported from `blackbull`.
- **`read_json` / `read_text`** — request-body helpers wrapping `read_body`.
  `read_json(receive)` returns the parsed JSON value or `None` on empty / invalid /
  undecodable bodies; `read_text(receive, encoding='utf-8')` decodes with
  `errors='replace'` so malformed bytes never raise. Both exported from `blackbull`.
- **`BB_BODY_CHUNK_SIZE` — streamed HTTP/1.1 `Content-Length` request bodies.**
  A `Content-Length` body is now delivered to the ASGI app as successive
  `http.request` events of at most `BB_BODY_CHUNK_SIZE` bytes (default 64 KiB,
  must be > 0; `more_body: True` until exhausted) instead of one
  `readexactly(content_length)` allocation — capping per-connection buffering and
  letting the app start work before the whole body arrives. The exact-bytes
  contract is preserved (a short body still raises `IncompleteReadError`).

### Changed
- **Cached request-listener check on the request hot path.** `RequestActor.run()`'s
  `has_any_request_listeners()` fast-path guard no longer re-scans the six
  request-lifecycle events on every request; the result is cached against a new
  `EventDispatcher.generation` counter (bumped on `on` / `intercept`) and
  recomputed only when listeners change — effectively once, at startup. Behaviour
  is identical, including for listeners registered after the first request.
- **Lazy per-connection cap counter (`connection-accept` fast path).** The
  `CapHitCounter` and its `os.urandom` connection id are now built only if a cap
  actually fires (`_LazyCapHitCounter`). On the keep-alive / healthy path this
  removes a `getrandom(2)` syscall, an allocation, and a flush from every accepted
  connection; cross-task propagation and cap-hit logging are unchanged.

---

## [0.44.1] — 2026-06-26

Sprint 55 close. A PATCH on top of `v0.44.0`: HTTP now scales across workers
while a stateful single-owner protocol (the MQTT 5 broker) runs alongside,
AsyncAPI 3.0 docs for the broker taps, and behaviour-preserving hot-path perf.
Measured **+8.7% FA-normalized mean HTTP/1.1 throughput** vs `v0.44.0` across the
HttpArena suite (c7i.8xlarge, FastAPI reference; validation 47/0 + WS 7/0), with
the largest gains on the connection-churn / pipelining lanes.

### Changed
- **Hot-path copy + logging reduction (no behaviour change).** Three low-risk
  perf wins on the HTTP/1.1 and HTTP/2 hot paths: `read_body()` collects chunks
  and joins once (single-chunk bodies returned with no copy at all);
  `HTTP1Actor._read_headers()` accumulates the header block in a `bytearray`
  (amortised O(1)) instead of the O(n²) `bytes +=`; and the eager per-frame
  logging on the H2 frame-assembly path (`frame.py` / `frame_types.py` /
  `stream.py`) is now lazy `%`-args or `isEnabledFor`-guarded, so nothing is
  formatted or concatenated when the log level is off. DEBUG output is unchanged
  when DEBUG is on; two valueless per-frame traces were dropped.
- **`RequestActor` fast path when no listeners are registered.** When no Level B
  request-lifecycle event handler is registered (the default), `RequestActor.run()`
  now calls the ASGI app directly, skipping the `EventAggregator` indirection
  (~4 async frames/request that Sprint 53 added for the MQTT broker pattern).
  Benefits HTTP/1.1 and HTTP/2 (both dispatch through `RequestActor`). The
  aggregator path is still taken the moment any listener — including an
  `error`-only one — is present.
- **`PrefixReader.readuntil()` short-circuit.** Once the protocol-detect prefix
  is drained (the common keep-alive case), `readuntil` delegates straight to the
  underlying reader, skipping the per-call buffer bookkeeping.

### Added
- **`AsyncAPIExtension` — AsyncAPI 3.0 docs for the MQTT broker.** Parallel to
  `OpenAPIExtension` (and coexisting with it), it serves an AsyncAPI 3.0
  document describing the app's `@mqtt.on_message` topic taps at
  `/asyncapi.json`, plus a CDN-hosted HTML viewer at `/asyncapi` (no new Python
  dependency). Each tap filter becomes a channel (the `{name}`-preserved filter
  as its address); each callback a `receive` operation. Generated lazily, so
  taps registered after the extension are still documented. `MQTTExtension`
  gains a public `iter_subscriptions()` accessor so the generator never reaches
  into private handler state. Payloads are opaque bytes for now (a `schema=`
  fast-follow is planned). `from blackbull.mqtt import AsyncAPIExtension`.
- **HTTP scales across workers alongside a stateful protocol (MQTT).**
  `app.run(port=8000, workers=4)` with, e.g., `MQTTExtension(port=1883)` no
  longer forces the whole process to a single worker. The master binds the
  protocol port once and hands it to **worker 0** only — the broker keeps its
  single owner (required by the MQTT 5 spec) while HTTP runs on every worker. A
  crashed worker 0 is respawned and re-inherits the still-open listener.
  Auto-reload (`--reload`) with a port-bound protocol still pins `workers=1`
  (the exec socket-handoff does not yet carry protocol listeners).

### Fixed
- **`BB_SOCKET_REUSEPORT` is now honoured on the HTTP listener.** The setting
  existed (`env.py`, CLI TOML mapping) but was never passed through
  `Server.open_socket()` to `create_dual_stack_sockets()`, so `SO_REUSEPORT`
  was silently inert on the bound HTTP sockets. Plumbed through (the stateful
  protocol port is still bound *without* it, by design — a single owner).
- **`WebSocketActor` no longer swallows `asyncio.CancelledError`.** `run()`
  caught `BaseException` (to isolate the connection from app/protocol errors),
  which also swallowed cancellation — so cancelling a WebSocket task completed it
  *normally* and reported the cancellation through `on_error` instead of
  propagating it. It now re-raises `CancelledError` before the generic handler
  (mirroring `HTTP1Actor`); the disconnect/close cleanup still runs in `finally`.
  (CodeQL `py/catch-base-exception`.)

### Internal
- **MQTT per-connection actor renamed `MQTTConnectionActor` → `MQTT5Actor`**,
  matching the `HTTP1Actor` / `HTTP2Actor` `<Protocol><Version>Actor` convention
  (the `Connection` suffix made it read like a dispatcher). No public API impact —
  it was never exported.
- CodeQL quality analysis is scoped to the shipped package (`blackbull/`) plus
  `examples/`; `tests/`, `bench/`, `templates/`, and `docs/` are excluded via
  `.github/codeql/codeql-config.yml`, removing ~196 style-lint alerts in
  non-shipped code.

---

## [0.44.0] — 2026-06-25

Combined release of Sprint 50 through Sprint 54 (see the Versioning note above).
Sprints 50–52 debut the Non-ASGI bridge and the first protocol to ride it — a
pure-Python MQTT 5 broker.  Sprints 53–54 rebuild that broker on the actor model
and make the connection dispatcher fully protocol-agnostic so the next protocol
adds zero hardcoded branches.

### Added
- **`{name}` topic captures for MQTT taps (Sprint 54).**
  `@mqtt.on_message(topic='sensors/{room}/temperature')` matches `{room}` as one
  level (like `+`) and injects it into the callback as a keyword argument,
  mirroring HTTP path params.
- `AbstractReader.at_eof()` (default `False`; `AsyncioReader` delegates to the
  underlying stream) so a long-lived raw-protocol read loop can detect peer
  close instead of relying solely on task cancellation.
- `Actor` accepts an optional `inbox_maxsize` (default `0` = unbounded) for a
  bounded inbox, enabling explicit overflow policies such as the `TapActor`'s
  drop-newest.
- **Generic extension mechanism.** `blackbull.extension.Extension` is the base
  class for plugins (`extension_key`, `init_app(app)`, optional async
  `startup`/`shutdown`); `app.add_extension(ext)` is the single registration
  seam on the core — it calls `init_app`, wires lifecycle into the
  `app_startup`/`app_shutdown` lifespan events, and returns the instance for
  chaining.  It duck-types on `init_app`, so legacy extensions keep working.
  `OpenAPIExtension` is retrofitted onto the base class as a second reference.
  This keeps `BlackBull` protocol-agnostic: a protocol is added by passing its
  extension to `add_extension`, never by editing the core class.  See
  `docs/guide/extensions.md`.
- **MQTT 5 broker sidecar (Sprint 52).** A pure-Python MQTT 5 broker runs on the
  Non-ASGI bridge alongside HTTP.  It is a **non-core "bridge" protocol** — it
  lives in its own `blackbull.mqtt` subpackage (distinct from the core HTTP
  family in `blackbull.protocol` / `blackbull.server`) and is opt-in via the
  `blackbull[mqtt]` extra, structured for later extraction to a standalone
  `blackbull-mqtt` package without core changes.  `blackbull.mqtt.messages` provides
  the 15 control-packet dataclasses, `encode_packet` / `decode_packet`, the full
  MQTT 5 property system (§2.2.2.2), reason codes, and `topic_matches_filter`.
  `MQTTActor` (`blackbull.mqtt.actor`) is the per-connection broker:
  CONNECT/CONNACK (protocol-level check → `0x84`), SUBSCRIBE/UNSUBSCRIBE,
  PUBLISH at QoS 0/1/2 with their acknowledgement flows, retained messages,
  Will (LWT) delivery on abnormal disconnect, keep-alive PING, and Clean
  Start / Session Present semantics.  The broker is wired in as
  `MQTTExtension`: `mqtt = app.add_extension(MQTTExtension(port=1883))`, with
  `@mqtt.on_message(topic=…)` tapping the broker's routing via an async
  `(topic, payload)` callback; `MQTTProtocolDetector` recognises the CONNECT
  first byte (`0x10`) for shared-port sniffing.  See `docs/guide/mqtt.md` and
  `examples/mqtt_broker.py`.
- **Non-ASGI protocol bridge (Sprint 50).** `app.raw_handler(name, *, port=…)`
  / `app.register_protocol_handler(...)` register a raw-TCP protocol that speaks
  the wire directly, alongside HTTP on other ports.  A `RawActor` drives the
  byte stream; see `docs/guide/raw-protocols.md` and `examples/echo_tcp.py`.
- **Unified `ProtocolRegistry` (Sprint 50).** Connection dispatch is now
  registry-driven (`Http1Binding`, `Http2Binding`, `RawBinding`) instead of a
  hardcoded `_dispatch()`; `ASGIServer` is reorganised and re-exported as
  `Server`.  Non-disruptive — all HTTP/1.1, HTTP/2, and WebSocket paths behave
  identically.
- **Protocol-agnostic Level B events (Sprint 50).** `EventAggregator` gains
  `on_connection_accepted(protocol=…)`, `on_connection_closed`,
  `on_message_received`, and `on_message_sent`, so observers can hook raw
  protocols, not just HTTP requests.
- **`ProtocolDetector` shared-port dispatch (Sprint 51).** Raw bindings may
  carry a detector consulted after the cleartext-HTTP chain, enabling a raw
  protocol to share a port with HTTP (the foundation for Sprint 52 MQTT).

### Changed
- **MQTT broker rewritten to the actor model (Sprint 53).** The procedural
  `MQTTActor` + process-global broker state (`_topic_router` / `_session_store`
  / `_retained_store`) are replaced by two `Actor`s: a single supervisor/
  lifespan-owned `BrokerActor` that owns *all* routing/session/retained state
  (serial inbox ⇒ no locks, no shared mutable state) and one
  `MQTTConnectionActor` per connection (its inbox is the sole socket writer; a
  reader loop forwards control packets to the broker). `serve_connection` wires
  the two; `MQTTExtension` owns the broker and starts/stops it on
  startup/shutdown. No user-facing wire behaviour changes — the conformance
  suite is unchanged. This makes MQTT — the reference bridge protocol — a
  first-class citizen of BlackBull's actor model, the template for future
  protocols.
- **`on_message` taps now receive a single `blackbull.mqtt.Message`**
  (`topic`/`payload`/`qos`/`retain`/`properties`) instead of `(topic, payload)`,
  mirroring how `@app.on` hands an observer one `Event`.
- **MQTT taps now dispatch on a decoupled `TapActor` by default (Sprint 54).**
  The connection *offers* each message to a single lifespan-owned `TapActor` and
  returns immediately, so a slow tap can no longer back-pressure delivery or the
  broker (the Sprint 53 inline dispatch did). The `TapActor`'s inbox is bounded;
  on overflow the newest message is dropped and a running dropped-count is logged
  — taps are best-effort observability, not a reliable delivery path.
  `MQTTExtension(tap_mode='inline')` restores the inline behaviour, and
  `tap_queue_size=` tunes the bound.
- **MQTT module split (Sprint 54).** The flat `blackbull/mqtt/actor.py` is broken
  into `broker.py` (`BrokerActor` + the Level A messages), `connection.py`
  (`MQTTConnectionActor`, `PacketFramer`, `serve_connection`), `tap.py`
  (`Message`, `Tap`, `TapActor`), and `extension.py` (`MQTTExtension`,
  `MQTTProtocolDetector`). Public imports from `blackbull.mqtt` are unchanged.
- Will (LWT) delivery on abnormal disconnect no longer relies on the old
  keep-globals-forever crutch: the long-lived `BrokerActor` outlives connection
  actors, so a peer's Will routes to live subscribers during teardown by
  construction. The broker now ends a connection on real EOF.
- `MQTTConnectionActor`'s read loop now frames packets through a small
  incremental `PacketFramer` (Sprint 54): it decodes straight off its internal
  buffer (no whole-buffer `bytes(...)` copy per attempt) and treats an incomplete
  packet as "await more bytes", dropping a byte to resync only on a genuine
  decode error.

### Fixed
- **MQTT subscription options now persist across reconnect (§3.1.2.11).** The
  broker stored each subscription as a `(filter, qos)` pair, silently dropping
  the No Local / Retain As Published / Retain Handling options — so a client
  reconnecting with Clean Start = 0 lost them. `BrokerActor` now keeps
  `session['subscriptions']` as `(filter, qos, options)` tuples (and a
  re-SUBSCRIBE to an existing filter replaces it per §3.8.4).
- **Shared-port MQTT detection no longer hangs.** A CONNECT packet with no CRLF
  in its first bytes used to ride the HTTP `readuntil(b'\r\n')` detection read
  and block until the slowloris timeout when MQTT shared an HTTP listener. The
  dispatcher now peeks a tiny protocol-agnostic discriminator, so the broker is
  recognised on the CONNECT's first byte. (Port-bound MQTT was never affected.)
- **`connection_closed` now fires for HTTP connections too.** Previously only
  raw/non-ASGI (MQTT) connections emitted `connection_closed`; HTTP connections
  emitted `connection_accepted` but never the matching close event. The
  lifecycle is now symmetric for every protocol, and the event carries the
  served protocol name (`http1` / `http2` / the binding name) and duration.
  **Behaviour change:** an `@app.on('connection_closed')` handler will now also
  receive HTTP connection events.

### Internal
- **`ConnectionActor` is now protocol-agnostic** (decouple-connection-detection).
  Detection peeks a binding-declared discriminator prefix and replays
  it to the winning binding via a `PrefixReader`; the three `serve_alpn` /
  `serve_cleartext` / `serve_raw` methods collapse to one `serve(conn)`, and the
  24-byte HTTP/2 preface read and the HTTP/1.1 request-line read move into the
  bindings. The detection-timeout 408 also moves into a binding hook
  (`ProtocolBinding.on_detect_timeout`; HTTP emits the 408, other protocols
  close silently). `ConnectionActor._dispatch()` no longer contains hardcoded
  byte counts, delimiters, or HTTP status strings. No hot-path regression
  (EC2 HttpArena gate).
- **`RawProtocolActor` (the non-ASGI Layer-2 actor) is removed.** Connection
  timing, error isolation, and the `connection_closed` event now live in
  `ConnectionActor.run()` for every protocol; a `RawBinding` calls its handler
  directly. One lifecycle owner instead of an HTTP path and a separate raw path.
- MQTT codec reads in spec terms instead of raw hex: named flag/level
  constants (`ConnectFlags`, `PublishFlagBits`, `SubscriptionOptions`,
  `ProtocolLevel`, `WILL_QOS_*`, `PUBLISH_QOS_*`, `RETAIN_HANDLING_*`,
  `RESERVED_FLAGS_0010`) in `blackbull.mqtt.messages`.
- Single source of truth for the reason codes the broker uses: `ReasonCode`
  (`IntEnum`) in `messages.py`; the duplicated per-module `_RC_*` constants in
  `broker.py`/`connection.py` are deleted.
- Raw protocol handlers are single-worker and cleartext-only for now; documented
  in `KNOWN_LIMITATIONS.md` / `docs/guide/raw-protocols.md`.  Combined Sprint
  50 + 51 + 52 work releases together as `v0.44.0`.
- `AbstractReader.readuntil` / `readexactly` now have concrete default
  implementations built on `read()`, so a minimal reader (e.g. an MQTT test
  double) only needs to implement `read`.  Concrete transport readers continue
  to override both with their native buffered versions.

## [0.43.2] — 2026-06-22

### Fixed

- **Middleware `send` wrappers received `Response` objects instead of ASGI
  dicts.**  The `_wrap_send` adapter was installed at the outermost layer
  (`BlackBull.__call__`), but `send` flows handler→outward, so it normalised
  *last* — after every middleware had already seen the raw object.  A
  middleware that wrapped `send` and inspected `msg['type']` therefore crashed
  with `TypeError: 'Response' object is not subscriptable` whenever a handler
  returned a `dict`/`str` (auto-`Response`) or called `send(Response(...))`.
  Fix: install `_wrap_send` at the *handler boundary* in `_dispatch`, so
  everything above the route handler observes plain ASGI dicts.  (The defect
  shipped in 0.43.0; it surfaced only in 0.43.1 once the lifespan-startup crash
  it hid behind — the beartype forward-ref bug — was fixed.)

### Internal

- **Unified the Response→ASGI serialisation onto a single source of truth.**
  `Response` is now ASGI-callable (`Response.__call__(scope, receive, send)`),
  mirroring `StreamingResponse`, so every response type shares one protocol.
  `app._wrap_send` and `middleware.utils._normalize_send` both delegate to it
  instead of carrying their own (and previously divergent) copies of the
  `http.response.start` + `http.response.body` event construction.  No wire
  behaviour change; terminal body events now consistently carry
  `more_body: False`.
- Added a regression test (`test_middleware_decorator.py`) asserting that a
  plain, undecorated middleware's `send` wrapper receives ASGI dicts through
  the full app stack.

## [0.43.1] — 2026-06-21

### Fixed

- **`Router.validate()` — forward-ref annotation crash on lifespan startup.**
  Route handlers annotated with string types (`'str'`, `'int'`, etc.) or compiled
  under `from __future__ import annotations` (PEP 563) caused a `SyntaxError`
  inside beartype's code generator (`${FORWARDREF:str]?}` — mismatched bracket),
  which surfaced as `RuntimeError: Lifespan startup failed` for all integration
  tests and the production `app.run()` path.
  Fix: resolve annotations via `typing.get_type_hints()` before passing to
  `die_if_unbearable`, and catch `BeartypeException` (beartype's base error class)
  as a defensive fallback for any other internal beartype code-gen failure.
  Affected `beartype` ≥ 0.22.8; no beartype version change required.

## [0.43.0] — 2026-06-20

### Added
- **`AppConfig` — declarative startup config.** A frozen dataclass holding
  exactly the parameters `run()`/`serve()` accept (port, TLS, workers,
  queue depths, reload, …).  `BlackBull(config=AppConfig(...))` declares the
  server settings once; `app.run()` resolves each setting **explicit arg →
  bound config → built-in default**.  Additive — existing programmatic
  startup is unchanged.  Exported as `blackbull.AppConfig`.
- **`blackbull serve` — zero-code static file server.** `blackbull serve
  [DIR]` serves a directory over HTTP/1.1 (and HTTP/2 when `--certfile` /
  `--keyfile` enable TLS) with ETag / `304` conditional requests, a
  directory index, and precompressed-sibling negotiation — a drop-in
  upgrade over `python -m http.server`.  The existing `blackbull
  module:attr` runner is unchanged.
- **`StaticFiles` directory index.** New opt-in `index=` parameter on
  `StaticFiles` and `app.static()` (default off): a request resolving to a
  directory serves the named file (e.g. `index.html`) when present, guarded
  by the same realpath + traversal check.

### Docs
- Added [`docs/about/rfc9113-implementation.md`](docs/about/rfc9113-implementation.md)
  — a section-by-section map of how BlackBull implements RFC 9113, ordered by the
  RFC's own §-numbers, with a coverage summary measured against the spec's
  normative requirements/options (no mandatory MUST is unimplemented).
- Corrected RFC 7540→9113 section citations in `http2_actor.py` and
  `frame_types.py` comments/docstrings (server push §8.4, malformed messages
  §8.1.1/§8.2.1; no behaviour change).
- Documented `AppConfig` and `blackbull serve` in the configuration,
  static-files, and running guides.

## [0.42.3] — 2026-06-19

**HTTP/2 perf: deferred HEADERS write coalesces HEADERS + DATA into one TCP segment.**

`HTTP2Sender` now buffers the `http.response.start` HEADERS frame internally
and flushes it together with the first `http.response.body` DATA frame in a
single `write()` call.  For single-body responses that fit within the current
flow-control windows and `max_frame_size`, this halves the number of `drain()`
calls per response and eliminates the inter-frame TCP segment gap produced by
the previous eager-write path.

Wire order is unchanged: HEADERS precedes DATA per RFC 9113 §8.1.  Date header
auto-injection (RFC 9110 §6.6.1) is preserved on all paths — bytes, ASGI event,
and trailers.

### Performance

- Single-body HTTP/2 responses: HEADERS + DATA coalesced into one `write()` +
  `drain()` (was two separate calls, ~2× the drain yield count per response).

### Internal

- `HTTP2Sender`: three new `__slots__` (`_buffered_status`, `_buffered_headers`,
  `_expect_trailers`) mirror the `HTTP1Sender` buffering model.
- `HTTP2Sender._flush_buffered_start()`: new method handles the coalesced write
  with flow-control and max-frame-size fallback.
- `HTTP2Sender.reset_per_request_state()`: clears buffered fields alongside
  `_end_stream_sent` for correctness across stream reuse.

## [0.42.2] — 2026-06-19

**RFC 9113 compliance fix: stream_id==0 validation for RST_STREAM and PUSH_PROMISE.**

Two frame types were missing the stream_id==0 connection-error check required
by RFC 9113: RST_STREAM (§6.4) and PUSH_PROMISE (§6.6).  A misbehaving client
sending either frame type targeting the connection stream (stream_id==0) would
not have been caught and rejected.  Both checks are now enforced alongside the
existing HEADERS, CONTINUATION, DATA, and PRIORITY checks.

The implementation consolidates all six stream-only checks from four
individual `if` tests into a single `frozenset` lookup (`_STREAM_ONLY_FRAME_TYPES`),
and similarly replaces a per-frame tuple allocation in `_frame_loop` with a
class-level `frozenset` (`_FRAME_SIZE_CONNECTION_ERROR_TYPES`).

### Fixed

- `HTTP2Actor`: RST_STREAM and PUSH_PROMISE frames with `stream_id == 0` now
  raise a connection error per RFC 9113 §6.4 and §6.6 respectively.

### Internal

- `HTTP2Actor._STREAM_ONLY_FRAME_TYPES`: class-level `frozenset` replaces
  four individual checks; coverage expanded to six frame types.
- `HTTP2Actor._FRAME_SIZE_CONNECTION_ERROR_TYPES`: class-level `frozenset`
  eliminates per-frame tuple allocation in `_frame_loop`.

## [0.42.1] — 2026-06-18

**Sprint 47: Custom HTTP Method Support (Proposal 009 Phase 1).**

Routes can now be registered and dispatched using non-IANA HTTP method
strings such as `BREW`, `PROPFIND`, and `WHEN` from RFC 2324 / RFC 4918.
Previously, any method not in `http.HTTPMethod` was rejected at dispatch
time with an immediate 405; the router never had a chance to match.

Key changes:

- `app.py` dispatch: `HTTPMethod(scope['method'])` `ValueError` now keeps
  the raw string and continues to the router instead of short-circuiting
  to 405.  IANA methods still resolve to the `HTTPMethod` enum value.
- `router.py`: `isinstance(x, HTTPMethod)` guard removed from `route_fn`;
  `methods` annotation broadened to `str | HTTPMethod | Iterable[str | HTTPMethod]`
  on all public registration surfaces (`RouteGroup.route`, `BlackBull.route`,
  `Router.route_fn`, `Router.route`, `BaseRouter.route`).
- RFC 9110 §5.6.2 token validation added at registration time: strings
  that are not valid HTTP tokens (e.g. `'BREW METHOD'` with a space) raise
  `ValueError` early.
- `blackbull-htcpcp` extension unblocked: `BREW`, `PROPFIND`, and `WHEN`
  routes now register and dispatch without the previous `try/except HTTPMethod`
  workaround; the three `xfail(strict=True)` tests are promoted to passing.

## [0.42.0] — 2026-06-16

**Sprint 46 close: deliberate-misbehaviour toolkit.**

A new top-level module — `blackbull.fault_injection` — that lets
test suites drive deliberately bad HTTP/1.1 against a real server
or serve deliberately bad HTTP/2 to a real client.  The scenario /
oracle surface that lived under `blackbull.client` in Sprint 45 is
promoted into a first-class public API, joined by a new HTTP/2
programmable fault server (`H2FaultServer`) with a named catalogue
of canned misbehaviours, an optional TLS path so the server can be
driven by httpx or curl, and a `[fault-injection]` install extra
that pulls in the optional dependencies.

This release also restructures `README.md` around the toolkit and
refreshes `SECURITY.md` (supported versions + in-scope modules).

### Added

- **`blackbull.fault_injection` public module** — a single
  namespace for the two directions of protocol fault injection.
  Re-exports the HTTP/1.1 client-side scenario / oracle / category
  surface previously at `blackbull.client.scenario` /
  `blackbull.client.scenario_oracle`, plus the new HTTP/2
  server-side surface below.  Refuses to start when `BB_PRODUCTION`
  is set so a deliberate-misbehaviour code path cannot fire in a
  production deployment.
- **`H2FaultServer`** (`blackbull/fault_injection/h2_server.py`) —
  a programmable HTTP/2 server built directly on `hpack` + the raw
  RFC 9113 frame layer (no use of BlackBull's own HTTP/2 actor —
  the point is to misbehave in ways the conformant stack would
  refuse to emit).  Accepts a `ScenarioH2` step list of
  `SendFrame` / `SendRawBytes` / `WaitForClientFrame` / `Sleep` /
  `Abort` / `CloseGracefully` and replays it against the connected
  client.
- **HTTP/2 fault catalogue** (`blackbull/fault_injection/catalogue.py`) —
  four spec-grade scenario constructors covering distinct failure
  modes: `half_closed_stream_no_data`, `exhausted_window_zero_initial`,
  `settings_max_frame_size_below_minimum`, `headers_continuation_dropped`.
  Each carries a docstring naming the expected client-side
  observable.
- **TLS support for `H2FaultServer`** — new `ssl_context=` kwarg
  on the server plus a `make_self_signed_h2_context()` helper
  (`blackbull/fault_injection/_tls.py`) that mints an ephemeral
  RSA-2048 self-signed cert (SAN `DNS:localhost,IP:127.0.0.1`,
  ALPN `[h2, http/1.1]`).  Required for any client that only
  speaks HTTP/2 via ALPN over TLS (httpx, curl, browsers).
  `server.url` advertises `https://` when an SSL context is
  provided.
- **`[fault-injection]` install extra** — `pip install
  'blackbull[fault-injection]'` adds `cryptography` (the TLS
  helper) and `httpx[http2]` (the canonical client example).
  `H2FaultServer` itself only depends on the stdlib; users driving
  it over plaintext h2c can skip the extra.
- **`examples/scenario_h1_fault_injection.py`** — HTTP/1.1
  scenarios driven against stdlib `http.server` in a background
  thread.  Four scenarios: `well_formed_request`,
  `slowloris_trickle`, `partial_headers_idle`,
  `abort_after_request_line`.
- **`docs/guide/fault_injection.md`** — full tutorial covering
  the install extra, the two directions, the TLS quick-start, and
  a `pytest.parametrize`-shaped fixture pattern that fans the
  catalogue across a client under test.

### Changed

- **`examples/scenario_h2_fault_injection.py`** — rewritten to use
  httpx (`http2=True` over TLS) instead of a synthetic-byte test
  client.  Each catalogue scenario now demonstrates a distinct,
  named real-client error, making the example genuinely
  instructive.
- **`README.md`** restructured per the narrative proposal: leads
  with a one-sentence value prop, drops the reader into Hello
  World with curl output, rewrites the "Why BlackBull" bullets as
  benefits, gives fault injection its own section, moves the
  Early Alpha warning to after the feature tour, ends with a CTA.
  Adds an Event API section and a `websocket` row to the
  middleware table.  Fixes stale links (`docs/guide.md` →
  `docs/guide/index.md`, `docs/ActorDesign.md` →
  `docs/about/internals.md`).
- **`SECURITY.md`** — supported versions moved to 0.41.x + 0.40.x
  (was stuck at 0.28.x for thirteen MINOR releases).  In-scope
  list now covers the `blackbull.fault_injection` safety locks
  (`BB_PRODUCTION`, `allow_remote=`).  Out-of-scope deps cleaned
  up: removed `h2` (never a runtime dep), added the optional
  extras (`brotli`, `zstandard`, `uvloop`, `watchfiles`).

### Internal

- `blackbull.client.scenario` / `scenario_oracle` modules moved
  to `blackbull.fault_injection.scenario_h1` /
  `oracle_h1`.  Import paths under `blackbull.client.*` keep
  working as re-exports.
- `tests/unit/test_fault_injection_h2.py` — 21 tests covering
  server lifecycle, the frame-level step VM, every catalogue
  entry, and the TLS / ALPN handshake against a real httpx
  client.
- `_tls.py` lazily imports `cryptography` inside
  `_generate_self_signed_pem()` so importing
  `blackbull.fault_injection` works without the
  `[fault-injection]` extra installed (only calling the TLS
  helper requires it).

### Docs

- **`.claude/skills/pre-release-docs/`** (local-only) — new skill
  that audits `README.md` / `SECURITY.md` / `CHANGELOG.md` /
  `KNOWN_LIMITATIONS.md` / `docs/guide/*` / `mkdocs.yml` for
  staleness before tagging a release.  Cross-linked from
  `.claude/patterns/release.md`.

### Compatibility

Additive on the public Python surface — no existing import path
breaks, no behaviour of previously shipped APIs changes.
`blackbull.client.scenario` / `blackbull.client.scenario_oracle`
still resolve as deprecation shims emitting `DeprecationWarning`;
removal floor is v0.45.0 / 2026-09-16 per the project's
deprecation policy.  New surface (`blackbull.fault_injection.*`)
is public and stable from this release forward.

## [0.41.0] — 2026-06-16

**Sprint 45 close: HTTP/2 SSE with proper backpressure.**

Real-demand streaming pattern (LLM token streams, log tails)
shipped on the existing HTTP/2 sender + flow-control path
touched by Sprint 38 trailers.  Simplified-handler shape:
`async def stream(): yield ...` returning an async generator is
auto-wrapped in a `StreamingResponse`; a new
`EventSourceResponse` subclass adds WHATWG Server-Sent Events
formatting on top of the same iterator-driven pipeline.  No
protocol-level work was needed — `HTTP2Sender._write_data`
already blocks on the per-stream / per-connection
flow-control credit (RFC 9113 §6.9) so each `yield`
naturally throttles to the credit the peer has granted.

### Added

- **`EventSourceResponse`** (`blackbull/response.py`) — formats
  each yielded item per the WHATWG SSE grammar.  Accepts
  `str`, `bytes`, or `Mapping` (with optional `data` /
  `event` / `id` / `retry` keys).  Auto-emits
  `Content-Type: text/event-stream` and
  `Cache-Control: no-cache`; both overridable via the
  `headers=` argument.  Multi-line `data` strings split into
  one `data:` field per line per the spec; non-string `data`
  values are JSON-serialised.
- **Async generator return type** for simplified handlers
  (`blackbull/router.py`) — a route that returns an
  `async def stream(): yield ...` generator is now wrapped
  automatically.  A returned `StreamingResponse` (or any
  subclass, including `EventSourceResponse`) is passed
  through verbatim so it can drive `scope/receive/send`
  directly.
- **`docs/guide/streaming.md`** — covers the simplified async-
  generator shape, the `StreamingResponse` / `EventSourceResponse`
  classes, the HTTP/1.1 (drain-based) and HTTP/2
  (flow-control-credit-based) backpressure models with a
  pointer to the unit test that proves the stall+resume
  behaviour experimentally, and a comparison table for
  picking between SSE, WebSocket, and plain chunked HTTP.
- **`examples/sse_token_stream.py`** — end-to-end demo: a
  browser EventSource page subscribing to a `/sse` endpoint
  that fakes LLM tokens, plus a `/raw` endpoint showing the
  bare async-generator handler shape.

### Internal

- `tests/unit/test_sse.py` — 16 tests covering the SSE
  encoder (data lines, event/id/retry fields, multi-line
  split, dict-data JSON encoding, unsupported-type
  TypeError), `EventSourceResponse` ASGI event shape
  (content-type + cache-control headers, one body event per
  yield, final empty body close, caller-supplied
  cache-control wins), the simplified-handler dispatcher
  (async-generator wraps to `StreamingResponse`,
  `StreamingResponse` instance passes through,
  `EventSourceResponse` instance passes through to take the
  subclass branch not the Response branch), and an HTTP/2
  backpressure test that forces both windows to zero before
  the write starts and confirms no DATA bytes hit the wire
  until the `_window_open` event fires.

### Compatibility

Additive surface — no existing handler shape changes
behaviour.  The new async-generator branch sits ahead of the
existing `bytes` / `str` / `dict` / `Response` dispatch in
`_adapt_handler`; handlers that used to raise `TypeError` on a
generator return now succeed.

## [0.40.1] — 2026-06-15

**Patch release: cap-hit observability follow-ups.**

Two correlation / resilience gaps in the Sprint 44 cap-hit
logger, addressed in a single inter-sprint patch.  Sprint 45
(HTTP/2 SSE with proper backpressure) remains the next sprint
and is unaffected.

### Added

- Every `blackbull.caps` record (first-hit, intermediate
  summary, and graceful-close summary) now carries a
  `connection_id` field in `record.extra`.  The id is an
  8-character hex string auto-generated per
  `CapHitCounter` and accessible via the new
  `CapHitCounter.connection_id` property.  Pass an explicit
  string to `CapHitCounter(connection_id=...)` when integrating
  with an upstream correlation system.  Lets log aggregation
  pipelines (SIEM / Loki / Datadog) join records from a single
  connection even when `peer` is shared across many clients
  behind a NAT / CGNAT.  Resolution order on the emission path:
  explicit `connection_id=` kwarg → active counter's id → None.
- `CapHitCounter` gains two dirty-flush triggers so a
  connection torn down by RST (or any abnormal close that skips
  the graceful `flush()` path) still emits a summary for
  suppressed hits:
    - **Threshold trigger** (`flush_threshold=`, default 100)
      — after this many suppressed hits on any single cap, emit
      one intermediate summary per non-zero cap and reset
      counts.  Defends against an attacker that RSTs after
      every cap-hit to suppress summaries entirely.  Set to 0
      to disable.
    - **Interval trigger** (`flush_interval=`, default 60.0 s)
      — an asyncio timer task lazily armed on the first
      suppressed hit emits + resets after this many seconds if
      any cap has suppressed hits.  Cancelled on every reset
      (threshold trigger, timer fire, or graceful `flush()`).
      Set to 0 to disable.
- Intermediate summaries carry the marker text "(connection
  still open)" in the message so subscribers can distinguish
  them from the graceful-close summary without inspecting
  state.

### Internal

- `tests/unit/test_cap_log.py` extended with 10 new tests:
  `connection_id` propagation through emission and summary;
  auto-generation produces unique 8-hex IDs; explicit
  `connection_id=` kwargs honoured; threshold trigger emits
  intermediate summary at boundary; threshold resets and
  resumes (two summaries from a single cap); interval timer
  fires after the configured delay; disabled triggers (both 0)
  yield no intermediate emission; threshold cancels pending
  interval timer (no double summary); graceful `flush()`
  cancels pending interval timer.
- `tests/unit/test_cap_log_sites.py` upgraded:
  the previously signature-shaped `h2_max_concurrent_streams`
  and `h2_ws_max_streams_per_connection` tests now drive the
  real rejection sites in `HTTP2Actor._on_headers_frame` and
  `_handle_h2_websocket` with `MagicMock(spec=Stream)` /
  `MagicMock(spec=asyncio.TaskGroup)` so they exercise the cap
  guard end-to-end while still satisfying beartype.
- `tests/unit/test_max_connections_503.py` extended to assert
  the `max_connections` cap-hit record fires alongside the 503
  + Retry-After response — a functional pass through
  `ASGIServer.client_connected_cb` rather than a direct
  `log_cap_hit()` call.

### Compatibility

`CapHitCounter()` is backwards-compatible — all new parameters
are keyword-only with sensible defaults.  Existing call sites
in `blackbull/server/connection_actor.py` need no change; the
counter auto-generates a `connection_id` and arms the
dirty-flush triggers transparently.

## [0.40.0] — 2026-06-15

**Sprint 44 close: cap-hit observability.**

Every user-tunable resource cap in BlackBull (header sizes, the
four timeouts, connection cap, WebSocket frame cap, HTTP/2
stream caps, compression in-flight, and the HTTP/2 per-stream
queue) now emits one `WARNING`-level record on the new
`blackbull.caps` logger when it rejects traffic.  Before this
sprint a cap firing was silent — the peer saw a 503 / CLOSE 1009
/ RST_STREAM / dropped event but operators got nothing.  The
1 MiB WebSocket frame default that shipped in v0.35.0 and was
caught by the v0.39.0 conformance lane is the kind of regression
this surfaces immediately.

A single misbehaving peer cannot flood the log: each
`ConnectionActor` carries a `CapHitCounter` bound on a
`contextvars.ContextVar`, so the first hit per
`(connection, cap)` logs in full and subsequent hits are
silently counted; one summary record per suppressed cap fires
on connection close.

### Added

- `blackbull/server/cap_log.py` — the single emission point
  `log_cap_hit()` plus `CapHitCounter` with the
  first-hit-then-summary rate-limit pattern.  `CapHitCounter.bind()`
  is a context manager that installs the counter on a
  `contextvars.ContextVar`; `ConnectionActor.run()` does this
  once per connection so every actor / stream / recipient task
  spawned under it picks up the counter without constructor
  plumbing (TaskGroup children inherit the context automatically).
- `tests/unit/test_cap_log.py` — 15 unit tests covering the
  helper in isolation: emission, rate limiting, summary on
  flush, ambient binding, child-task inheritance via TaskGroup.
- `tests/unit/test_cap_log_sites.py` — coverage gate: one test
  per inventory cap plus a parametrised static audit that fails
  CI if a future PR adds a `BB_*` cap to the inventory list
  without wiring a `log_cap_hit('<cap>', ...)` call.

### Internal

Cap rejection sites wired to `log_cap_hit()` — twelve in all:

- `BB_MAX_CONNECTIONS` — accept loop in
  `blackbull/server/server.py` (process-scoped, no counter
  needed; an adversary cannot loop past the cap).
- `BB_HEADER_TIMEOUT` — slowloris defences in
  `blackbull/server/connection_actor.py` (ALPN-h2 preface +
  cleartext first-line) and `blackbull/server/http1_actor.py`
  (header-completion phase).
- `BB_HEADER_MAX_LINE` and `BB_HEADER_MAX_TOTAL` — H/1.1 parser
  in `blackbull/server/http1_actor.py`; HTTP/2 CONTINUATION
  guard in `blackbull/server/http2_actor.py`.
- `BB_BODY_TIMEOUT` — H/1.1 recipient in
  `blackbull/server/recipient.py` (was indistinguishable from
  EOF mid-body before; now split so the timeout path logs).
- `BB_REQUEST_TIMEOUT` — H/1.1 and H/2 paths.
- `BB_WRITE_TIMEOUT` — both write paths in
  `blackbull/server/sender.py` (`AsyncioWriter.write` and
  `AsyncioWriter.writelines`).
- `BB_WS_MAX_FRAME_PAYLOAD` — WebSocket frame guard in
  `blackbull/server/recipient.py:WebSocketRecipient._read_loop`.
- `BB_H2_MAX_CONCURRENT_STREAMS` — both stream-open guards in
  `blackbull/server/http2_actor.py`.
- `BB_H2_WS_MAX_STREAMS_PER_CONNECTION` — RFC 8441 WS guard.
- `BB_COMPRESSION_MAX_INFLIGHT` — executor-saturation bypass in
  `blackbull/middleware/compression.py`.
- HTTP/2 per-stream queue drops in
  `blackbull/server/recipient.py:HTTP2Recipient` (logged under
  the cap name `stream_queue_depth`).

`BB_WS_QUEUE_DEPTH` was deliberately **not** wired — the
WebSocket event queue applies backpressure via blocking
`await put()` rather than dropping, so a hit is normal flow
control rather than a rejection.

### Docs

- `docs/guide/logging.md` gains a *Cap-hit log — `blackbull.caps`*
  section covering the inventory, record shape (the `cap`,
  `requested`, `limit`, `peer`, `scope_path`, `protocol`
  structured fields), the rate-limit model, and a
  ready-to-paste subscription recipe.
- `docs/reference/env-vars.md` gains a section-header note in
  *Connection limits and timeouts* pointing at the new logging
  section, plus a one-liner under *Logging* on how to set the
  `blackbull.caps` level programmatically.

## [0.39.1] — 2026-06-15

**Patch release: two cross-platform bug fixes surfaced via the
proposals folder.**

Inter-sprint patch — no sprint scope change.  Two reproducible
defects reported by external triage notes were both verified
against current `master` and fixed in the smallest possible
diff.  Sprint 44 (cap-hit observability) remains the active
sprint and is unaffected.

### Fixed

- `blackbull/server/server.py` — `SocketManager` no longer
  crashes on platforms where `socket.AF_UNIX` is undefined
  (notably some Windows builds where the `socket` module ships
  without Unix-domain socket support).  The attribute is now
  resolved via `getattr` once at context entry; sockets are
  compared against the sentinel only when it is non-`None`.
  Before the fix, every accepted connection raised
  `AttributeError: module '_socket' has no attribute 'AF_UNIX'`
  on those platforms, making BlackBull unusable.
- `blackbull/response.py` — `Response(..., headers=[('Foo', 'bar')])`
  with `str`-typed tuple elements is now accepted.  The
  constructor coerces both key and value to ASCII `bytes`
  (per RFC 9110 §5.5) on the way in so the sender's later
  `b''.join(parts)` no longer raises
  `TypeError: sequence item N: expected a bytes-like object,
  str found`.  Bytes-typed tuples continue to pass through
  unchanged.  Non-ASCII input raises `UnicodeEncodeError` at
  construction time rather than letting obs-text bytes onto
  the wire.

### Internal

- `tests/unit/test_socket_manager_af_unix.py` — regression test
  that monkeypatches `socket.AF_UNIX` away and exercises
  `SocketManager` against a real AF_INET socket; would have
  caught the Windows crash had it existed earlier.
- `tests/unit/test_response.py` — added
  `test_response_str_headers_coerced_to_bytes`.

## [0.39.0] — 2026-06-15

**Sprint 43 close: conformance lane.**

The h2spec + Autobahn external conformance suites are now wired
into CI alongside a new docker-free regression replay of the
HTTP/1.1 differential user-corpus.  A push or PR to `master`
triggers
[`conformance.yml`](https://github.com/TOKUJI/BlackBull/blob/master/.github/workflows/conformance.yml)
on a fresh `ubuntu-latest` runner; the README's *RFC conformance*
badge tracks workflow status, and per-run artefacts (h2spec
JUnit XML, Autobahn `index.json`, pytest output) are attached
for 30 days.  A weekly cron run catches upstream container /
binary-release regressions between pushes.

### Added

- `.github/workflows/conformance.yml` — three-job workflow
  running h2spec (RFC 9113 + RFC 7541), Autobahn|Testsuite (RFC
  6455 + RFC 7692), and the docker-free corpus replay on push,
  PR, and weekly cron.
- `tests/conformance/http1/test_h1_user_corpus_replay.py` —
  reads each `diff_*.meta.json` sidecar in
  `tests/conformance/http1/fuzz/user-corpus/`, sends the
  recorded `wire_request_latin1` to a live in-process
  BlackBull, and asserts the response status still matches the
  recorded `blackbull_status`.  Runs in well under a second
  with no Docker dependency.  Complements the full nginx-
  oracle differential test (which still requires Docker and so
  skips on most CI runners).
- `bench/conformance/h2spec_app.py` — minimal HTTPS+HTTP/2
  fixture for h2spec, matching the shape of `autobahn_app.py`
  so the CI workflow can start a self-contained conformance
  server.

### Changed

- WebSocket inbound-frame payload cap default raised from **1 MiB
  to 64 MiB** and made configurable via the new
  `BB_WS_MAX_FRAME_PAYLOAD` env var.  The cap itself (Sprint 39
  v0.35.0) is the right defense against an adversary advertising
  a 2^63 - 1 payload to OOM the server; the original 1 MiB
  *value* was conservative enough to silently regress
  Autobahn|Testsuite 9.1.4-9.1.6 / 9.2.4-9.2.6 / 9.3.9 / 9.4.9
  (4–64 MiB single- and fragmented-message cases) that BlackBull
  passed pre-v0.35.0.  The Sprint 43 conformance CI lane
  surfaced the regression on its first run — exactly the
  function the lane was wired in for.  64 MiB matches the
  largest Autobahn 9.x case while still bounding per-connection
  memory; lower for stricter exposure (e.g. 1 MiB matching the
  `python-websockets` default).
- `docs/about/conformance.md` gains a *Verifying your fork
  stays RFC-correct* section with a five-step recipe (pytest →
  corpus replay → h2spec → Autobahn → CI push) and a *Docker-
  free regression replay* subsection under the differential
  corpus discussion.  The coverage-summary table now reflects
  CI coverage for h2spec / Autobahn / RFC 8441.
- `README.md` gains the *RFC conformance* badge linking to the
  workflow runs.  No peer-framework claims (per
  `feedback_public_docs_humble`).

## [0.38.0] — 2026-06-14

**Sprint 42 close: prove the extension surface.**

The Sprint 40 `init_app(app)` convention now has a real second
implementation living outside the BlackBull repo: the
[`blackbull-session`](https://github.com/TOKUJI/blackbull-session)
package on PyPI.  Packaging the existing in-tree `Session` middleware
as a separate distribution was the proving exercise — the convention
survived contact with a real cross-repo release cycle, OIDC trusted
publishing, dependency floor selection, and a deprecation cycle, and
no convention adjustments were needed.

### Added

- `docs/guide/extensions.md` gains a *Patterns and pitfalls from real
  extractions* appendix, capturing concrete decisions from the
  `blackbull-session` packaging (dependency floor, public middleware
  helpers, eager+deferred construction, the `extension_key` class
  attribute, `app` as first positional argument, the
  `blackbull[testing]` dependency for downstream test suites, and the
  recommended deprecation cycle).
- `docs/guide/extensions.md` gains a *Common extension categories*
  fair-treatment section, listing reasonable shapes for sessions,
  authentication, authorisation, observability, rate limiting,
  caching, database integration, background tasks, admin panels, CORS
  / CSRF, WebSocket helpers, and static / template engines.  No
  endorsements — the framework does not curate a "blessed extension"
  registry.

### Deprecated

- `blackbull.middleware.Session` now emits `DeprecationWarning` on
  construction.  Migrate to `pip install blackbull-session` and
  `from blackbull_session import SessionExtension`.  The in-tree
  form will be removed no earlier than BlackBull v0.41 (and not
  before 2026-07-14) — see the
  [Patterns and pitfalls](https://github.com/TOKUJI/BlackBull/blob/master/docs/guide/extensions.md#deprecating-an-in-tree-class-youre-extracting)
  section for the deprecation-window policy.

### Changed

- `examples/ChatServer/chatserver.py` migrated from the in-tree
  `Session` middleware to `SessionExtension`, demonstrating the
  `pip install blackbull-session` adoption path.
- `docs/guide/middleware.md` Session section rewritten to lead with
  the `blackbull-session` extension; an admonition documents the
  deprecation and removal target.

### Internal

- The audit of `OpenAPIExtension` against the documented convention
  found a 1:1 match (`extension_key` class attribute, eager + deferred
  construction, collision check with `is not self` idempotence, `app`
  as first positional argument).  No back-port required.

## [0.37.0] — 2026-06-14

**Sprint 41 close: OpenAPI as the reference implementation of the
`init_app(app)` extension convention.**

Most of the OpenAPI surface (`generate_spec`, `swagger_ui_html`, v1
router introspection, v2 type → JSON-schema synthesis with dataclass
support, the user guide, and 30 unit tests) was already in tree from
earlier work.  Sprint 41 closes the remainder: the public
`OpenAPIExtension` class, an `openapi-spec-validator` conformance
check, and the doc + example updates that make this the canonical
example of Sprint 40's extension convention.

### Added
- `blackbull.openapi.OpenAPIExtension` — the in-tree reference
  implementation of the `init_app(app)` extension convention.
  Supports both the eager `OpenAPIExtension(app, ...)` and deferred
  `ext = OpenAPIExtension(...); ext.init_app(app)` construction styles
  documented in the extensions guide.  Raises `RuntimeError` with
  module attribution when another extension is already registered
  under `app.extensions['openapi']`.  Retains handler references on
  `self` so the registered routes survive past `init_app` return.
- `tests/unit/test_openapi.py`: +6 `TestOpenAPIExtension` cases
  (deferred / eager / `docs_path=None` / collision / same-instance
  idempotency / `enable_openapi` parity) plus 2 conformance cases
  that validate the generated spec against `openapi-spec-validator`
  for both the fixture app and a dataclass-driven app.
- `openapi-spec-validator` added to the `[testing]` extra so the
  conformance check runs with `pip install -e '.[testing]'`.

### Changed
- `BlackBull.enable_openapi(...)` refactored to a thin delegating
  wrapper around `OpenAPIExtension`.  Behaviour and signature are
  unchanged; the core convenience method is now written in terms of
  the public extension surface — the same shape third-party authors
  use.
- `examples/SimpleTaskManager/app.py` switched from
  `app.enable_openapi(...)` to `OpenAPIExtension(app, ...)` to
  demonstrate the reference form in a real end-to-end app.

### Docs
- `docs/guide/openapi.md`: new "The `OpenAPIExtension` class" section
  showing the two construction styles and when to prefer them over
  the convenience method.  Query-parameter gap noted under "What's
  not yet automated" — the simplified-handler model has no annotation
  source for query params today, so they're not emitted.
- `docs/guide/extensions.md`: new "In-tree reference:
  `OpenAPIExtension`" callout pointing readers to the OpenAPI
  module as the concrete example of the convention.
- `docs/about/internals.md`: post-Sprint-40 audit corrections.
  `ServerActor` (which doesn't exist as a class) is renamed to
  `ASGIServer` in 4 sites (hierarchy diagram, dedicated section
  heading, supervisor strategies table row, exception propagation
  table row).  `app_startup` / `app_shutdown` attribution corrected
  to `BlackBull._handle_lifespan` rather than the server.
  `RequestActor` added under `StreamActor` (HTTP/2) in the hierarchy
  diagram since the H/2 path also delegates through it for the
  ASGI call.

### Migration risk
Zero.  `BlackBull.enable_openapi(...)` signature and behaviour are
unchanged; `OpenAPIExtension` is purely additive.

## [0.36.0] — 2026-06-14

**Sprint 40 close: slim extension surface.**

### Added
- `BlackBull.extensions: dict[str, object]` — namespace for
  third-party integrations following the `init_app(app)`
  convention.  Empty at construction; extensions write themselves
  into it under a documented key.
- `app.on_error(int)` — accepts a plain `int` HTTP status code
  in addition to `HTTPStatus` and exception classes.  Coerced to
  `HTTPStatus` internally; ergonomic shortcut for extension code
  that already uses raw status codes.

### Docs
- New guide page `docs/guide/extensions.md` covering the
  `app.extensions` namespace, the `init_app(app)` convention,
  the `blackbull-<name>` → `app.extensions['<name>']` key
  convention with `RuntimeError` collision detection, and the
  author-managed dependency-ordering pattern with the
  prerequisite-check idiom.

## [0.35.0] — 2026-06-14

**Sprint 39 close: RFC 8441 interop, default-on safety guards,
HTTP/2 + WebSocket security hardening.**

Sprint 39 closed the RFC 8441 (WebSocket-over-HTTP/2) interop
gap with a public client, then layered in the safety guards
needed before the eventual `BB_H2_ENABLE_WEBSOCKET` default
flip — plus three security-hardening fixes for pre-existing
exploitable gaps in the HTTP/2 and WebSocket paths.  One of
those fixes (server-side connection-level `WINDOW_UPDATE` on
inbound DATA) was surfaced during the WS-over-H2 64 KiB interop
test but turned out to affect any HTTP/2 upload past 65,535
cumulative bytes per connection — pre-Sprint-39 hangs on
plain HTTP/2 POST workloads above that boundary.

### Security

- **HTTP/2 CONTINUATION header-block size cap** (high severity).
  `HTTP2Actor._on_continuation_frame` previously appended every
  CONTINUATION payload to `header_frame.raw_block` with no size
  limit; an attacker could flood the server with CONTINUATION
  frames until OOM.  Mirror the HTTP/1.1 `BB_HEADER_MAX_TOTAL`
  budget (64 KiB default) and emit `RST_STREAM(ENHANCE_YOUR_CALM)`
  (RFC 6585 §5 / RFC 9113 §7) on over-cap streams — the same
  error code nginx and Envoy use.
- **WebSocket frame payload size cap** (medium severity, requires
  established WebSocket connection).  `WebSocketRecipient._read_loop`
  did not bound the declared payload length; a post-handshake
  adversary could advertise a 2**63 - 1 payload (RFC 6455 §5.2
  maximum) and the server would attempt to buffer it.  New
  `_MAX_FRAME_PAYLOAD` class attribute (default 1 MiB) +
  `max_frame_payload` constructor parameter — the check fires
  *before* any body bytes are read off the wire, raises
  `FramePayloadTooLarge` from `read_payload`, and the recipient
  translates it into `CLOSE(1009)` (MESSAGE_TOO_BIG).
- **HTTP/2 inbound RST_STREAM rate limit** (high severity —
  CVE-2023-44487 "Rapid Reset").  Per-second rolling counter on
  inbound `RST_STREAM` frames.  Over `_RST_RATE_LIMIT=20/s`, the
  connection is closed with `GOAWAY(ENHANCE_YOUR_CALM)`; a fresh
  handshake is required to retry.  The check is placed before
  stream-state validation so both the canonical attack shape
  (`HEADERS`+`RST_STREAM` cycles) and abusive RSTs on idle
  streams count toward the budget.

### Added

- **`BB_H2_WS_MAX_STREAMS_PER_CONNECTION`** (default `5`) caps
  concurrent WebSocket (Extended CONNECT) streams per HTTP/2
  connection.  Defends against stream-exhaustion DoS: without this
  cap, an attacker can hold up to `BB_H2_MAX_CONCURRENT_STREAMS`
  (default 100) idle WS streams per connection across an unbounded
  `BB_MAX_CONNECTIONS` (default 0).  `0` disables the cap (no upper
  bound beyond `BB_H2_MAX_CONCURRENT_STREAMS`).  Only meaningful
  when `BB_H2_ENABLE_WEBSOCKET=1`.  Exceeded requests receive
  `RST_STREAM(REFUSED_STREAM)`; the cap is per-connection, not
  global.
- **`blackbull.client.WebSocketH2Client` / `WebSocketH2Session`** —
  public RFC 8441 client built on `HTTP2Client` and BlackBull's own
  `ws_codec.encode_frame`.  Splits outgoing WebSocket payloads
  across multiple H2 DATA frames at `max_frame_size`, emits
  `WINDOW_UPDATE` for stream + connection-level receive flow control,
  and runs the Extended CONNECT handshake through a small
  `register_raw_stream` mechanism on `HTTP2Client`.

### Fixed

- **Server-side connection-level `WINDOW_UPDATE` on inbound DATA**
  (RFC 9113 §6.9.1).  `HTTP2Actor._on_data_frame` previously
  credited only the stream-level window when delivering a DATA
  frame, leaving the connection-level receive window depleting
  toward zero across requests.  Any single request body — or
  cumulative inbound across a keep-alive H2 connection — past
  65,535 bytes stalled waiting for credit that never came.  The
  server now sends both `WINDOW_UPDATE(stream_id, length)` and
  `WINDOW_UPDATE(0, length)` after delivery.  Surfaced during the
  WS-over-H2 64 KiB interop test; the bug was broader than RFC 8441
  and affects any large H2 upload.
- **`HTTP2WSReader` unbounded buffer growth**.  Without a cap,
  ``put_DATAFrame`` credited every incoming DATA frame's window
  even when the WS actor wasn't draining — a misbehaving peer
  could grow ``_buffer`` without bound.  Now caps at ``max_buffer``
  (default 1 MiB) with a credit-on-drain backpressure model: bytes
  are always buffered (no silent loss — the peer's window already
  debited on the wire), but ``WINDOW_UPDATE`` is withheld while
  over the cap and replayed once ``readexactly`` drains back
  under it.  `_on_data_frame` recognises the
  `backpressures_via_credit` marker so the backpressure path
  doesn't `RST_STREAM` the connection; recipients without the
  marker keep the legacy `ENHANCE_YOUR_CALM` semantics.

### Docs

- `KNOWN_LIMITATIONS.md` — the RFC 8441 section now documents the
  stream-exhaustion attack surface and the recommended mitigations
  (nginx frontend or finite `BB_MAX_CONNECTIONS`).
- `docs/reference/env-vars.md` — new `BB_H2_WS_MAX_STREAMS_PER_CONNECTION`
  row in the WebSocket table, and a production-posture note on
  `BB_H2_ENABLE_WEBSOCKET` pointing at the nginx-frontend shape.

### Internal

- `HTTP2Client.register_raw_stream(stream_id)` — per-stream queue
  for raw frame I/O, used by `WebSocketH2Client` to receive frames
  on a stream without racing the receive loop.  Connection-level
  frames (WINDOW_UPDATE, SETTINGS) bypass the raw-stream queue so
  flow-control state stays consistent.
- `HTTP2Actor._make_done_cb(stream_id, *, is_ws=False)` —
  consolidates per-stream lifecycle cleanup (the existing
  `_active_stream_count` decrement, the sender/recipient dict
  evictions, and the new RFC 8441 `_ws_stream_count` decrement)
  in one site.  `is_ws=True` opts the WS counter in at the call
  site so regular HTTP stream completions don't silently drift
  the WS counter below the true in-flight count.
- `HTTP1Sender` / `HTTP2Sender` — `reset_per_request_state()`
  encapsulates the per-keep-alive-request reset block surfaced by
  Sprint 38's `BB_REQUEST_TIMEOUT` work.  `HTTP1Sender` also
  extracts `_ensure_framing_headers` / `_ensure_date_header` helpers
  shared by `_flush` and `_pathsend`.  `HTTP2Sender`'s bytes
  `__call__` path now carries the same `_end_stream_sent` defensive
  guard the dict path got in Sprint 38.

### Status

- `BB_H2_ENABLE_WEBSOCKET` remains opt-in (default `False`).
  Sprint 39 lands the interop coverage + safety guards so the
  eventual default flip does not regress the project's security
  posture.

---

## [0.34.0] — 2026-06-13

**Sprint 38 close: cross-protocol parity.**

Two of the same family of HTTP/1.1 ↔ HTTP/2 inconsistencies, one
direction in each path — closed in one sprint.

### Added

- **HTTP/2 response trailers** (`http.response.trailers`).  The
  HTTP/2 sender at `blackbull/server/sender.py` now emits a
  `HEADERS` frame with `END_STREAM | END_HEADERS` and regular
  fields only (no pseudo-headers, per RFC 9113 §8.1).  Previously
  this event logged `HTTP2Sender: unhandled event type` and was
  silently dropped — an ASGI 3.0 conformance gap and the
  prerequisite primitive for any future gRPC work.  Receive-side
  trailers (scope-passed-to-handler) remain out of scope.
- **`BB_REQUEST_TIMEOUT` on the HTTP/1.1 path.**  Previously the
  env var applied only to HTTP/2 streams (via
  `HTTP2Actor._spawn_stream_task`'s `asyncio.wait_for` wrapper);
  the HTTP/1.1 path ran handlers unbounded.  Now the HTTP/1.1
  keep-alive loop wraps each dispatch with the same
  `asyncio.wait_for` guard.  On expiry the server emits
  `408 Request Timeout` with `Connection: close` (and synthesises
  the response cleanly when the handler had only buffered
  `http.response.start` without flushing it to the wire) and
  closes the connection — no keep-alive across a timed-out
  request.  `0` (the default) preserves the pre-Sprint-38
  unbounded behaviour.
- **Defensive `END_STREAM`-already-sent guard on `HTTP2Sender`.**
  RFC 9113 §8.1 — frames after `END_STREAM` are a connection
  error.  If an ASGI application erroneously sends another
  `http.response.body` / `http.response.start` /
  `http.response.trailers` event after the response is complete,
  the sender now logs a warning and drops the event rather than
  writing a frame the peer would treat as a protocol violation.
  Control-plane frames (`WINDOW_UPDATE`, `RST_STREAM`, `GOAWAY`,
  …) bypass the guard since the framework needs to send those
  after the response ends.

### Fixed

- **`HTTP1Sender` per-request state was sticky across keep-alive
  requests.**  The sender is constructed once per TCP connection
  and reused across N keep-alive requests, but
  `_started`/`_chunked`/`_buffered_status`/`_buffered_headers`/
  `_expect_trailers` were never reset between requests.  After
  the first response, the `_started` flag stayed `True`, which
  caused the new `BB_REQUEST_TIMEOUT` synthesis to silently skip
  the 408 emit on a second-or-later keep-alive request (because
  the timeout branch checks `if not send._started`).  Reset
  inline in HTTP1Actor's keep-alive loop alongside the existing
  `send._head_mode` / `send._log_record` resets.  Pre-Sprint 38
  this had no externally observable effect because no caller
  consulted `_started`; the new timeout path required it.
- **`HTTP1Actor._dispatch_request` was swallowing
  `CancelledError`.**  The aggregator path's
  `try: await request_actor.run() except BaseException: return
  False` deliberately catches handler errors to keep the
  keep-alive loop alive — but `asyncio.wait_for`'s cancellation
  mechanism IS a `CancelledError`, so the swallow silently
  turned timeouts into normal close-without-response.  Inserted
  `except asyncio.CancelledError: raise` ahead of the
  `BaseException` catch so `wait_for` sees the cancellation and
  raises `TimeoutError` to the outer keep-alive loop.

### Docs

- **`intercepting_send` middleware pattern documented.**  Added a
  "Post-response middleware (inspect / modify the response)"
  subsection to [`docs/guide/middleware.md`](docs/guide/middleware.md)
  showing the worked status-logger example, a table mapping
  common goals (add a response header, compute a checksum,
  replace the body, short-circuit a status code) to the right
  hook point inside the wrapped `send`, a pointer to
  `Compression` as the reference implementation, and a
  streaming-buffering caveat.  Previously this pattern was used
  internally by `Compression` and `Cache` but only discoverable
  by reading their source.
- **`BB_REQUEST_TIMEOUT` doc framing updated.**  `docs/reference/
  env-vars.md` and the `blackbull/env.py` module docstring no
  longer describe it as a "Per-HTTP/2-stream deadline" — the
  cross-protocol behaviour is the new framing, with the
  protocol-specific cancellation mechanism described inline
  (RST_STREAM CANCEL on HTTP/2; 408 + `Connection: close` on
  HTTP/1.1).

### Conformance

- 19 new tests under
  [`tests/conformance/http2/test_rfc9113_trailers.py`](tests/conformance/http2/test_rfc9113_trailers.py)
  (frame shape, no-pseudo-headers, empty trailers,
  body-then-trailers, field encoding, trailers-only response,
  sender contract, cross-protocol symmetry,
  no-longer-unhandled).
- 20 new tests under
  [`tests/conformance/http1/test_http1_request_timeout.py`](tests/conformance/http1/test_http1_request_timeout.py)
  (408 + close, fast-handler unaffected, disabled-by-zero,
  boundary, isolation, pipelining, custom value, buffered-start
  + timeout, keep-alive second-request reset).

---

## [0.33.1] — 2026-06-12

**Brotli default quality aligned with documented dynamic-content
usage.**

The brotli library's own default — used implicitly by the
`Compression` middleware in 0.33.0 and earlier — is quality 11,
designed for build-time / static pre-compression of assets that
will be served thousands of times from disk.  Applied to live
dynamic responses, q=11 spends 5–15 ms of CPU per response on
small payloads, saturating the event loop under any load.

This release sets the dynamic-response default to **q=4** and
makes the value configurable.  The fix follows the brotli
library's intended usage modes (q=4–6 dynamic, q=11 offline
static) rather than introducing a benchmark-mode toggle.

### Changed

- **Brotli default quality lowered from 11 to 4** for the
  `Compression` middleware's dynamic-response path.  q=4 matches
  Google's and Cloudflare's recommendation for dynamic content;
  q=5 matches Apache `mod_brotli`'s default; q=6 matches nginx
  `ngx_brotli`'s default.  q=11 remains the right pick for
  build-time pre-compression of static sibling assets (`.br`
  files served from disk) — never on live responses.

  Configurable via `BB_BROTLI_QUALITY` (env var) or
  `Compression(brotli_quality=...)` (constructor kwarg).
  Behavioural wire output is unchanged (still valid brotli);
  only CPU cost on the request path drops.

### Added

- `BB_BROTLI_QUALITY` env var and `Settings.brotli_quality`
  field.  Documented in
  [`docs/reference/env-vars.md`](docs/reference/env-vars.md).

### Tests

- `tests/unit/test_compression_brotli_quality.py` pins the
  module-level default (4), verifies the constructor kwarg
  propagates to the bound brotli callable via
  `functools.partial`, and round-trips the env var →
  `Settings` → middleware path.

---

## [0.33.0] — 2026-06-12

**Sprint 37 — defaults reset to RFC / kernel baselines; static
body cache becomes opt-in.**

This release moves BlackBull's defaults from a benchmark-tuned
posture to RFC 7540 / Linux kernel baselines, so a fresh install
behaves predictably regardless of host tuning state and the
framework can stand on its architecture alone.  The previous
tuned values are preserved as documented production
recommendations.

### Changed

- **`StaticFiles` body cache is now opt-in** (default
  `cache=False`).  `app.static(url_prefix, root_dir)` reads files
  from disk on every request unless explicitly opted in via
  `app.static(url_prefix, root_dir, cache=True)`.  Sibling
  existence (for `.br` / `.zst` / `.gz` precompressed serving) is
  recomputed per-request when the cache is off; memoised when on.
  Most production deployments terminate static traffic at nginx
  or a CDN and won't notice — standalone setups that previously
  benefitted from the in-process cache should opt in to keep
  prior performance.  See
  [`docs/guide/static-files.md`](docs/guide/static-files.md) for
  the full discussion.

- **Seven framework defaults reset to platform baselines**:

  | Setting | Pre-0.33 | 0.33 | Baseline source |
  |---|---|---|---|
  | `BB_SOCKET_BACKLOG` | 4096 | 128 | kernel `net.core.somaxconn` traditional default |
  | `BB_SOCKET_SNDBUF` | 262144 | 0 | kernel default (unchanged unless set) |
  | `BB_SOCKET_RCVBUF` | 262144 | 0 | kernel default (unchanged unless set) |
  | `BB_SOCKET_REUSEPORT` | True | False | kernel default |
  | `BB_TCP_USER_TIMEOUT_MS` | 60000 | 0 | kernel default (off) |
  | `BB_H2_INITIAL_WINDOW_SIZE` | 1048576 | 65535 | RFC 7540 §6.9.2 |
  | `BB_H2_CONNECTION_WINDOW_SIZE` | 4194304 | 65535 | RFC 7540 §6.9.2 minimum |

  Production deployments that need throughput should set these
  explicitly — the previous values plus per-variable rationale
  are documented under "Performance recommendations" in
  [`docs/reference/env-vars.md`](docs/reference/env-vars.md).

- **`BB_FRAME_YIELD_EVERY`, `BB_COMPRESSION_MAX_INFLIGHT`,
  `BB_KEEP_ALIVE_TIMEOUT` deliberately kept** at their previous
  values (8, `cpu*2`, 5.0 respectively).  These are
  correctness / fairness / safety mechanisms (cooperative-yield
  fairness, compression-offload backpressure cap, keep-alive
  idle timer), not numerical optimisations above a platform
  baseline.

### Added

- `cache` keyword parameter on
  [`StaticFiles.__init__`](blackbull/middleware/static.py) and
  [`app.static()`](blackbull/app.py) — opt in to the in-process
  body cache for standalone deployments that serve static
  traffic directly.

- [`docs/guide/static-files.md`](docs/guide/static-files.md) —
  rewrote "In-memory cache" as an opt-in feature with explicit
  when-to-turn-on / when-to-leave-off guidance.  New
  "Precompressed sibling serving" section documenting
  `.br` / `.zst` / `.gz` lookup as an official feature (same
  pattern as nginx's `gzip_static` / `brotli_static`), including
  the Range-bypass and `Vary: Accept-Encoding` behaviour.

- [`docs/reference/env-vars.md`](docs/reference/env-vars.md) —
  new "Performance recommendations" section that documents the
  pre-0.33 tuned values as production tuning targets with
  per-variable rationale.

### Tests

- 1,268 tests pass on the release commit, 196 skipped
  (testcontainer-gated), 0 failures.
- Two H/2 architecture handshake tests reshaped to set non-default
  values via `monkeypatch.setenv` + `reset_settings_cache()` and
  assert the actor honours the configured value, instead of
  tautologically asserting "value > RFC default" (which used to
  pass by coincidence on the tuned defaults).  The new shape is
  the right pattern for any future test reading framework-default
  numerics — assert behaviour, not magic constants.

### Notes

- **Migration**: standalone deployments serving static files
  directly should pass `cache=True` to `app.static(...)` to keep
  prior performance.  Deployments behind nginx / a CDN are
  unaffected — static traffic doesn't reach the framework on
  that topology.
- **Production tuning**: deployments that previously implicitly
  benefitted from the tuned socket / H/2 window defaults should
  set the recommended env vars explicitly — see
  `docs/reference/env-vars.md` "Performance recommendations" for
  the recipe.
- BlackBull has no production users yet (per `CLAUDE.md`, this
  is a personal learning project) so the default flip doesn't
  break anyone in the wild.

---

## [0.33.0] — 2026-06-12

**Sprint 37 — defaults reset to RFC / kernel baselines; static
body cache becomes opt-in.**

This release moves BlackBull's defaults from a benchmark-tuned
posture to RFC 7540 / Linux kernel baselines, so a fresh install
behaves predictably regardless of host tuning state and the
framework can stand on its architecture alone.  The previous
tuned values are preserved as documented production
recommendations.

### Changed

- **`StaticFiles` body cache is now opt-in** (default
  `cache=False`).  `app.static(url_prefix, root_dir)` reads files
  from disk on every request unless explicitly opted in via
  `app.static(url_prefix, root_dir, cache=True)`.  Sibling
  existence (for `.br` / `.zst` / `.gz` precompressed serving) is
  recomputed per-request when the cache is off; memoised when on.
  Most production deployments terminate static traffic at nginx
  or a CDN and won't notice — standalone setups that previously
  benefitted from the in-process cache should opt in to keep
  prior performance.  See
  [`docs/guide/static-files.md`](docs/guide/static-files.md) for
  the full discussion.

- **Seven framework defaults reset to platform baselines**:

  | Setting | Pre-0.33 | 0.33 | Baseline source |
  |---|---|---|---|
  | `BB_SOCKET_BACKLOG` | 4096 | 128 | kernel `net.core.somaxconn` traditional default |
  | `BB_SOCKET_SNDBUF` | 262144 | 0 | kernel default (unchanged unless set) |
  | `BB_SOCKET_RCVBUF` | 262144 | 0 | kernel default (unchanged unless set) |
  | `BB_SOCKET_REUSEPORT` | True | False | kernel default |
  | `BB_TCP_USER_TIMEOUT_MS` | 60000 | 0 | kernel default (off) |
  | `BB_H2_INITIAL_WINDOW_SIZE` | 1048576 | 65535 | RFC 7540 §6.9.2 |
  | `BB_H2_CONNECTION_WINDOW_SIZE` | 4194304 | 65535 | RFC 7540 §6.9.2 minimum |

  Production deployments that need throughput should set these
  explicitly — the previous values plus per-variable rationale
  are documented under "Performance recommendations" in
  [`docs/reference/env-vars.md`](docs/reference/env-vars.md).

- **`BB_FRAME_YIELD_EVERY`, `BB_COMPRESSION_MAX_INFLIGHT`,
  `BB_KEEP_ALIVE_TIMEOUT` deliberately kept** at their previous
  values (8, `cpu*2`, 5.0 respectively).  These are
  correctness / fairness / safety mechanisms (cooperative-yield
  fairness, compression-offload backpressure cap, keep-alive
  idle timer), not numerical optimisations above a platform
  baseline.

### Added

- `cache` keyword parameter on
  [`StaticFiles.__init__`](blackbull/middleware/static.py) and
  [`app.static()`](blackbull/app.py) — opt in to the in-process
  body cache for standalone deployments that serve static
  traffic directly.

- [`docs/guide/static-files.md`](docs/guide/static-files.md) —
  rewrote "In-memory cache" as an opt-in feature with explicit
  when-to-turn-on / when-to-leave-off guidance.  New
  "Precompressed sibling serving" section documenting
  `.br` / `.zst` / `.gz` lookup as an official feature (same
  pattern as nginx's `gzip_static` / `brotli_static`), including
  the Range-bypass and `Vary: Accept-Encoding` behaviour.

- [`docs/reference/env-vars.md`](docs/reference/env-vars.md) —
  new "Performance recommendations" section that documents the
  pre-0.33 tuned values as production tuning targets with
  per-variable rationale.

### Tests

- 1,268 tests pass on the release commit, 196 skipped
  (testcontainer-gated), 0 failures.
- Two H/2 architecture handshake tests reshaped to set non-default
  values via `monkeypatch.setenv` + `reset_settings_cache()` and
  assert the actor honours the configured value, instead of
  tautologically asserting "value > RFC default" (which used to
  pass by coincidence on the tuned defaults).  The new shape is
  the right pattern for any future test reading framework-default
  numerics — assert behaviour, not magic constants.

### Notes

- **Migration**: standalone deployments serving static files
  directly should pass `cache=True` to `app.static(...)` to keep
  prior performance.  Deployments behind nginx / a CDN are
  unaffected — static traffic doesn't reach the framework on
  that topology.
- **Production tuning**: deployments that previously implicitly
  benefitted from the tuned socket / H/2 window defaults should
  set the recommended env vars explicitly — see
  `docs/reference/env-vars.md` "Performance recommendations" for
  the recipe.
- BlackBull has no production users yet (per `CLAUDE.md`, this
  is a personal learning project) so the default flip doesn't
  break anyone in the wild.

---

## [0.32.0] — 2026-06-11

**Sprint 36 close — `TestClient`, per-stream `__slots__`, ASGI 3.0
compliance fixes.**

This release ships a new public surface
[`blackbull.testing.TestClient`](blackbull/testing.py) for in-memory
ASGI 3.0 testing, applies `__slots__` to the per-HTTP-stream and
per-frame hot path, and fixes three latent bugs that had silently
prevented BlackBull apps from running behind any external ASGI
server (uvicorn, hypercorn, `httpx.ASGITransport`).

### Added

- New module [`blackbull/testing.py`](blackbull/testing.py)
  exposing `TestClient`, `WebSocketTestSession`, and
  `WebSocketDisconnect`.  Synchronous façade over
  `httpx.AsyncClient` + `httpx.ASGITransport` with a dedicated
  background-thread event loop bridging sync calls, the ASGI
  lifespan protocol, and WebSocket sessions.  Full pass-through of
  httpx kwargs (`json=`, `content=`, `data=`, `files=`, `auth=`,
  `params=`, `headers=`, `cookies=`, `timeout=`,
  `follow_redirects=`); cookie / header jars exposed via
  `client.cookies` / `client.headers`.  Streaming WebSocket
  receives via `ws.iter_text()` / `ws.iter_bytes()`.

- `__slots__` on per-stream / per-frame hot-path classes —
  [`Stream`](blackbull/protocol/stream.py),
  [`BaseSender`](blackbull/server/sender.py),
  [`HTTP1Sender`](blackbull/server/sender.py),
  [`HTTP2Sender`](blackbull/server/sender.py),
  [`WebSocketSender`](blackbull/server/sender.py).  Removes the
  per-instance `__dict__` on the per-HTTP/2-stream and
  per-HTTP/1.1-request hot path.

- New doc page [`docs/guide/testing.md`](docs/guide/testing.md)
  leading with the `TestClient` pattern, with worked examples for
  HTTP, WebSocket streaming, lifespan, file upload, auth, timeout,
  and per-request kwargs.

### Fixed

- `BlackBull.__call__` is now correctly ASGI 3.0 compliant under
  external transports.  Three independent bugs that had locked the
  framework to its own server:

  - [`blackbull/app.py`](blackbull/app.py) `_wrap_send` was
    calling the external send with three positional args (the
    BlackBull-internal sender signature).  Now emits standard
    ASGI 3.0 `http.response.start` + `http.response.body` event
    dicts.

  - `scope['headers']` is normalised to a
    [`Headers`](blackbull/headers.py) instance once at the entry
    point.  External transports deliver the standard
    list-of-tuples; BlackBull handlers and helpers
    (`parse_cookies`, `TrustedProxy`, `StaticFiles`) reach into
    `Headers.get` / `.getlist`.

  - [`blackbull/request.py`](blackbull/request.py) `parse_cookies`
    now accepts both the `Headers` shape and the standard
    list-of-tuples — belt-and-braces with the `__call__`
    normalisation.

### Changed

- [`KNOWN_LIMITATIONS.md`](KNOWN_LIMITATIONS.md) rewritten to
  user-facing content only.  209 → 157 lines.  WSL2 measurement
  specifics, sprint references, and maintainer roadmap items moved
  into [`bench/CHARACTERIZATION.md`](bench/CHARACTERIZATION.md) and
  the sprint logs.  Renamed "Benchmark + measurement caveats" to
  "Deployment notes" with just the multi-worker scaling guidance.

- 14 integration test files migrated from `live_server` +
  `httpx.AsyncClient` to `TestClient` (net −271 lines).  Files
  testing the wire (HTTP/2, WebSocket, TLS, chunked streaming,
  static-file serving) stay socket-bound.

### Notes

- The `_wrap_send` fix means BlackBull apps now run unchanged
  under uvicorn / hypercorn / granian / any other ASGI 3.0
  server.  Before this release, they did not — every response
  crashed with a `TypeError` on the external send signature.
- BlackBull's own server is unchanged in behaviour; its internal
  sender already handled the dict event shape on its match arms.
- Second regression test for the Sprint 35 auto-release tooling:
  pushing the `v0.32.0` tag should automatically create the
  GitHub Release from this CHANGELOG section.

---

## [0.31.3] — 2026-06-10

**Static-path perf fix.**  `StaticFiles` + `Compression` middleware
chain on slim container images (`python:3.13-slim`, distroless, etc.)
no longer runs inline brotli at default quality 11 on already-
compressed font payloads.  Slim images ship no system MIME database,
so `mimetypes.guess_type('foo.woff2')` returned `None`,
`StaticFiles` fell back to `application/octet-stream`, and
`Compression`'s skip list did not recognise the type — brotli ran on
~22 KB WOFF2 bodies, blocking the worker for tens of ms per request.

### Changed

- [`blackbull/middleware/static.py`](blackbull/middleware/static.py)
  registers common web-asset MIME types at module import via
  `mimetypes.add_type`: `font/woff`, `font/woff2`, `image/webp`,
  `image/avif`, `application/wasm`.  Idempotent; benefits every
  `mimetypes.guess_type` caller — not just `StaticFiles`.
- [`blackbull/middleware/compression.py`](blackbull/middleware/compression.py)
  adds `font/woff`, `font/woff2`, `application/font-woff`,
  `application/font-woff2` to `_SKIP_CONTENT_TYPES`.  Belt-and-braces
  — the mime fix on its own resolves the deployed case, but the skip
  list keeps the middleware honest if a caller hand-sets a font
  Content-Type without the registration happening first.  `font/ttf`
  / `font/otf` / `font/sfnt` are intentionally NOT skipped — those
  are uncompressed font tables that do benefit from gzip / brotli.

### Added

- Phase-trace observability (`BB_PHASE_TRACE=1`) gains finer marks
  inline in the HTTP/1 sender (`start_arm_in`, `start_arm_out`,
  `body_arm_in`, `body_arm_out`) and new `AccessLogRecord` fields for
  request `Accept-Encoding` / `Range` and response `Content-Type` /
  `Content-Encoding`.  Each access-log line gains a
  `req[ae=… range=…] resp[ct=… ce=…]` trailer when phase trace is
  on.  Off by default; no per-request overhead in production.
- `publish.yml` now auto-creates a GitHub Release after the PyPI
  publish job succeeds, sourcing release notes from the matching
  `## [x.y.z]` section in this file.  v0.31.3 is the first release
  exercising this — the Release should appear at
  `https://github.com/TOKUJI/BlackBull/releases/tag/v0.31.3`
  automatically.

### Notes

- No public API change.  No migration needed.
- The static-path perf characteristics improve materially when
  serving WOFF / WOFF2 fonts behind `Compression` middleware on slim
  container images; deployments on hosts with a populated
  `/etc/mime.types` (Debian with `mime-support`, Ubuntu, macOS) were
  already fine and see no change.

---

## [0.31.2] — 2026-06-10

**README documentation links on PyPI now resolve.**  `v0.31.1`'s
README used GitHub-relative paths (`docs/guide.md`, `CHANGELOG.md`,
…) which render correctly on github.com but 404 under
`pypi.org/project/blackbull/`.  Rewritten as absolute
`https://github.com/TOKUJI/BlackBull/blob/master/<path>` URLs.

### Fixed

- Six relative documentation links in `README.md`
  (`docs/about/conformance.md`, `KNOWN_LIMITATIONS.md`,
  `docs/guide.md`, `docs/ActorDesign.md`, two refs to
  `CHANGELOG.md`) now point at GitHub so PyPI's rendered
  project page resolves them.

### Notes

- No code change; no API surface change; no migration needed.

---

## [0.31.1] — 2026-06-10

**Sprint 33 static-path perf fixes reach PyPI.**  `v0.31.0` shipped
on 2026-06-04 and predates Sprint 33's static-middleware work; the
three landed PRs below were on master but never made it into a
published wheel.  Sprint 34's release-management audit surfaced
the gap (see `bench/sprint-logs/sprint-34.md`).  No new code in
this release — just the cut from the correct git revision.

### Changed

- **Static cache documents `stat()`-based invalidation, not
  permanent staleness** (PR #47, `docs(known-limitations): clarify
  static cache is stat-invalidated`).
- **Static cache hits avoid `mimetypes.guess_type()` regex on every
  request** — mime is computed once at cache-store and held in the
  cache entry; `stat()` is throttled by `BB_STATIC_STAT_TTL_S`
  (default 1 s); response body + headers go out as a single
  `writelines()` vectored write instead of two `write()` calls
  (PR #48, `perf(static): cache mime + throttle stat + vectored
  write on cache hit`).
- **Static middleware hot path uses `os.path` instead of
  `pathlib.Path`** — `_root` is a `str`; traversal check is a
  single string-prefix comparison against the pre-computed
  `<root>/` form; precompressed-sibling existence is
  `os.path.isfile(target + suffix)`; `_root: Path` is kept as a
  back-compat property (commit `7b63fbe`,
  `perf(static): replace pathlib with os.path on the hot path`).

### Notes

- Public surface unchanged.  No deprecations; no migration needed.
- `v0.32.0` (Sprint 33 close release) will fold in the
  phase-trace API and Compression pass-through fast-path PRs
  currently in review.

---

## [0.31.0] — 2026-06-04

**Sprint 32 close — HTTP/2 stream-info ASGI extensions.**  Moves
the existing RFC 9218 priority hint from `scope['http2_priority']`
under `scope['extensions']` per ASGI convention and adds a new
HTTP/2 stream-info extension exposing `stream_id` and send-side
flow-control state.  Lays the foundation for future gRPC over
HTTP/2 work; no gRPC code in this release, but a written
assessment is included.

### Added

- **`scope['extensions']['http.response.priority']`** — RFC 9218
  priority hint at the conventional ASGI scope-extensions
  location.  Field name matches gunicorn's beta HTTP/2 surface;
  the *contents* are RFC 9218 urgency/incremental rather than the
  RFC 7540 weight/depends_on tree that RFC 9113 §5.3.2
  deprecated.  Shape: `{'urgency': int 0-7, 'incremental': bool}`.
- **`scope['extensions']['http.response.http2_stream']`** — new
  BlackBull HTTP/2 stream-info extension.  Snapshot at scope
  build time of `{'stream_id': int, 'send_window_remaining':
  int, 'connection_send_window_remaining': int}`.  Peer
  recv-window is intentionally absent (we send WINDOW_UPDATE per
  consumed DATA frame, so there's no scalar to snapshot).
- **`docs/about/grpc-assessment.md`** — written assessment of
  what gRPC over BlackBull would need, what's available today,
  what Sprint 32 unlocks (server-streaming back-pressure
  visibility), and what's still missing for a minimum gRPC
  server.  No commitment; the document is decision input for a
  future sprint.

### Changed

- **`docs/guide/http2.md`** leads with the new
  `scope['extensions']['http.response.priority']` location.  A
  *Migrating from `scope['http2_priority']`* subsection explains
  the rename.
- **`examples/PriorityExample/`** updated to read priority from
  the new extension location.  The example no longer reads
  `scope['http2_priority']`; that key remains populated only for
  the deprecation window.

### Deprecated

- **`scope['http2_priority']`** — the top-level scope key that
  carried the same RFC 9218 urgency/incremental dict.  Still
  populated for backwards compatibility during the v0.31 cycle
  and scheduled for removal in `0.32.0`.  Apps should read
  `scope['extensions']['http.response.priority']` instead.

### Tests

- 9 new unit tests in `tests/unit/test_http2_extensions.py`
  pinning the helper's per-request fresh-dict semantics,
  the priority extension contents (RFC 9218 default + explicit
  pass-through), and the http2_stream snapshot fields.
- 3 new integration tests in
  `tests/integration/test_http2_advanced.py` confirming the new
  extension keys show up in a real HTTP/2 scope and agree with
  the deprecation alias.

### Notes for adopters

- **Migrating from `scope['http2_priority']`.**  Replace
  `scope.get('http2_priority', DEFAULT)` with
  `(scope.get('extensions') or {}).get('http.response.priority', DEFAULT)`.
  The dict shape is unchanged.
- **HTTP/1.1 requests** do not advertise the new HTTP/2
  extensions.  `scope['extensions']` will contain
  `http.response.pathsend` (from v0.30) but not
  `http.response.priority` / `http.response.http2_stream`.
- **Window snapshot caveat.**  The send-window fields are taken
  at scope-build time; they shift as the response body streams.
  Live readings (e.g. for iterative gRPC server-streaming
  back-pressure) need a future sprint — see *Open question* in
  `docs/about/grpc-assessment.md`.

### Out of scope / deferred

- **HTTP/2 mutation API** — set-priority-on-push, app-driven
  window updates, dependency edits.  Wait for an adopter need.
- **gRPC implementation** — only the assessment doc this sprint.
  A real gRPC server is 1-3 sprints of work on top of these
  primitives; the assessment doc spells out the breakdown.
- **`scope['http2_priority']` removal** — happens in `v0.32.0`;
  retained this release to give adopters one cycle to migrate.
- **RFC 7540 weight/depends_on parity with gunicorn** — RFC 9113
  deprecated those; modern clients don't send them.

---

## [0.30.0] — 2026-06-04

**Sprint 31 close — zero-copy static-file serving for cleartext
HTTP/1.1.**  The streaming path for files > 4 MiB (the in-memory
cache threshold) previously went through chunked
`asyncio.to_thread`; microbench measured ~64 µs of per-chunk
event-loop dispatch overhead, which dominated the 16 ms total cost
on a 16 MiB transfer.  This release swaps that for a single
`loop.sendfile()` call when the transport supports it.

### Added

- **`http.response.pathsend` ASGI extension** — cleartext HTTP/1.1
  scopes now advertise the standard ASGI extension
  ([asgi.readthedocs.io](https://asgi.readthedocs.io/en/latest/extensions.html#path-send)).
  The application sends `http.response.start` (with Content-Length)
  followed by `{'type': 'http.response.pathsend', 'path': str}`;
  the sender takes responsibility for delivering the file bytes
  via `loop.sendfile`.  TLS connections do NOT advertise the
  extension — `loop.sendfile` raises `NotImplementedError` on SSL
  transports because the kernel can't see the plaintext.  (PR #44)
- **`AbstractWriter.sendfile(file, offset, count)`** — protocol-
  agnostic zero-copy primitive.  Default implementation raises
  `NotImplementedError`; `AsyncioWriter` drains buffered writes
  then calls `loop.sendfile` against the underlying transport.
  Propagates `NotImplementedError` so `HTTP1Sender` can fall back
  to a chunked read+write loop for TLS connections.

### Changed

- **`StaticFiles` middleware large-file path** — when scope
  advertises `http.response.pathsend` AND the response is not 206
  (Range requests carry no offset/count in the extension), the
  middleware emits `http.response.pathsend` instead of the chunked
  `http.response.body` stream.  Cached (small) files are
  unchanged: the bytes are already in Python, so the cache path
  stays the same.

### Performance

EC2 `c7i.2xlarge` cross-check on a 16 MiB file at c=64, 60 s
measurement window:

| | chunked (v0.29.0) | sendfile (this release) | Δ |
|---|---:|---:|---:|
| Effective throughput | 25 r/s | **569 r/s** | **23×** |
| Server-side p50 latency | 664 ms | **44 ms** | **15× lower** |
| Server-side p99 latency | 742 ms | 520 ms | 1.4× lower |

The chunked path was dispatch-bound at ~25 r/s (16 ms of pure
event-loop overhead per 16 MiB request); sendfile moves the
dispatch into kernel-space.  Effective throughput at this
concurrency is ~9 GB/s on loopback.

### Tests

- 14 new unit/architecture tests covering `AsyncioWriter.sendfile`
  (happy / TLS-NotImpl / abstract default), `HTTP1Sender`'s
  `pathsend` handler (header rendering, computed Content-Length,
  TLS chunked fallback, HEAD-only, defensive no-op),
  `StaticFiles` emitting `pathsend` correctly (extension present /
  absent / Range / small files), and the `HTTP1Actor` scope
  extension advertisement (cleartext / TLS).
- Total unit-test count: **1,234 passing** (was 1,206 at 0.29.0).
  Beartype-instrumented run: also clean.

### Notes for adopters

- **No API change.**  Existing apps see zero-copy file serving
  automatically for large files over cleartext HTTP/1.1.  TLS and
  HTTP/2 connections continue using the chunked streaming path.
- **HTTP/2 not affected.**  `h2` frames in user-space; there is
  no kernel path to interleave DATA frames around our HEADERS
  block.  HTTP/2 keeps the existing chunked streaming.
- **Range requests not affected.**  The ASGI pathsend extension
  carries no offset/count, so Range responses keep the chunked
  path that correctly honours `Content-Range`.
- **`KNOWN_LIMITATIONS.md`** — static-file note refreshed to
  reflect the three-way classification (cached / sendfile /
  chunked) while keeping the "front a real CDN for anything
  user-visible" framing.

### Out of scope / deferred

- **HTTP/2 zero-copy** — no kernel path exists.  Documented as
  intentional; revisit only if a real user need surfaces.
- **Off-loop cached (small-file) read on cache miss** — Sprint 31
  Task 1 diagnosis measured the cold-cache penalty at
  sub-millisecond p50 even for 1 MiB files.  Not worth the
  complexity.

---

## [0.29.0] — 2026-06-04

**Sprint 30 close — event-loop integrity under hostile / burst load
(Tier 1 only).**  Supersedes the `0.29.0a1` alpha pre-release: the
custom-protocol path (Tier 1.5, PRs #36 / #37 / #38) shipped in `a1`
behind `BB_USE_CUSTOM_PROTOCOL=False` was **reverted before the
final** after the EC2 cross-check showed it regressed client-side
latency by ~9 % (p50 189 → 207 ms) and throughput by ~8 % at c=4096
on `c7i.2xlarge`.  The code is parked on the
`Sprint30-tier1.5-custom-protocol` branch for future revisit; it is
not in this release in any form.  See *Notes for adopters* below
for migration guidance from `a1`.

### Added

- **`BB_WRITE_TIMEOUT`** (default 30 s, `0` disables) — bounds the
  time spent in `StreamWriter.drain()` waiting for the kernel send
  buffer to flush.  Defends against the **slow-read** shape of
  slowloris: a client that reads the response 1 byte/sec eventually
  fills the kernel send buffer and the server's drain blocks
  indefinitely without this timeout.  On timeout the transport is
  force-closed and the failure surfaces as a peer-side
  `ConnectionResetError` for the sender's existing error path.
  (PR #33)
- **`BB_MAX_CONNECTIONS` graceful 503 response** — when the cap is
  reached, new connections now receive HTTP/1.1 `503 Service
  Unavailable` with `Retry-After: 1` before close.  Previously the
  rejection path silently closed the socket, which load-balancers
  interpret as a server crash.  ALPN-h2 connections still close
  without writing (no SETTINGS exchange yet for clean GOAWAY).
  (PR #35)

### Changed

- **`BB_KEEP_ALIVE_TIMEOUT` default lowered from `60` to `5` seconds.**
  Aligns with the industry-standard short-idle default (uvicorn,
  granian, Caddy, Apache, Go `net/http` — all 5 s; gunicorn 2 s).
  60 s was a long-standing outlier that parked ghost / idle
  connection tasks in the loop's `readuntil` for far longer than
  necessary, inflating suspended-task count and amplifying drain
  time on burst-close.  **Behaviour change**: clients that pause
  >5 s between requests on a keep-alive connection will be closed
  and must reopen.  Set `BB_KEEP_ALIVE_TIMEOUT=60` to restore the
  prior default.  (PR #34)
- **`BB_MAX_CONNECTIONS` default raised from `0` (disabled) to
  `1024` per worker.**  Unbounded per-worker concurrency lets a
  single client, burst, or slowloris-class workload park thousands
  of suspended-readuntil tasks on the event loop, amplifying drain
  time on burst-close and inflating worst-case latency.  1024 is
  the typical ceiling for a single asyncio loop; multi-worker
  servers multiply the ceiling (`workers × max_connections`).
  **Behaviour change**: deployments accepting >1024 concurrent
  connections per worker now see HTTP/1.1 503 once the cap is
  reached.  Set `BB_MAX_CONNECTIONS=0` to restore unbounded.
  (PR #35)

### Fixed

- **`AsyncioWriter.close()` no longer awaits `wait_closed()`.**  The
  synchronous `self._sw.close()` already initiates the TCP shutdown
  and schedules the transport's `connection_lost` callback.  Awaiting
  `wait_closed()` afterwards serialised our connection-actor
  coroutine with full transport-close completion, adding 1-3
  event-loop turns per connection.  Under burst-keepalive workloads
  (HttpArena `static` at c=4096) those extra turns multiplied into
  multi-second drains that monopolised the loop and degraded
  throughput on back-to-back wrk runs.  (PR #32)
- **`ConnectionActor.run` drops redundant `asyncio.TaskGroup` wrap.**
  Both HTTP/1.1 (`HTTP1Actor`) and HTTP/2 (`HTTP2Actor`) run their
  protocol-specific logic without spawning sibling tasks at this
  level; HTTP/2 manages per-stream tasks via its own internal
  TaskGroup inside `HTTP2Actor.run()`.  The outer wrap added no
  supervision — just an extra `asyncio.Task` allocation per
  connection (observed 2× alive-task count vs connections in
  diagnostic dumps).  Replaced with a direct `await self._dispatch()`
  + plain `except Exception`.  (PR #32)

### Local benchmark (HttpArena static profile, c=4096, 3 back-to-back wrk runs)

| Configuration | Run 1 r/s | Run 2 r/s | Run 3 r/s | Degradation 1→3 |
|---|---:|---:|---:|---:|
| **Master before Sprint 30** (cap=0) | 4,630 | 4,362 | 4,048 | **12.6%** |
| **Sprint 30 default** (cap=1024, keep-alive 5 s) | 4,287 | 4,173 | 4,081 | **4.8%** |
| Same with c=1024 (under cap) | 4,704 | 5,159 | 5,056 | **none — runs 2/3 faster** |

The cliff at c=4096 is halved.  At c=1024 (the realistic adopter
concurrency) it is **eliminated** — back-to-back runs 2/3 are
faster than run 1.

### Tests

- 9 new unit tests across `test_asyncio_writer.py` (5 — write-timeout
  edge cases) and `test_max_connections_503.py` (4 — 503-response
  shape).

### Notes for adopters

- **Default keepalive 60 s → 5 s** matches every other major HTTP
  server.  If your clients legitimately need longer idle periods,
  set `BB_KEEP_ALIVE_TIMEOUT` explicitly.
- **Default max-connections 0 → 1024** caps per-worker concurrency.
  For higher load, set `workers=N` (multi-worker scales the
  ceiling).  `BB_MAX_CONNECTIONS=0` restores unbounded.
- **Migrating from `0.29.0a1`.**  The `a1` alpha shipped a
  `BB_USE_CUSTOM_PROTOCOL` env var (default off) wiring a custom
  `asyncio.Protocol` subclass.  That env var is removed in `0.29.0`;
  anyone who set it explicitly should unset it.  The code is parked
  on `Sprint30-tier1.5-custom-protocol` if you need to keep
  experimenting.

### Out of scope / deferred

- **Custom asyncio protocol (`_BlackBullProtocol` + `ProtocolBuffer`,
  former Tier 1.5).**  Parked on the `Sprint30-tier1.5-custom-protocol`
  branch.  EC2 cross-check (`c7i.2xlarge`, c=4096, 60 s window)
  measured **client-side p50 latency 189 → 207 ms (+9 %)** and
  **throughput 5,329 → 4,879 r/s (-8 %)** with the toggle on — a
  regression, not the local microbenchmark's ~5 % drain-time win.
  Removed from the release rather than shipped as opt-in code that
  the EC2 evidence says nobody should turn on.
- **Accept-pausing watermarks** (`BB_ACCEPT_PAUSE_HIGH/LOW_WATERMARK`):
  prototyped on the `tier2-accept-pausing` branch but deferred — the
  mechanism works (3× client-side latency reduction in measurement)
  but trades throughput in a way that surprises adopters who expect
  asyncio servers to be throughput-stable.  Branch retained for
  future revisit if a priority-scheduling primitive becomes available.

---

## [0.28.1] — 2026-06-02

**PATCH release — fixes a `Compression` + `StaticFiles` interaction
discovered while preparing the Sprint 29 HttpArena leaderboard
submission.**  Adds precompressed-variant serving so a static-file
workload with `Accept-Encoding: br/gzip/zstd` no longer engages
on-the-fly compression for every request.  Adds backpressure on
the Compression executor so the same workload degrades gracefully
when no precompressed sibling is available instead of collapsing
under burst load.

### Fixed
- **`Compression` middleware emitted duplicate `http.response.start`
  events when the upstream response was already encoded.**  Under
  HTTP/1.1 keep-alive this caused the sender to treat the second
  start as the end of the first response and close the connection
  — visible as a 1:1 success/read-error ratio in `wrk` and a
  ~500× throughput drop on `Accept-Encoding`-bearing static
  workloads.  Now: when `skip_compression` triggers, the start
  event is forwarded inline and the outer code path returns
  early.  Regression test added at
  [`tests/unit/test_compression_backpressure.py::test_skip_path_emits_exactly_one_start_event`](tests/unit/test_compression_backpressure.py).

### Added
- **Precompressed-variant serving in `StaticFiles`.**  When the
  client offers `Accept-Encoding: br | gzip | zstd` and a
  `<path>.br` / `.gz` / `.zst` sibling exists on disk,
  `StaticFiles` serves that file directly with the matching
  `Content-Encoding` header (and `Vary: Accept-Encoding`).  No
  on-the-fly compression on the static hot path.  Server
  preference order matches the `Compression` middleware
  (`br > zstd > gzip`).  Range requests bypass sibling lookup
  to avoid encoded-vs-Range size confusion.  Same pattern as
  nginx `gzip_static`, Caddy `file_server { precompressed }`,
  Apache `mod_negotiation`.
- **`Compression` executor-queue backpressure.**  New
  constructor argument `executor_max_inflight` and env var
  `BB_COMPRESSION_MAX_INFLIGHT` (default
  `max(os.cpu_count() * 2, 4)`).  When at the cap, additional
  eligible responses are served **uncompressed** rather than
  queued.  Prevents the unbounded executor backlog that caused
  the HttpArena `static` profile to collapse to 0 r/s on
  run 2/3 under c=1024.  `0` disables the cap (pre-0.28.1
  unbounded behaviour, if you want it back).
- **`Compression` skips already-encoded responses.**  When the
  upstream response has a `Content-Encoding` header set (e.g.
  by the new precompressed-variant `StaticFiles` path), the
  middleware forwards as-is rather than wrapping again.  Same
  shape Starlette / Caddy / nginx use.

### Changed
- **`StaticFiles` cache key extended** to record content-encoding
  alongside (path, mtime, size).  Different encodings of the
  same file now coexist in the cache as separate entries.

### Local benchmark — three back-to-back wrk passes, `c=1024`

| Workload | 0.28.0 | 0.28.1 |
|---|---:|---:|
| `Accept-Encoding: br` + precompressed sibling | 54 / 0 / 0 r/s | **54,664 / 34,380 / 34,920 r/s** |
| `Accept-Encoding: br` + no sibling (backpressure) | 54 / 0 / 0 r/s | **3,857 / 3,994 / 3,951 r/s** stable |
| No `Accept-Encoding` (no Compression engagement) | 24,572 / 27,386 / 31,358 r/s | unchanged |

### Tests
- **+10 unit tests.**  `tests/unit/test_static.py` gains 9 tests
  covering precompressed-variant negotiation (br/gzip/zstd
  preference, q=0 refusal, Range bypass, no-sibling fall-through,
  cache hits, separate-encoding cache entries).
  `tests/unit/test_compression_backpressure.py` is new with 6
  tests covering the executor-inflight counter (under cap →
  compresses; at cap → serves uncompressed; counter decrement on
  success and on exception; small-body path bypasses the cap;
  skip-path emits exactly one start event).  Total unit-test
  count: **812 passing**.

### Notes for adopters
- For `static` content under burst load, the right pattern is to
  ship precompressed `.br` / `.gz` / `.zst` siblings on disk
  (build-time step) and rely on the new variant-serving path.
  Compression on the fly via the `Compression` middleware is
  fine for small dynamic responses but doesn't scale to thousands
  of concurrent requests on large bodies — for that, terminate
  compression at a CDN or reverse proxy.

---

## [0.28.0] — 2026-05-31

**Sprint 28 — Early Alpha readiness.**  First release labelled
*Early Alpha*: the framework now has a soak-tested leak-free
posture and an EC2-reproducible benchmark cross-check against
FastAPI.  API may still break between MINOR versions per
ZeroVer; see [`KNOWN_LIMITATIONS.md`](KNOWN_LIMITATIONS.md) for
the explicit "what's not promised yet" list.

### Added
- **`KNOWN_LIMITATIONS.md`** — single consolidated doc covering
  RFC 8441 opt-in, HTTP/2 mux overhead, slowloris response shape,
  single-host benchmark caveats, RFC-defensible diffs from nginx
  in the differential corpus, no DB layer, no HTTP/3, no gRPC.
- **`bench/soak/`** — soak harness (1-hour wrk + tracemalloc +
  `/proc/<pid>/status` sampling, mixed-lane lua script).  Two
  1-hour soaks (single-worker + 4-worker) across 19.5 M requests
  confirmed RSS plateau, FD return-to-baseline, no growing
  tracemalloc slab.  Artefacts gitignored under
  `bench/results/soak/`.
- **`bench/aws/httparena_compare.sh`** — EC2 c7i.xlarge HttpArena
  comparison harness; provisions Docker + liburing 2.9 + gcannon
  from source + wrk + h2load + h2spec + Autobahn runner; vendors
  `bench/httparena/` as the framework; trap-EXIT teardown.
- **CLI `--version` flag** — prints `blackbull <version>` and
  exits 0.  Reads from `importlib.metadata.version('blackbull')`
  so it always agrees with the installed wheel.
- **HttpArena `/ws` echo route** + `/baseline2` (H/2 path) in
  `bench/httparena/app.py`.  Closes the WebSocket profile and the
  H/2 baseline; previously only H/1.1 was implemented.

### Changed
- **`StaticFiles` middleware now caches small files in memory.**
  mtime+size-keyed LRU cache (default ≤ 4 MiB per file, 256
  entries); cache hits are two `send()` calls with no thread-pool
  dispatch.  Replaces the per-request `asyncio.to_thread(...)`
  open/seek/read/close chain that exhausted the default
  ThreadPoolExecutor (8 workers) at HttpArena's c=1024–6800 load
  — first run plateaued at 71-79 r/s and subsequent runs collapsed
  to 0 r/s as the dispatch queue saturated.  Local back-to-back
  c=1024 measurements after the fix: 17,885 / 18,345 / 18,149 r/s
  with worker RSS flat at ~33 MB (was 275 → 768 MiB).  Files
  above the threshold keep the streaming path so per-request peak
  memory stays at one chunk regardless of body size.
- **Default error handler is environment-aware.**
  `_default_error_handler` (registered on every HTTP error status
  and `Exception`) now reads `BLACKBULL_ENV`:
  - `development` — surfaces the full Python traceback inline so
    users debugging locally see the failure point in the response
    body.  `Accept: text/html` returns a styled HTML page;
    everything else returns text/plain.
  - `production` — terse: status code + phrase only.  Exception
    class and message no longer leak to the network.
  Sets `Content-Type` and `Content-Length` explicitly on all
  error responses (previously omitted).
- **`bench/httparena/launcher.py` now spawns three workers** —
  HTTP cleartext on :8080, HTTPS+H1 on :8081, HTTPS+H2 on :8443.
  Matches HttpArena's `scripts/validate.sh` port layout
  (`PORT=8080`, `H1TLS_PORT=8081`, `H2PORT=8443`).  Closes the 5
  `json-tls` validation failures the previous two-process layout
  caused (nothing was bound on :8081).  Shape mirrors the
  HttpArena `frameworks/fastapi/launcher.py` reference — no
  port-readiness gating, no TLS-handshake synchronisation.

### Fixed
- **Static-file middleware run-2/run-3 collapse to 0 r/s** under
  HttpArena's high-concurrency wrk passes.  Root cause was
  asyncio thread-pool exhaustion (see "Changed" above), not a
  memory leak — RSS climbed because thousands of in-flight scope
  dicts and file descriptors accumulated while waiting on the
  shared executor.
- **`bench/peers/asgi_app.py`** + **`bench/app.py`** — replaced
  `status: 200 / 404` integer literals with `HTTPStatus.OK` /
  `HTTPStatus.NOT_FOUND`.  Cosmetic; no runtime behaviour
  change.

### EC2 cross-check (Sprint 28 Task 3 + Task 4 carry-forward)
- HttpArena validate on `c7i.xlarge`: **49/49 pass** (previous
  pass-count 44/5 fail before the launcher fix).  Includes
  baseline H/1.1, pipelined, limited-conn, json, json-comp,
  json-tls, upload, static, baseline-h2, static-h2, echo-ws.
- HttpArena benchmark numbers captured for BlackBull and FastAPI
  across the validated profiles.  Detailed results in the
  Sprint 28 internal log; consolidated summary in
  `bench/CHARACTERIZATION.md ## Sprint history`.
- Static throughput on EC2 remained the dominant gap pre-cache;
  the in-memory cache lands as a 0.28.0 source change but the
  EC2 re-measure of static under the new code is a Sprint 29
  open carry-forward (no new EC2 spend in Sprint 28).

### Methodology
- **Early Alpha classification confirmed** after Task 2 (soak) and
  Task 4 (release-shape + EC2 cross-check) closed.  Both blocking
  risks noted at the start of the sprint — no ≥1-hour soak, no
  externally reproducible benchmark — are now closed.

---

## [0.27.1] — 2026-05-31

Packaging cleanup ahead of first PyPI publish.  Between-sprints PATCH
work per the ZeroVer rule — no `blackbull/` source changes outside
the new PEP 561 marker.

### Fixed
- **`beartype` promoted from `[validation]` optional extra to a hard
  dependency.**  `Router.validate()` imports `beartype.door` at every
  `app.run()` / `app.serve()` boot; with the prior packaging, a cold
  `pip install blackbull` followed by running any app crashed with
  `ModuleNotFoundError: No module named 'beartype'`.  The
  `[validation]` extra is retained as an empty no-op for
  backwards-compatible install commands.

### Packaging
- **Wheel slimmed from 243 → 67 files** (~590 KiB → ~209 KiB).  `[tool.setuptools.packages.find]`
  now `include = ["blackbull*"]` with explicit exclusions for `bench`,
  `tests`, `examples`, `docs`, `site`, `templates`.  Previously the
  wheel shipped benchmark snapshot directories and the full test
  suite, which `pip install blackbull` users had no reason to
  download.
- **PEP 561 typed distribution** — added `blackbull/py.typed` marker
  + `[tool.setuptools.package-data]` entry so downstream type-checkers
  (mypy, pyright) trust the inline annotations.
- **PyPI classifiers** — Development Status, Framework :: AsyncIO,
  License :: OSI Approved :: Apache Software License, Python 3.11 /
  3.12 / 3.13, HTTP Servers topic, Typing :: Typed.
- **Keywords** — `asgi asyncio http http2 websocket web framework
  server`.
- **`project.urls`** expanded — separate Documentation, Changelog,
  Issues entries (previously just two redundant pointers at the
  GitHub repo).

### Documentation
- **README rewritten** as a PyPI sell sheet — what BlackBull is, why
  someone would pick it, working install + hello-world + TLS +
  WebSocket + middleware snippets.  Internal P1/P2/P3/P4 roadmap
  removed (lived as project-facing todo, not user-facing reference).
- **Fixed a real bug in the prior README's hello-world.** Was
  `asyncio.run(app.run(port=8000))` — `app.run()` is itself a
  blocking sync entry point; wrapping it in `asyncio.run` raised on
  the first execution.  Now `app.run(port=8000)`, matching
  `examples/helloworld-simple.py`.

---

## [0.27.0] — 2026-05-30

Sprint 27 — methodology pin (cascade-multiplier rule) + HttpArena
local-only integration prep.  No optimisation Phase 2: the new
profile data placed the original Sprint 28 candidate (SSL/TLS Python
glue) under deployment-posture conditions and the `httptools` target
sub-cascade, both reclassified accordingly.

### Added
- **Cascade-multiplier rule** pinned in
  [`bench/CHARACTERIZATION.md ## Methodology`](bench/CHARACTERIZATION.md)
  as a sprint-gating mechanism.  Profile-share table maps per-call
  microbench delta → expected B-lane throughput delta.  Replaces the
  ad-hoc "≈2-3×" rule of thumb with three explicit bands
  (≥ 30 %, 10-30 %, < 10 %).
- **`bench/aws/profile_lanes.sh`** — wrk-driven per-lane py-spy
  capture on EC2 split topology.  `BB_TLS=0` opt-in for cleartext
  profiling (mirrors nginx-fronted production posture).  Lessons
  baked in: `(...&)` subshell form for nohup re-parenting, cold-cache
  warmup discard before the measured wrk pass.
- **`bench/app.py --no-tls`** flag — listens cleartext for the
  TLS-off profiling lane.
- **`bench/httparena/`** scaffold (Task 4) — HttpArena
  `frameworks/blackbull/`-shaped Docker container with two-process
  launcher (cleartext :8080, TLS :8081), `meta.json` declaring 13
  profiles, integration with the existing `Compression` +
  `StaticFiles` middleware.  Verified per-process 1.5-7.2× faster
  than FastAPI/uvicorn on H/1.1 + static paths; HTTP/2 served (vs
  uvicorn's zero h2c).  Not enabled for leaderboard submission.

### Fixed
- **HTTP/2 query-string scope (`scope['path']`)** — the H/2 `:path`
  pseudo-header was copied verbatim into `scope['path']` including
  any `?query`, and `scope['query_string']` was set to `str` where
  ASGI requires `bytes`.  Router pattern `/json/{count:int}` then
  failed to match `/json/3?m=2.0` and returned 404.  Centralised the
  split into a `_split_h2_path()` helper called from `parse_headers`
  (http + RFC 8441 websocket branches), `Stream.update_scope`, and
  the push-promise scope builder in `http2_actor.py`.  HTTP/1.1 was
  unaffected (its parser already partitions path + query at
  request-line decode time).
- **`Compression` middleware Content-Length** — when the upstream
  handler sets `Content-Length` on the uncompressed body (e.g.
  `StaticFiles`), the middleware previously left the original value
  in place after compressing.  Broke HTTP/1.1 keepalive framing and
  triggered protocol errors on strict HTTP/2 clients.  Now strips
  upstream `Content-Length` and writes the post-compression length.

### Methodology
- Sprint 28 anchor flipped from `httptools` to **deployment-posture
  dependent**.  Re-profile (`0c80080`, B1/B3 EC2 c7i.xlarge, py-spy
  200 Hz) showed:
  - With BlackBull terminating TLS: SSL/TLS Python glue ~15 %
    self-time → cascade-rule prediction +7-8 % B1 if coalesced.
  - With TLS terminated upstream (`--no-tls`, nginx-fronted
    posture): SSL/TLS slice **disappears** — no single Python slice
    exceeds ~5 % self-time post-Sprint-26.
  - `_parse` (the `httptools` target) was ~2 % self-time in both
    topologies — sub-cascade per the new rule.  Decommissioned as a
    Sprint 28 candidate; pure-Python H1 parser kept as identity.
- HTTP/3 / QUIC confirmed as **intentionally out of scope** —
  removed from Sprint 28 candidates.

---

## [0.26.0] — 2026-05-30

Sprint 26 — deadline subsystem rework.

### Changed
- **Per-arm `loop.call_later` replaced by a per-process tick scanner.**
  Singleton `TimerHandle` re-arms itself every `BB_DEADLINE_TICK_MS`
  (default 300 ms); `ConnectionDeadline.arm` / `disarm` become
  attribute writes + set ops.  Per-call cost: 1.69 µs → 350 ns
  (−79.3 % on Phase A, −83.7 % cumulative vs the Sprint 23
  `@contextmanager` baseline).  uvloop sanity: 332 ns/call.
- **`ConnectionDeadline.guard()` is now class-based.**  Replaced the
  `@contextmanager` decorator with `__enter__` / `__exit__` on the
  deadline instance directly; saves the per-call generator-frame
  allocation.  All five exit-path semantic cases (normal, non-CE
  raise, foreign CE, deadline CE, same-tick race) preserved
  byte-equivalent.
- EC2 c7i.xlarge sequential cross-pair (N=2) vs Sprint 25 close:
  B1 +17.4 % / +14.2 % (BB_UVLOOP=0/1), B2 +17.1 % / +19.1 %,
  B3 +18.3 % / +14.5 %.  7/7 B-lanes ✓.

### Added
- `bench/aws/full_ab_sequential.sh` — sequential N-pair wrapper for
  AWS accounts under the 32-vCPU default limit (parallel M=3 with
  c7i.xlarge + c7i.2xlarge needs 36 vCPU).  Methodologically
  equal-or-better than parallel M=N: sequential pairs sample
  different time windows, so neighbour drift becomes part of the
  cross-pair signal.
- `BASE_REF=<commit>` env mode in `bench/aws/full_ab.sh` — compare
  HEAD bytes vs an arbitrary historical commit for cumulative
  cross-sprint re-measures.

### Fixed
- `bench/aws/install.sh` — apt source swapped from
  `us-east-1.ec2.archive.ubuntu.com` to `archive.ubuntu.com` after
  the regional EC2 mirror was observed serving a 14-hour-stale
  `noble-updates/universe/binary-amd64/Packages.xz`.  Tolerate-on-
  failure retained as belt-and-braces.
- `full_ab.sh` — provisioning warnings now log exit code + tail of
  failing log into `orchestrator.log`; `up.sh` retries once after
  partial-state teardown.
- `_pair_bench.sh` — uvicorn bookend now pins `--loop uvloop`
  explicitly (was silently auto-detected; pair.log label
  `kind=uvicorn, uvloop=0` was misleading).

---

## [0.25.0] — 2026-05-29

Sprint 25 — HTTP/1 parser hot-path + cross-pair EC2 harness.

### Changed
- **`_parse` URL splitter** — `urllib.parse.urlparse` + `re.sub` →
  three `bytes.partition` calls + slice.  Per-call −91.6 %.
- **Header-loop regex validators** — per-byte `any(...)` validation
  scans → compiled-regex `search()` (`_FIELD_NAME_INVALID_RE`,
  `_FIELD_VALUE_INVALID_RE`).  Per-call −77.1 %.
- EC2 c7i.xlarge `TOPO=split` M=3 cross-pair: B1 +13.6 % / +15.3 %
  (BB_UVLOOP=0/1), 6/7 lanes ✓ (B7 △).  Measured 2-3× the
  microbench prediction at c=256 — the cascade-effect calibration
  is the load-bearing methodology lesson (later refined in 0.26.0
  + 0.27.0).

### Added
- `bench/aws/full_ab.sh` + `bench/aws/_pair_bench.sh` +
  `bench/aws/_aggregate_ab.py` — multi-pair cross-instance harness
  with uvicorn bookends (host-drift detection), identity check
  (`_assert_server_kind`), mpstat / vmstat capture, trap-cleanup.
- Per-sprint findings + raw numbers split out to
  `bench/sprint-logs/` (gitignored — protects external-server
  numbers from being cited as competitive benchmarks).
  `bench/CHARACTERIZATION.md` trimmed to current-state only.

---

## [0.24.0] — 2026-05-28

Sprint 24 — follow-ups + Lane E.

### Changed
- Methodology hardening: `RUNS_WRK=3` with MAD noise column + 🌫
  flag; `DURATION` default 30 → 60 s; `WARMUP=15` to settle
  allocator + TCP autotune + TLS session cache.
- Single-worker baseline-of-record refreshed against the new
  warmup + duration defaults.
- HPACK fastpath extended to request-side pseudo-headers
  (PUSH_PROMISE path).

### Added
- Lane E — connection churn (`Connection: close` per request),
  opt-in via `LANES="E-wrk"`.  Exposes accept-loop + TLS-handshake
  cost that the keep-alive-dominated Lane B hides.
- `/etc/sysctl.d/99-blackbull-bench.conf` installed by
  `bench/aws/install.sh` (`tcp_tw_reuse=1` + widened
  `ip_local_port_range`) to lift Lane E off the default
  accept-queue / port-range floor.
- Top-of-file cross-topology warning box in
  `bench/CHARACTERIZATION.md`; per-sprint status badges;

---

## [0.23.0] — 2026-05-25

Sprint 23 — `asyncio.timeouts.*` cost removed from the per-request
hot path.

### Changed
- Replaced per-phase `async with asyncio.timeout(d):` context
  managers in `connection_actor.py` (sniff + preface),
  `http1_actor.py` (header + keep-alive idle), and `recipient.py`
  (per-chunk body) with a single rescheduled `loop.call_later()`
  `TimerHandle` per connection.
- AWS single-worker on c7i.xlarge `TOPO=split`: B1 plaintext
  **+6.5 %** (14 822 → 15 793 req/s).  py-spy at 200 Hz showed
  `asyncio.timeouts.*` at **0 samples** (was 9.6 % inclusive in
  Sprint 21 Phase B).

### Added
- `blackbull/server/deadline.py::ConnectionDeadline` with `guard()`
  contextmanager (Phase A surface; later replaced by class-based
  `__enter__`/`__exit__` in 0.26.0).

---

## [0.22.0] — 2026-05-23

Sprint 22 — framework / server separation.

### Changed
- `Headers` + `HeaderList` moved from `blackbull/server/` to
  top-level `blackbull/headers.py`.
- `ASGIEvent` folded into `blackbull/asgi.py`.
- `import blackbull` no longer transitively loads the server
  stack — use `from blackbull.server import ASGIServer` when
  embedding the server.

### Removed
- `BlackBull.{serve, create_server, has_server, wait_for_port,
  stop, port}` — embedded-server lifecycle is no longer part of
  the public API.  Callers wanting async lifecycle use
  `ASGIServer` from `blackbull.server` directly.
- `BlackBull.run()` is now synchronous (was async).

---

## Pre-0.22 — Phase 6 actor-model refactor

### Added

- **Level B event API** — `@app.on(event)` for fire-and-forget observation and
  `@app.intercept(event)` for synchronous interception with `call_next` chaining.
  Nine built-in events: `app_startup`, `app_shutdown`, `request_received`,
  `before_handler`, `after_handler`, `request_completed`, `request_disconnected`,
  `error`, `websocket_connected`, `websocket_message`, `websocket_disconnected`.
- **`asgi.py`** — `ResponseStart` / `ResponseBody` dict subclasses and
  `parse_response_event()` for typed ASGI send-event dispatch.
- **Observer task lifecycle** — in-flight `@app.on` tasks are tracked and drained
  at shutdown with a configurable timeout (`observer_shutdown_timeout`).
- WebSocket connection identity: `scope['_connection_id']` set to `uuid4` on connect.

### Changed

- **Middleware re-implemented as intercept sugar.** `app.use(mw)`,
  `middlewares=[...]` on routes/groups, `@app.on_startup`, `@app.on_shutdown`,
  and `@app.on_error(status)` all lower to `@app.intercept('...')` registrations.
  There is now a single runtime path; the old middleware chain is removed.
- **`StreamingAwareMiddleware` ABC removed.** Streaming is handled transparently
  by `HTTP1Sender`; middleware authors no longer need to subclass it.
- All built-in middleware (`compress`, `websocket`, `StaticFiles`) re-implemented
  as intercept hook registrations.
- Examples (`SimpleTaskManager`, `ChatServer`, `LoggingExample`, `PriorityExample`)
  rewritten to use the event API.

### Added (Phase 6 — Actor model)

- `blackbull/actor.py` — `Message` dataclass base and `Actor` base class with
  queue-based inbox (`asyncio.Queue`).
- `blackbull/event_aggregator.py` — `EventAggregator` bridges Level A Actor messages
  to Level B `EventDispatcher` calls. Framework-internal; not exported from
  `blackbull/__init__.py`.
- `blackbull/server/http1_actor.py` — `HTTP1Actor` (keep-alive loop per connection)
  and `RequestActor` (single request lifetime). Transport metadata (`peername`,
  `sockname`, `ssl`) injected as explicit keyword args — no `asyncio.StreamWriter`
  dependency.
- `blackbull/server/http2_actor.py` — `HTTP2Actor` (connection state machine) and
  `StreamActor` (per-stream ASGI dispatch). Runs stream tasks in `asyncio.TaskGroup`
  so all streams complete before the connection closes.
- `blackbull/server/websocket_actor.py` — `WebSocketActor` drives the WebSocket
  lifecycle after the HTTP upgrade.
- `blackbull/server/connection_actor.py` — `ConnectionActor` accepts TCP connections
  and dispatches to the correct protocol actor.
- `blackbull/client/` — async client package: `HTTP1Client`, `HTTP2Client`,
  `WebSocketClient`, and `Client` (ALPN-dispatching front door).

### Changed (Phase 6)

- `HTTP11Handler`, `HTTP2Handler`, `WebSocketHandler` deleted; Actors are the sole
  runtime path.
- `AbstractReader` / `AbstractWriter` used throughout — no implicit
  `asyncio.StreamWriter` dependency anywhere.
- Test suite reorganised into `tests/unit/` (parsing, framing, data structures),
  `tests/architecture/` (actor + event contracts), and `tests/conformance/http1/`
  and `tests/conformance/http2/` (full round-trip tests against a real `ASGIServer`).

### Fixed

- `parse_cookies()` now collects all `Cookie` headers. Firefox sends separate
  headers per cookie over HTTP/2; the previous code discarded all but the first.