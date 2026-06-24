# Changelog

All notable changes to BlackBull are recorded here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## Versioning

BlackBull uses [ZeroVer](https://0ver.org/) prior to a 1.0 commitment:

- `0.MINOR.PATCH`
- `MINOR` advances at each sprint close (one minor per closed sprint).
  The minor number does **not** equal the sprint number — patch releases
  and combined-sprint releases have introduced an offset (Sprint 49
  closed as `v0.43.0`).
- **Exception (2026-06-25)**: Sprints 50 through 54 are not independently
  released — they ship together as `v0.44.0` (the next minor after `v0.43.0`),
  the MQTT-broker debut plus its actor-model rebuild and the protocol-agnostic
  connection dispatcher.  Normal per-sprint versioning resumes at the next
  sprint close as `v0.45.0`.
- `PATCH` covers bug fixes / harness work between sprints.
- No `1.0.0` until the framework's identity (pure-Python H1 parser,
  BlackBull-internal `ASGIServer`, per-process tick scanner deadline
  subsystem) and public API have stabilised across several sprints.

The runtime version is exposed as `blackbull.__version__` via
`importlib.metadata.version("blackbull")` — single source of truth is
`pyproject.toml`.  Re-run `pip install -e .` after a local version bump
so the editable install's metadata catches up.

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