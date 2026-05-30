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

(Nothing yet — next sprint pending.)

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

Sprint 24 — external-audit follow-ups + Lane E.

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
  `bench/CHARACTERIZATION.md`; per-sprint status badges; "Audit
  recommendations already in place" defensive cross-ref.

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