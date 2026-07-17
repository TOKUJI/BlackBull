# BlackBull — Known limitations

This document is the single user-facing inventory of behaviours and
gaps that may surprise an app author adopting BlackBull at its
current **Early Alpha** maturity level.  The companion
[`docs/about/conformance.md`](docs/about/conformance.md) records the
protocol-level test coverage behind the standards-compliance claims;
this file is the narrative "things to know before you build on top
of it" list.

> **Read this alongside the strengths.** A gap list read on its own is
> unbalanced.  For the architectural bets behind these trade-offs, see
> [`docs/about/architecture.md`](docs/about/architecture.md); to decide
> whether BlackBull fits your use case, see
> [`docs/getting-started/why-blackbull.md`](docs/getting-started/why-blackbull.md).

Every item here is something the project knows about and has a
position on.  Behaviours that aren't listed below — anything that
would surprise us as well — belong on the GitHub issue tracker.

---

## Protocol-level

### RFC 8441 (WebSocket-over-HTTP/2) is opt-in

WebSocket bootstrapping via HTTP/2 Extended CONNECT (RFC 8441) is
implemented but disabled by default.  Set
`BB_H2_ENABLE_WEBSOCKET=1` (or `h2_enable_websocket=True` in the
Settings) to advertise `SETTINGS_ENABLE_CONNECT_PROTOCOL=1` and
accept Extended CONNECT bootstraps.

**Why**: when TLS is active the browser may pick HTTP/2 via ALPN
even for `ws://` upgrades; with RFC 8441 off, the upgrade is
blocked and the browser falls back to HTTP/1.1.  Test coverage
isn't yet broad enough to promote this to default-on.

**Attack surface when enabled**: with
`BB_H2_ENABLE_WEBSOCKET=1` and BlackBull terminating HTTP/2
directly (no nginx / L7 proxy in front), the server is exposed
to stream-exhaustion attacks — an attacker can open up to
`BB_H2_MAX_CONCURRENT_STREAMS` (default 100) Extended CONNECT
streams per connection, multiplied by `BB_MAX_CONNECTIONS`
(default `0` = unbounded), holding all of them idle.  Mitigate
by setting `BB_MAX_CONNECTIONS` to a finite value and relying on
`BB_H2_WS_MAX_STREAMS_PER_CONNECTION` (default `5`).  The
recommended production shape — nginx terminating TLS/HTTP/2
with BlackBull on HTTP/1.1 behind it — eliminates this surface
entirely because nginx does not forward RFC 8441 Extended
CONNECT to the backend.

### HTTP/2 multiplex overhead vs HTTP/1.1

The HTTP/2 implementation is conformant (passes `h2spec`,
RFC 9113) but each stream carries actor-machinery overhead — a
per-stream queue, sender state, and priority-tree bookkeeping —
that the HTTP/1.1 path doesn't pay.  Single-connection,
high-multiplex workloads will spend more per request on HTTP/2
than on HTTP/1.1; the difference grows with mux depth.

If a workload needs maximum throughput on a single HTTP/2
connection at high mux, a fronting HTTP/2 terminator (nginx,
Envoy) is the usual production shape; BlackBull behind that
terminator on HTTP/1.1 is the matched-cost path.

### h2c prior-knowledge shares the HTTP/1.1 port

BlackBull's plaintext listener auto-detects the HTTP/2
connection preface and switches to h2c — there is no separate
"h2c-only" port that refuses HTTP/1.1.  This is RFC-permissible
(RFC 9113 allows h2c via prior knowledge on any port) and means
the same port serves both protocols at the framework's
discretion.

### Slowloris response is correct but not quantitatively characterised

Three timeouts (`BB_HEADER_TIMEOUT`, `BB_BODY_TIMEOUT`,
`BB_KEEP_ALIVE_TIMEOUT`) defend against partial-data attacks;
tests verify a 408 is returned.  What's *not* characterised is
the exact "with N slow connections, first new connection
accepted within M ms" curve — only the qualitative claim "RFC
9110 §15.5.9 compliant 408 plus the three timeouts work".

---

## RFC-defensible differential-corpus divergences

The differential fuzz harness compares BlackBull's response to
the same request against nginx.  Two captured divergences are
**RFC-defensible**, kept in the corpus deliberately, and not
treated as bugs:

| Wire request | nginx | BlackBull | Why we're right |
|---|---|---|---|
| `GET&nbsp;&nbsp;http://localhost/x HTTP/1.0` (double-SP between method and target) | 200 | 400 | RFC 9112 §3 — request-line tokens are separated by exactly one SP.  nginx is lenient; we reject. |
| `GET&nbsp;&nbsp;http://localhost/x HTTP/9.9` (double-SP **and** unknown version) | 505 | 400 | Validation-order choice: the request-line SP grammar (RFC 9112 §3) is checked before the HTTP version, so the malformed line is 400 first.  nginx reports the version problem (505) instead.  A *well-formed* request with an unsupported version does get 505 from BlackBull (RFC 9110 §15.6.6). |

Both are documented in
[`tests/conformance/http1/fuzz/user-corpus/diff_README.md`](tests/conformance/http1/fuzz/user-corpus/).
We're not chasing nginx parity here unless a real user need
appears.

---

## Deployment notes

### Raw protocol handlers: single-owner, HTTP still scales, cleartext by default

When a non-ASGI protocol handler is registered via `app.raw_handler()` or
`app.register_protocol_handler()`, BlackBull applies these constraints:

**The protocol has a single owner, but HTTP scales (Sprint 55).** A stateful
broker must have one owner (see the MQTT section below), so the master binds
the protocol port once and hands it to **worker 0** only; the broker lives
there.  HTTP is stateless, so `app.run(port=8000, workers=4)` alongside, say,
`MQTTExtension(port=1883)` now runs HTTP on **all** workers while the broker
runs on worker 0.  If worker 0 crashes the master respawns it and it
re-inherits the still-open listening socket, so the broker resumes on the same
port (clients reconnect; in-memory broker state is not preserved — see below).

The one exception is **auto-reload** (`--reload`): it hands listening sockets
across an `exec` via fd inheritance, and that handoff does not yet include the
protocol listeners, so `reload=True` with a port-bound protocol still forces
`workers=1`.  Run without reload to scale HTTP alongside the broker.

`BB_SOCKET_REUSEPORT=1` makes each HTTP worker bind its own kernel accept
queue (best load distribution); without it the workers share the master's
listening socket.  The protocol port is always bound without `SO_REUSEPORT`
— a single owner is the point.

**Cleartext by default; TLS is opt-in per binding (Sprint 75)** — a raw
protocol socket is bound without TLS unless the binding is registered with
`tls=True` (`app.raw_handler(name, port=…, tls=True)` /
`MQTTExtension(port=8883, tls=True)`), in which case it is served through
the same TLS machinery as the HTTPS listener (certificate from
`certfile`/`keyfile` or `ssl_context`; startup fails fast when `tls=True`
has no certificate to use).  A TLS-terminating proxy in front of the raw
socket remains a fine alternative.

### MQTT broker: best-effort taps, in-memory single-process state

The MQTT 5 broker (`blackbull.mqtt`, opt-in via `blackbull[mqtt]`) rides
the raw-protocol bridge above, so it inherits its constraints —
**single-owner** (the broker runs on worker 0; HTTP still scales across all
workers) and **cleartext unless registered with `tls=True`**
(`MQTTExtension(port=8883, tls=True)` for `mqtts://`).  Beyond those:

**Why a single broker owner is a protocol requirement, not an
implementation limitation.**  The MQTT 5.0 specification (OASIS Committee Specification
02, March 2019) defines semantics that depend on broker-side state visible
to every connection: publish-subscribe matching (§3.3), retained messages
(§3.3.2.3), session state across Clean Start = 0 reconnects (§3.1.2.11),
Will messages (§3.1.2.5), and QoS 1/2 delivery tracking (§4.3).  All five
break if that state is split across worker processes with no shared store.
This is consistent with industry practice: the Eclipse Mosquitto
reference implementation ([mosquitto.org](https://mosquitto.org/)) is
single-threaded by architecture for the same reason, and EMQX clusters via
Erlang distributed message passing rather than local worker splitting.
(Confirmed 2026-06-25 against the OASIS MQTT 5.0 specification and
Mosquitto's project documentation.)

**`on_message` taps are best-effort observability, not a delivery path.**
In the default `tap_mode='actor'`, each PUBLISH is *offered* to a
bounded-inbox `TapActor` that **drops the newest message on overflow**
(with a running dropped-count logged) so a slow tap can never
back-pressure routing.  Use a real MQTT subscription when you need
guaranteed delivery; treat a tap as a probe.

**Broker state is in-memory and per-process.**  Subscriptions, sessions,
and retained messages live in the one serving process (worker 0) — not
shared across workers (hence the single-owner model) and not persisted
across a restart.  A restart, or a worker-0 respawn after a crash, clears
all session and retained state.  Sessions are also kept for the process
lifetime rather than expired on a timer.

**New messages are not queued for offline sessions.**  A disconnected
client with a live session (`Session Expiry Interval > 0`) gets §4.4
replay of its *unacknowledged in-flight* QoS 1/2 messages on reconnect,
but messages **newly published while it was offline are not queued** and
will never be delivered to it.  The same applies to a shared-subscription
group with no connected member (see
[`docs/guide/mqtt.md`](docs/guide/mqtt.md)).  If you need store-and-forward
for offline consumers, use a broker with persistent offline queues.

### Multi-worker scaling tops out at physical core count

Worker throughput scales roughly to the **physical core count**,
not the logical (SMT-thread) count.  Multiplying workers past
the physical-core ceiling does not multiply throughput.  Plan
capacity against `nproc / 2` on typical SMT-enabled hardware.

---

## What BlackBull doesn't do

### Static-file serving is not a production CDN

[`blackbull/middleware/static.py`](blackbull/middleware/static.py)
serves files three ways at runtime:

- Read from disk on every request (the default since 0.33) — each
  hit runs `path.stat()` + `open().read()`.  Correct under file
  edits with no staleness window, but pays the per-request syscall
  cost.
- Zero-copy via `loop.sendfile` (cleartext HTTP/1.1, > 4 MiB,
  no Range) — single kernel-side transfer, no per-chunk
  event-loop dispatch.  Opted in via the `http.response.pathsend`
  ASGI extension; cleartext HTTP/1.1 advertises it.
- Chunked through `asyncio.to_thread` (TLS, HTTP/2, Range
  requests) — correct, but every chunk pays thread-pool dispatch
  overhead.  Use the fronting nginx path below if this is on the
  critical path for you.

An optional in-memory cache (≤ 4 MiB) is available with
`app.static(prefix, root, cache=True)`: first hit reads sync,
subsequent hits serve from a per-process LRU, and the entry is
stat-invalidated per request so edits on disk show up on the
next request with no staleness window.  Default is `cache=False`;
standalone deployments serving static traffic directly should opt
in to keep prior performance.

`StaticFiles` itself emits no `ETag`; pair it with the `Cache`
middleware for ETag / `If-None-Match` revalidation (`blackbull serve`
does this for you).  What's still missing across all paths:
byte-range-multipart and CDN edge-cache invalidation glue.  For
anything user-visible, front a real static-file server (nginx,
S3 + CloudFront).

### No internal database layer

BlackBull is a protocol-layer framework.  There is no built-in
ORM, no connection pool, no migration tool.  Apps should bring
their own (`asyncpg`, `databases`, `tortoise-orm`, etc.).

### `Depends` is deliberately minimal; query params are scalars only

The v0.56.0 dependency-injection surface is fenced by design
(see [`docs/guide/dependency-injection.md`](docs/guide/dependency-injection.md)):
providers take **no parameters**, and a provider that itself declares
`Depends` (nesting, common in FastAPI code) is a registration-time
`TypeError` — compose inside the provider body instead.  No interface
binding, no interception (the event API covers cross-cutting concerns).
Query params resolve scalars only (`str`/`int`/`float`/`bool`,
optionally `| None`); repeated-key aggregation (`?tag=a&tag=b` →
`list[str]`) and query-model objects are not supported — parse
`scope['query_string']` yourself for those.  Fences are lifted on
demonstrated demand, not speculatively.

### Optional `[speed-h1]` C parser stub not implemented

`pyproject.toml` lists no `[speed-h1]` extra today.  The
pure-Python HTTP/1 parser is fast enough that swapping in a C
parser (e.g. `httptools`) isn't on the critical path; a future
opt-in knob is sketched in the roadmap but not built.

### No HTTP/3 / QUIC

Out of scope.  Revisit if a real user need appears.

### gRPC: core handlers exchange raw bytes; reflection is v1alpha-only

All four gRPC RPC shapes — unary, server-streaming, client-streaming, and
bidirectional — ship in `blackbull.grpc` (`app.enable_grpc`), with `gzip`
message compression (`grpc-accept-encoding: identity,gzip`).
`application/grpc` requests multiplex onto the same HTTP/2 port as REST and
WebSocket, with `grpc-status` reported in trailers (a trailing HEADERS
frame). Core handlers exchange raw message bytes by design; object-typed
servicers, server reflection, `grpc.health.v1`, and rich error details ship
in the optional [`blackbull-protobuf`](https://github.com/TOKUJI/blackbull-protobuf)
package (`pip install 'blackbull[protobuf]'`). Remaining gaps: reflection
serves `grpc.reflection.v1alpha` only (`v1` is a planned fast-follow;
grpcurl and most clients fall back to v1alpha automatically), and BlackBull
is server-side only — use `grpcio` for a client. See
[`docs/guide/grpc.md`](https://github.com/TOKUJI/BlackBull/blob/master/docs/guide/grpc.md).

### CLI `--bind` host is advisory

The `blackbull` console script covers the ASGI-runner shape
(`blackbull app:app --bind ...`), the zero-code static server
(`blackbull serve ./dir`), `--version`, `--config`, `--reload`, and
focused errors on a bad `module:attr`.  The one notable gap: the
host portion of `--bind host:port` (and the absence of a `host`
field on `AppConfig`) is advisory — the socket layer binds dual-stack
on **all** interfaces, so `--bind 127.0.0.1:8000` still listens on
every interface.  Use a `unix:` bind, `fd://` socket activation, or a
fronting proxy when interface filtering matters.

---

## Where to file new findings

Bug reports + protocol-spec disagreements:
[github.com/TOKUJI/BlackBull/issues](https://github.com/TOKUJI/BlackBull/issues).
Include the wire request (raw bytes if possible) and the
expected vs observed response.
