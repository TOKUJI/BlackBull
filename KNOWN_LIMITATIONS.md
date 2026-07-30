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

**A limitation is not the same as a missing feature.** This file lists
things that could *surprise* you: a default that is not what you would
guess, a cost you would not predict, a surface that is narrower than its
equivalent elsewhere.  Capabilities BlackBull simply doesn't aim to
provide — HTTP/3, an ORM, a gRPC client — are not limitations; they are
scope, and they live in [Deliberate non-goals](#deliberate-non-goals--not-limitations)
at the end so they don't pad the list above.

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

### HTTP QUERY (RFC 10008): `Accept-Query` is not synthesised on OPTIONS

QUERY routing, `Accept-Query` content negotiation, and Content-Type
enforcement ship (see the [routing guide](docs/guide/routing.md)).  One
edge: RFC 10008 mentions emitting `Accept-Query` on OPTIONS for a path,
but BlackBull only auto-handles OPTIONS when a route is registered for
it, so the header rides the QUERY route's own responses (including its
415) rather than being synthesised on an unrouted OPTIONS.

---

## Runtime constraints

Behaviours that hold at run time regardless of how you deploy.  The
operational how-to for each lives in the deployment guide; what's here is
the part that can surprise you.

### Raw protocol sockets are cleartext unless you ask for TLS

A socket bound by `app.raw_handler()` or `app.register_protocol_handler()`
carries **no TLS by default**.  Register the binding with `tls=True`
(`app.raw_handler(name, port=…, tls=True)`, `MQTTExtension(port=8883,
tls=True)`) to serve it through the same TLS machinery as the HTTPS
listener — certificate from `certfile`/`keyfile` or `ssl_context`, with
startup failing fast when `tls=True` has no certificate to use.  A
TLS-terminating proxy in front of the raw socket is a fine alternative.

A raw protocol also has a **single owner** — it is bound on worker 0 only,
while HTTP scales across all workers.  See
[Workers](docs/deployment/workers.md) for the mechanics, including why
`--reload` forces `workers=1` when a protocol port is bound.

### MQTT broker state is in-memory and lost on restart

The MQTT 5 broker (`blackbull.mqtt`, opt-in via `blackbull[mqtt]`) rides the
raw-protocol bridge, so it inherits both constraints above.  Beyond those:

**State does not survive a restart.**  Subscriptions, sessions, and retained
messages live in the one serving process — not shared across workers, not
persisted.  A restart, or a worker-0 respawn after a crash, clears all
session and retained state.  Sessions are kept for the process lifetime
rather than expired on a timer.

**Nothing is queued for offline sessions.**  A disconnected client with a
live session (`Session Expiry Interval > 0`) gets §4.4 replay of its
*unacknowledged in-flight* QoS 1/2 messages on reconnect, but messages
**newly published while it was offline are not queued** and will never reach
it.  The same applies to a shared-subscription group with no connected
member.  If you need store-and-forward for offline consumers, use a broker
with persistent offline queues.

**`on_message` taps are best-effort observability, not a delivery path.**
In the default `tap_mode='actor'` each PUBLISH is *offered* to a
bounded-inbox `TapActor` that drops the newest message on overflow (with a
running dropped-count logged), so a slow tap can never back-pressure
routing.  Use a real MQTT subscription when you need guaranteed delivery.

Why one owner is a *protocol* requirement rather than an implementation
shortcut — and how Mosquitto and EMQX handle the same constraint — is
explained in [the MQTT guide](docs/guide/mqtt.md).

---

## Deliberately narrow surfaces

These are implemented and supported, but fenced tighter than the
equivalent surface in a larger framework.  Each fence is lifted on
demonstrated demand, not speculatively.

### Static-file serving: know the per-request cost

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

`StaticFiles` emits a strong `ETag` (over mtime + size) and
`Last-Modified`, and answers `If-None-Match` / `If-Modified-Since`
with a `304`, on by default via `conditional=True`.  Pass
`conditional=False` to suppress the validators.  Byte-range-multipart
responses are not implemented across any of the three paths.

`app.static()` registers a route rather than global middleware, so a
request that is not for a static path never enters `StaticFiles`.  The
consequence to plan around: a miss *under* the prefix is answered `404`
by the static route rather than falling through to another route that
also matches the prefix.  Fully-static routes still resolve first, so
only an overlapping *parametrised* route is affected.

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
`conn.query_string` yourself for those.  Fences are lifted on
demonstrated demand, not speculatively.

### WebSocket injection: annotated query params only, one lifetime

Since v0.64.0 a WebSocket handler resolves path params, query
params, and `Depends` from its signature, as an HTTP handler does.
Two deliberate differences remain.

A WebSocket **query param must carry its annotation** — on an HTTP
route a bare name is taken as a `str` query param, but the reserved
names (`ws`, `websocket`, `conn`, `connection`) make bare parameters
ambiguous on a socket, so `async def chat(socket)` stays a
registration-time `TypeError` instead of silently becoming a query
param named `socket`.  There is also no body parameter: a WebSocket
has no request body, so the HTTP `body`/dataclass forms have no
analogue.

`Depends` resolves **once per connection**, with no way to ask for
another lifetime.  That is correct for values and wrong for scarce
resources — a socket pins its dependency for hours, so a pool of N
serves N concurrent sockets.  Application-scope the pool
(`@app.on_startup`) and borrow per use; see the dependency-lifetime
section of `docs/guide/websockets.md`.  Per-message scope is not
merely unimplemented but structurally unavailable: it needs a
per-frame dispatch seam, and the handler owns its receive loop.  A
declared-scope API is unlocked on demonstrated demand, not
speculatively.

### gRPC: reflection is v1alpha-only

All four gRPC RPC shapes — unary, server-streaming, client-streaming, and
bidirectional — ship in `blackbull.grpc` (`app.enable_grpc`), with `gzip`
message compression (`grpc-accept-encoding: identity,gzip`).
`application/grpc` requests multiplex onto the same HTTP/2 port as REST and
WebSocket, with `grpc-status` reported in trailers (a trailing HEADERS
frame). Core handlers exchange raw message bytes by design; object-typed
servicers, server reflection, `grpc.health.v1`, and rich error details ship
in the optional [`blackbull-protobuf`](https://github.com/TOKUJI/blackbull-protobuf)
package (`pip install 'blackbull[protobuf]'`). The remaining gap: reflection
serves `grpc.reflection.v1alpha` only — `v1` is a planned fast-follow, and
grpcurl and most clients fall back to v1alpha automatically. See
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

## Deliberate non-goals — not limitations

Capabilities BlackBull does not aim to provide.  Nothing here is a gap
in an implemented feature; each is a scope decision with a supported
alternative.  Listed so the question is answered once — not as a
shortfall list.

| Not provided | Why, and what to use |
|---|---|
| **HTTP/3 / QUIC** | Out of scope at Early Alpha. Revisit if a real user need appears. Front with a proxy that terminates H3 if you need it at the edge. |
| **ORM / connection pool / migrations** | BlackBull is a protocol-layer framework, like Flask and Starlette. Bring `asyncpg`, `databases`, `tortoise-orm`, or `SQLAlchemy`. |
| **A gRPC client** | Server-side only, by design. Use `grpcio` for clients. |
| **CDN edge-cache invalidation glue** | For user-visible static assets, front a real static server or CDN (nginx, S3 + CloudFront). The built-in `StaticFiles` targets app-adjacent assets. |
| **A C-accelerated HTTP/1 parser (`[speed-h1]`)** | No such extra exists. A pure-Python parser on every path is the project's identity; accelerating it with a C dependency would trade that away. Performance work targets the Python path instead. |

---

## Where to file new findings

Bug reports + protocol-spec disagreements:
[github.com/TOKUJI/BlackBull/issues](https://github.com/TOKUJI/BlackBull/issues).
Include the wire request (raw bytes if possible) and the
expected vs observed response.
