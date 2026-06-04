# BlackBull — Known limitations

This document is the single user-facing inventory of behaviours and
gaps that may surprise an app author adopting BlackBull at its
current **Early Alpha** maturity level.  The companion
[`docs/about/conformance.md`](docs/about/conformance.md) records the
protocol-level test coverage behind the standards-compliance claims;
this file is the narrative "things to know before you build on top
of it" list.

The list is consolidated from internal sprint logs and the
differential nginx fuzz corpus — every item here is something we
*know about* and have a position on.  Items that are surprises to
us as well belong on the GitHub issue tracker, not here.

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
blocked and the browser falls back to HTTP/1.1.  We don't have
the test coverage to promote this to default-on yet.

### HTTP/2 multiplex overhead vs HTTP/1.1

In WSL2 single-host benchmarks (Sprint 27 close-out HttpArena
sweep), HTTP/2 per-process throughput trailed HTTP/1.1 by
roughly **10-35 %** depending on multiplex depth.  The H/2
implementation is conformant (passes `h2spec`, RFC 9113) but
the per-stream actor machinery (queue, sender state, priority
tree) adds per-request overhead that the H/1.1 path doesn't pay.
Single-stream H/2 latency at high mux (c=128 × m=32) can stretch
into the hundreds of milliseconds under saturation.

**Position**: planned revisit after Sprint 28; not the current
priority target.  If your workload needs maximum H/1.1
throughput, BlackBull is competitive; if it needs maximum H/2
multiplexed throughput on a single connection, a hand-tuned
Rust server will do better today.

### h2c prior-knowledge shares the H/1.1 port

BlackBull's plaintext listener auto-detects the HTTP/2 connection
preface and switches to h2c — there is no separate "h2c-only"
port that refuses H/1.1.  This is RFC-permissible (RFC 9113
allows h2c via prior knowledge on any port) but **diverges from
HttpArena's expectation** of an h2c-only :8082; we don't claim
the `baseline-h2c` / `json-h2c` HttpArena profiles in
`bench/httparena/meta.json` for this reason.

### Slowloris response is correct but not quantitatively characterised

Three timeouts (`BB_HEADER_TIMEOUT`, `BB_BODY_TIMEOUT`,
`BB_KEEP_ALIVE_TIMEOUT`, Sprint 17) defend against partial-data
attacks; tests verify a 408 is returned.  **What's not
characterised**: the exact "with N slow connections, first new
connection accepted within M ms" curve.  No quantitative
hardening claim beyond "RFC 9110 §15.5.9 compliant 408 + the
three timeouts work".

---

## RFC-defensible differential-corpus divergences

The differential fuzz harness compares BlackBull's response to
the same request against nginx.  Two captured divergences are
**RFC-defensible**, kept in the corpus deliberately, and not
treated as bugs:

| Wire request | nginx | BlackBull | Why we're right |
|---|---|---|---|
| `GET&nbsp;&nbsp;http://localhost/x HTTP/1.0` (double-SP between method and target) | 200 | 400 | RFC 9112 §3 — request-line tokens are separated by exactly one SP.  nginx is lenient; we reject. |
| `GET  /x HTTP/9.9` (validation-order on unknown version) | 400 | 505 | RFC 9110 §15 — unsupported HTTP version is 505 (HTTP Version Not Supported); nginx returns the more generic 400. |

Both are documented in
[`tests/conformance/http1/fuzz/user-corpus/diff_README.md`](tests/conformance/http1/fuzz/user-corpus/).
We're not chasing nginx parity here unless a real user need
appears.

---

## Benchmark + measurement caveats

### WSL2 noise floor swallows optimisations under ~15 %

Sprint 9d established that on WSL2 loopback, the B2r latency
spread for an unchanged code path is 1.3-5.1 ms (a > 3× spread
with no code change).  Any throughput / latency optimisation
below ~15 % is invisible at WSL2 scale.  Sub-millisecond claims
belong on EC2.

### HttpArena local numbers are not directly comparable to their leaderboard

HttpArena's public leaderboard runs on dedicated 64-core
hardware.  The numbers in
[`bench/CHARACTERIZATION.md`](bench/CHARACTERIZATION.md) and
the Sprint 27 close-out HttpArena comparison are local WSL2
single-host runs; they validate BlackBull's *shape* relative to
peers on the same machine but absolute throughput does not
predict leaderboard ranking.

### Multi-worker scaling is sub-linear

`R(W)` scales to roughly the **physical core count**, not the
logical (SMT-thread) count (Sprint 21 Phase B `R(W)` formula;
re-validated in the Sprint 27 close-out HttpArena sweep where
both BlackBull and FastAPI clustered near `nproc / 2` for
1w → Nw scaling).  Multiplying workers past the physical-core
ceiling does not multiply throughput.

### Single-host benchmark caveat

When the load generator and server share a host, kernel
scheduling and accept-queue contention skew both throughput
*and* latency away from real-NIC behaviour.  Use a split
topology (`bench/aws/up.sh TOPO=split`) for any number that
needs to reflect production-shape traffic.

---

## What BlackBull doesn't do

### Static-file serving is not a production CDN

[`blackbull/middleware/static.py`](blackbull/middleware/static.py)
serves files three ways:

- Cached in-memory (≤ 4 MiB) — first hit reads sync, subsequent
  hits serve from a per-process LRU.
- Zero-copy via `loop.sendfile` (cleartext HTTP/1.1, > 4 MiB,
  no Range) — single kernel-side transfer, no per-chunk
  event-loop dispatch.  Opted in via the `http.response.pathsend`
  ASGI extension; cleartext HTTP/1.1 advertises it.
- Chunked through `asyncio.to_thread` (TLS, HTTP/2, Range
  requests) — correct, but the per-chunk thread-pool dispatch
  costs roughly 64 µs each.  Use the fronting nginx path below if
  this is on the critical path for you.

The in-memory cache is stat-invalidated per request — every
request runs `path.stat()` and refills the entry when mtime or
size changes, so edits on disk show up on the next request with
no staleness window.  What's still missing: ETag-driven
revalidation, byte-range-multipart, and CDN edge-cache
invalidation glue.  For anything user-visible, front a real
static-file server (nginx, S3 + CloudFront).

### No internal database layer

BlackBull is a protocol-layer framework.  There is no built-in
ORM, no connection pool, no migration tool.  Apps should bring
their own (`asyncpg`, `databases`, `tortoise-orm`, etc.).
HttpArena's database-backed profiles (`async-db`, `crud`,
`fortunes`, `api-4`, `api-16`) are intentionally not
implemented in `bench/httparena/`.

### Optional `[speed-h1]` C parser stub not implemented

`pyproject.toml` lists no `[speed-h1]` extra today.  The
pure-Python HTTP/1 parser is fast enough on the cascade-rule
metric (Sprint 27 profile showed `_parse` at ~2 % B1 self-time
— sub-cascade).  A future user-facing knob to swap in
`httptools` is documented in the roadmap but not built.

### No HTTP/3 / QUIC

Out of scope.  Revisit if a real user need appears.

### No gRPC

Out of scope.

### CLI ergonomics are minimal

The `blackbull` console script ships in 0.27.1 but only covers
the basic ASGI-runner shape (`blackbull app:app --bind ...`).
A few quality-of-life knobs (`--version`, helpful error on
missing `app:module`, `--reload-dir`) are pending and tracked
in Sprint 28 Task 5.

---

## What's flagged but not yet measured

These items will be addressed in Sprint 28 or Sprint 29 close:

- **Quantitative slowloris characterisation** — current tests
  verify 408 is returned, not the quantitative "N held conns →
  M-ms new-connection latency" curve.
- **Streaming-body partial-request fuzz** — atheris fuzz reaches
  the header parser well, the streaming-body path less so.

---

## Where to file new findings

Bug reports + protocol-spec disagreements:
[github.com/TOKUJI/BlackBull/issues](https://github.com/TOKUJI/BlackBull/issues).
Include the wire request (raw bytes if possible) and the
expected vs observed response.
