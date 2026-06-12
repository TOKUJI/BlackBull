# Changelog

All notable changes to BlackBull are recorded here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## Versioning

BlackBull uses [ZeroVer](https://0ver.org/) prior to a 1.0 commitment:

- `0.MINOR.PATCH`
- `MINOR` advances at each sprint close (one minor per closed sprint).
  The number matches the sprint: `0.26.0` = Sprint 26 close.
- `PATCH` covers bug fixes / harness work between sprints.
- No `1.0.0` until the framework's identity (pure-Python H1 parser,
  BlackBull-internal `ASGIServer`, per-process tick scanner deadline
  subsystem) and public API have stabilised across several sprints.

The runtime version is exposed as `blackbull.__version__` via
`importlib.metadata.version("blackbull")` — single source of truth is
`pyproject.toml`.  Re-run `pip install -e .` after a local version bump
so the editable install's metadata catches up.

---

## [Unreleased]

(Nothing yet.)

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