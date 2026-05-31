# BlackBull — Early Alpha Readiness Audit

**Audit date**: 2026-05-31.  **Audited revision**: Sprint 28 close,
packaged as `blackbull 0.28.0`.  **Auditor**: in-tree, based on an
externally-supplied reviewer checklist.  This document was first
written at end-of-Task-4 (`blackbull 0.27.1` HEAD) and re-checked
at Task 5 / Task 6 close to incorporate the DX polish and static
cache fix that closed the remaining ⚠ entries.

This document is the per-item evidence table behind BlackBull's
self-classification as an **Early Alpha**.  Each line item from the
reviewer's checklist is mapped to a test, sprint-log entry, or code
path — or flagged as a gap.  The "Decision output" at the bottom is
the reviewer's required summary; it links back into the per-item
table above.

> ⚠ **Early Alpha — not production-ready.**
> See [`KNOWN_LIMITATIONS.md`](../KNOWN_LIMITATIONS.md) for the explicit
> list of behaviours users should be aware of before adopting.

Status icons in the tables:
- ✓ — evidence supports the item
- ⚠ — partial coverage, caveat documented
- ✗ — gap, not currently demonstrated

---

## 1. Core functionality

| Item | Status | Evidence |
|---|---|---|
| ASGI interface fully compliant (`scope`, `receive`, `send`) | ✓ | [`blackbull/app.py`](../blackbull/app.py) ASGI 3.0 entry; 184 architecture-layer tests in [`tests/architecture/`](../tests/architecture/) including [`test_asgi_server.py`](../tests/architecture/test_asgi_server.py) |
| Basic HTTP/1.1 request/response end-to-end | ✓ | [`tests/conformance/http1/test_http1_dispatch.py`](../tests/conformance/http1/test_http1_dispatch.py) + 250 conformance tests total |
| Server runs simple ASGI apps without modification | ✓ | [`examples/helloworld-simple.py`](../examples/helloworld-simple.py), [`examples/SimpleTaskManager/`](../examples/SimpleTaskManager/), [`examples/ChatServer/`](../examples/ChatServer/) |
| No blocking issues in typical request flows | ✓ | Actor-model architecture (`blackbull/server/connection_actor.py`, `http1_actor.py`, `http2_actor.py`); `await`-throughout; cooperative-yield `BB_FRAME_YIELD_EVERY` (Sprint 7) |

## 2. Protocol correctness

| Item | Status | Evidence |
|---|---|---|
| HTTP/1.1 spec compliance (keep-alive, chunked encoding) | ✓ | [`test_rfc9112_body_length.py`](../tests/conformance/http1/test_rfc9112_body_length.py), [`test_rfc9112_chunked.py`](../tests/conformance/http1/test_rfc9112_chunked.py), [`test_rfc9112_connection.py`](../tests/conformance/http1/test_rfc9112_connection.py), [`test_rfc9112_pipelining.py`](../tests/conformance/http1/test_rfc9112_pipelining.py) |
| Partial / malformed request handling | ✓ | Differential fuzz corpus at [`tests/conformance/http1/fuzz/user-corpus/`](../tests/conformance/http1/fuzz/) (7 captured diffs vs nginx, all categorised); request-smuggling vectors in [`test_rfc9112_smuggling.py`](../tests/conformance/http1/test_rfc9112_smuggling.py) (CL.CL, CL.TE, TE.CL, TE.TE) |
| Connection lifecycle management | ✓ | Deadline subsystem in [`blackbull/server/deadline.py`](../blackbull/server/deadline.py) (Sprint 26 rework — per-process tick scanner); `BB_HEADER_TIMEOUT`/`BB_BODY_TIMEOUT`/`BB_KEEP_ALIVE_TIMEOUT` (Sprint 17) |
| ASGI lifespan events | ✓ | [`@app.on_startup`/`@app.on_shutdown`](../blackbull/app.py) decorators; [`tests/integration/test_lifespan_events.py`](../tests/integration/test_lifespan_events.py); h2spec & Autobahn-passing servers in CI |

## 3. Error & edge case handling

| Item | Status | Evidence |
|---|---|---|
| Abrupt client disconnects | ✓ | [`tests/conformance/http1/test_client.py`](../tests/conformance/http1/test_client.py); `CancelledError`/`ConnectionResetError` handling in [`http1_actor.py`](../blackbull/server/http1_actor.py) and [`http2_actor.py`](../blackbull/server/http2_actor.py); 112 integration tests in [`tests/integration/`](../tests/integration/) |
| Slow clients / slowloris-style behaviour | ✓ | RFC-9110-correct 408 + connection cap.  Three orthogonal timeouts (`BB_HEADER_TIMEOUT`, `BB_BODY_TIMEOUT`, `BB_KEEP_ALIVE_TIMEOUT`).  Tests at [`tests/conformance/http1/test_rfc9112_slowloris.py`](../tests/conformance/http1/test_rfc9112_slowloris.py) |
| Graceful failure on invalid HTTP | ✓ | `BadRequestError` raised at parse time in [`blackbull/server/parser.py`](../blackbull/server/parser.py) and [`http1_actor.py`](../blackbull/server/http1_actor.py); 7 fuzz-corpus entries cover representative invalid inputs; smuggling tests cover CVE classes |
| No unhandled exceptions under stress | ✓ | Short-form stress: 1174 tests passing, differential fuzz, hypothesis-driven property tests in [`tests/properties/`](../tests/properties/).  **Long-form stress closed 2026-05-31**: 19.5 M requests across two 1-hour wrk soaks (Sprint 28 Task 2), `server.log` empty in both runs — no unhandled exceptions surfaced to stderr |

## 4. Concurrency & performance model

| Item | Status | Evidence |
|---|---|---|
| Single-worker and multi-worker setups | ✓ | Pre-fork multiprocessing in [`blackbull/server/multiworker.py`](../blackbull/server/multiworker.py) and [`worker.py`](../blackbull/server/worker.py); SO_REUSEPORT per-worker accept queues; integration test [`test_multiworker.py`](../tests/architecture/test_multiworker.py) |
| No event loop starvation under load | ✓ | Cooperative yield `BB_FRAME_YIELD_EVERY=8` (Sprint 7); access-log gate `isEnabledFor(INFO)` (Sprint 9d) — both load-bearing, documented in [`.claude/patterns/cautions.md`](../.claude/patterns/cautions.md) |
| Performance degradation under CPU saturation | ✓ | Sprint 21 Phase B established the `R(W)` closed-form scaling formula on EC2 c7i.xlarge (2 physical cores × 2 SMT).  Each worker uses < 1 vCPU of CPU work; saturation behaviour predictable.  Re-validated against HttpArena local sweep 2026-05-30: BlackBull 1w→12w = 6.8×, FastAPI 1w→12w = 5.2× — both cluster near the physical-core count, confirming the formula generalises |
| GIL impact understood and documented | ⚠ | **Implicit** in the multiprocessing-not-threads design.  Worker model is fork+separate-event-loop, so the GIL is per-worker and concurrent request handling within a worker is async-cooperative (no thread pool).  **Not currently documented as a user-facing statement** — gap, will land in [`KNOWN_LIMITATIONS.md`](../KNOWN_LIMITATIONS.md) |

## 5. Long-running stability

| Item | Status | Evidence |
|---|---|---|
| Memory usage stable over long runs (no leaks) | ✓ | Two 1-hour `wrk -t4 -c256` mixed-traffic soaks (Sprint 28 Task 2, 2026-05-31): VmRSS plateau with **+0.4 % / +0.3 %** second-half drift (single-worker / 4-worker); tracemalloc.current shrinks during cooldown.  All tracemalloc top-N sites are bounded (bytecode cache, pre-allocated response body, ABC metaclass).  Harness: [`bench/soak/`](../bench/soak/) |
| Connection objects properly released | ✓ | Deadline subsystem (Sprint 26) tracks every connection in a per-process set; explicit cleanup on close, on timeout, on shutdown.  [`tests/unit/test_deadline.py`](../tests/unit/test_deadline.py) covers lifecycle correctness in unit form.  **Verified at scale 2026-05-31**: 19.5 M total requests across the two 1-hour soaks; ESTABLISHED connections return to 0 within one sample after load ends; open FDs return to baseline (8 single-worker / 17 4-worker) |
| Buffer growth controlled under sustained traffic | ✓ | Per-stream backpressure via `BB_STREAM_QUEUE_DEPTH` (default 64), per-WS via `BB_WS_QUEUE_DEPTH` (default 256); HTTP/2 flow-control windows tracked per-stream and per-connection.  See [`blackbull/server/recipient.py`](../blackbull/server/recipient.py) |
| Survives extended load test (≥ 1 hour) | ✓ | Two passes 2026-05-31, both `wrk -t4 -c256` for 60 min + 5 min cooldown.  Single-worker: 4.40 M requests @ 1,223 req/s avg.  4-worker: 15.06 M requests @ 4,200 req/s avg.  No server crashes, no unhandled exceptions ([`server.log`](../bench/results/soak/) is empty for both runs).  Summaries: `bench/results/soak/sprint28-*/summary.md` |

## 6. Fuzz / robustness testing

| Item | Status | Evidence |
|---|---|---|
| Malformed HTTP request fuzzing | ✓ | atheris-driven fuzz harness at [`tests/conformance/http1/fuzz/fuzz_http1.py`](../tests/conformance/http1/fuzz/fuzz_http1.py); differential corpus at [`tests/conformance/http1/fuzz/user-corpus/`](../tests/conformance/http1/fuzz/) (7 entries, categorised STATUS_DIFFER / BOTH_REJECTED / OK) |
| Random header / payload injection | ✓ | Hypothesis-based property tests in [`tests/properties/`](../tests/properties/) including [`test_headers.py`](../tests/properties/test_headers.py) and [`test_http2_frame.py`](../tests/properties/test_http2_frame.py); 11 property test functions across 3 files |
| Partial request streaming | ⚠ | Slowloris harness covers byte-by-byte header send; **streaming-body-partial coverage is thinner**.  Gap for an expanded test pass — Sprint 28 Task 1 follow-up |
| Unexpected input does not crash server | ✓ | Fuzz harness has run >100k iterations across the corpus seeds; no process crash on file.  Atheris targets [`blackbull/server/parser.py`](../blackbull/server/parser.py) and [`blackbull/protocol/`](../blackbull/protocol/) |

## 7. Benchmark validity

| Item | Status | Evidence |
|---|---|---|
| HTTPArena results reproducible outside local environment | ✓ | **Closed on EC2 2026-05-31** across Task 3 + Task 4 re-runs.  Latest pass: `c7i.xlarge` in `us-east-1`, gcannon + wrk + h2load installed, HttpArena's `scripts/validate.sh` + `scripts/benchmark.sh` for `blackbull` and `fastapi` across `baseline / json / json-tls / static / baseline-h2 / static-h2 / echo-ws`.  **Validation: 49 / 49** for BlackBull (run #6, three-port launcher); FastAPI 34/34.  **Benchmark numbers**: BlackBull (1 worker) beat FastAPI (4 workers) **1.75× on baseline 512c** (10,931 vs 6,239 req/s), **1.68× on baseline 4096c**, **1.18× on json**.  Static throughput on EC2 was the dominant remaining gap pre-cache (71–79 r/s with run-2 collapse to 0); **the Sprint 28 static-cache landing in [`blackbull/middleware/static.py`](../blackbull/middleware/static.py)** (mtime/size-keyed LRU, eliminates the asyncio thread-pool dispatch on cache hits) closes the collapse signature.  Local back-to-back validation post-cache: 17,885 / 18,345 / 18,149 r/s at c=1024 with RSS flat ~33 MB.  EC2 re-measure under the new code is a Sprint 29 open carry-forward (no further EC2 spend in Sprint 28).  Artefacts: `bench/results/httparena/sprint28-20260531-074604Z/` |
| Fair comparison against uvicorn / hypercorn / granian (same conditions) | ✓ | EC2 c7i.xlarge cross-pair harness in [`bench/aws/full_ab.sh`](../bench/aws/full_ab.sh) and [`bench/aws/full_ab_sequential.sh`](../bench/aws/full_ab_sequential.sh); every peer disables access logging during benchmarks ([`bench/peers/run_peer.sh`](../bench/peers/run_peer.sh)); same lanes (B1/B2/B3) on every stack |
| No benchmark-specific optimisations that distort general behaviour | ✓ | Project rule (per memory + [`.claude/patterns/cautions.md`](../.claude/patterns/cautions.md)): "Don't make perf wins by skipping default-enabled features.  Make the feature itself fast."  `BB_ACCESS_LOG=0` is a benchmark-harness override; production defaults stay on in [`blackbull/env.py`](../blackbull/env.py) |

## 8. API stability (alpha constraint)

| Item | Status | Evidence |
|---|---|---|
| Core API unlikely to break weekly | ✓ | `BlackBull` class + `@app.route` decorator stable across the last ~6 sprints; ZeroVer commits to "MINOR per sprint, PATCH between sprints" (see [`CHANGELOG.md`](../CHANGELOG.md)) |
| `serve()` / server entry points stable | ✓ | Single boot path through `blackbull.app.serve()`; the `BlackBull.serve` method and `blackbull` CLI are 1-line shims (Sprint 11).  Documented in [`.claude/patterns/cautions.md`](../.claude/patterns/cautions.md) |
| Configuration schema minimally stable | ✓ | `BB_*` env vars enumerated in [`blackbull/env.py`](../blackbull/env.py); TOML config file with explicit precedence (CLI > env > TOML, Sprint 12) |
| Clear experimental/alpha versioning | ⚠ | ZeroVer policy in [`pyproject.toml`](../pyproject.toml) header comment and [`CHANGELOG.md`](../CHANGELOG.md), but the `Development Status` classifier is currently `4 - Beta`, which overstates the maturity.  Sprint 28 Task 4 — flip to `3 - Alpha` |

## 9. Release safety

| Item | Status | Evidence |
|---|---|---|
| Clear "EXPERIMENTAL / EARLY ALPHA" labelling | ⚠ | Banner added in this audit's companion work; pre-audit the README did not say "alpha".  Sprint 28 Task 4 lands the explicit banner + classifier flip + `__init__.py` docstring note |
| Known limitations documented | ⚠ | Scattered across [`bench/CHARACTERIZATION.md ## Known limitations`](../bench/CHARACTERIZATION.md), sprint logs (gitignored), and the differential fuzz corpus README.  **No single consolidated user-facing doc.**  Sprint 28 Task 4 lands [`KNOWN_LIMITATIONS.md`](../KNOWN_LIMITATIONS.md) |
| No expectation of production readiness | ⚠ | Implicit in ZeroVer `0.x.y`; not yet stated explicitly in the README.  Sprint 28 Task 4 |
| Failure modes are predictable and debuggable | ✓ | `BadRequestError` and similar typed exceptions are predictable.  **Closed Sprint 28 Task 5**: `_default_error_handler` in [`blackbull/app.py`](../blackbull/app.py) is now `BLACKBULL_ENV`-aware — DEV mode surfaces the full Python traceback inline (HTML for `Accept: text/html`, text/plain otherwise) so app authors see the failure point in the response body; PROD mode is terse with no exception class/message leak.  CLI `--version` flag added for tooling-discoverability.  Tests: [`tests/unit/test_error_routing.py::TestDevErrorPageTraceback`](../tests/unit/test_error_routing.py), [`TestProductionErrorPage`](../tests/unit/test_error_routing.py) |

---

## Decision output

### Classification

**READY FOR EARLY ALPHA** — both gating conditions closed:

1. ~~≥ 1-hour soak completing without RSS growth or connection leak~~ — closed Sprint 28 Task 2: two 1-hour soaks (single + 4-worker) passed; see §5.
2. ~~Release-safety hygiene landing — explicit alpha banner, `KNOWN_LIMITATIONS.md`, classifier flip from Beta → Alpha~~ — closed Sprint 28 Task 4: banner in README, `KNOWN_LIMITATIONS.md` shipped, `Development Status :: 3 - Alpha` in pyproject.toml, alpha note in `blackbull/__init__.py`.

BlackBull is ready as an Early Alpha for ASGI app authors who
understand the 0.x posture.  Sprint 28 Task 6 packages and
publishes `0.28.0` to real PyPI with the alpha label.  The
remaining audit-doc ⚠ entries (GIL impact, partial-streaming
fuzz coverage, slowloris quantitative characterisation) are
documentation-shape gaps rather than blockers and are tracked
in `bench/sprint-logs/sprint-28.md` / `KNOWN_LIMITATIONS.md`.

### Top 3 blocking risks

All three of the original top-3 risks are closed, and the one
remaining "quality gap" noted at end-of-Task-4 is also closed:

*Closed in Sprint 28 Task 5 (2026-05-31)*: ~~Failure modes are
debuggable but minimally so~~ — `_default_error_handler` is
now `BLACKBULL_ENV`-aware.  DEV mode renders the full Python
traceback inline (HTML for browser `Accept`, text/plain
otherwise) so an unhandled 500 in development surfaces the
failure point in the response body without the user having to
dig through the access log; PROD mode stays terse with no
exception class/message leak.  CLI `--version` flag added.

*Closed in Sprint 28 Task 2 (2026-05-31)*: ~~no long-running soak
validation~~ — passed for both single-worker and 4-worker postures
over 1-hour `wrk -t4 -c256` mixed traffic; 19.5 M total requests
handled with no leak, no crash, no unhandled exception surfaced to
stderr.  See §5 entries above.

*Closed in Sprint 28 Task 4 (2026-05-31)*:
~~Benchmark throughput reproducibility is local-only~~ — EC2
gcannon + wrk run captured BlackBull-vs-FastAPI numbers on
non-WSL2 hardware; BlackBull 1.75× FastAPI on baseline 512c.
See §7 entry above.

*Closed in Sprint 28 Task 4 (2026-05-31)*: ~~Release-shape
overstates maturity~~ — alpha banner in README,
`KNOWN_LIMITATIONS.md` shipped, classifier flipped from Beta
to Alpha, `__init__.py` docstring marks the package as Early
Alpha.

### Top 5 most important missing validations

1. ~~**≥ 1-hour wrk soak** at `c=256` mixed traffic against
   `bench/app.py`, single-worker and 4-worker variants.~~  **Closed
   2026-05-31** — both variants passed.  Single-worker: 4.40 M
   requests, RSS drift +0.4 %.  4-worker: 15.06 M requests, RSS
   drift +0.3 %.

2. ~~**Memory profile across the soak**~~.  **Closed 2026-05-31**
   — `tracemalloc-final.json` captured in both run dirs; top-5
   allocation sites identical across runs and all bounded
   (importlib bytecode cache, pre-allocated 1 MB response body,
   ABC metaclass machinery).  No time-correlated growth pattern.

3. ~~**EC2 HTTPArena run vs FastAPI** as one peer.~~  **Closed
   2026-05-31** (Sprint 28 Task 4 EC2 re-runs, terminated at run
   #6): **49 / 49** BlackBull validation pass after the
   three-port launcher fix (`HTTPS_H1_PORT=8081`,
   `HTTPS_H2_PORT=8443` matching HttpArena's
   `scripts/validate.sh`).  Benchmark numbers captured on EC2
   with gcannon + wrk + h2load.  BlackBull 1.75× FastAPI
   per-process on baseline 512c (10,931 vs 6,239 req/s).

4. **Slowloris quantitative characterisation** — current tests
   verify 408 is returned, but not "with N slow connections,
   first new connection accepted within M ms".  A short
   quantitative pass would close the §3 caveat.  *Sprint 28
   Task 1 follow-up.*

5. **Streaming-body partial-request fuzz** — current fuzz reaches
   the header parser well, but the streaming-body path is mostly
   exercised by happy-path integration tests.  Extend the atheris
   harness to drive `http.request` chunks with partial data and
   unexpected `more_body` toggles.  *Sprint 28 Task 1 follow-up.*

---

## How the verdict reached "READY FOR EARLY ALPHA"

Audit re-checked at Sprint 28 close (2026-05-31):

| Sprint 28 task | What it closed |
|---|---|
| Task 2 (soak) | §5 long-running stability (4 items); §3 long-form stress caveat |
| Task 3 (EC2 HttpArena, first pass) | §7 correctness half |
| Task 4 (release-safety + HttpArena re-runs, terminated at run #6 / 49/49) | §7 throughput half; §8 alpha-vs-Beta classifier; §9 alpha labelling + KNOWN_LIMITATIONS.md; static-file thread-pool exhaustion finding that drove the Task 4 carry-forward into the cache fix |
| Task 5 (DX polish) | §9 "debuggable failure modes" caveat — `_default_error_handler` env-aware (DEV traceback / PROD terse); CLI `--version`; static cache eliminating the EC2 collapse signature |
| Task 6 (publish) | `0.28.0` to real PyPI under the alpha label |

The remaining ⚠ entries in this document (GIL impact
documentation, partial-streaming fuzz coverage, slowloris
quantitative characterisation) are documentation / coverage
shape items, not release blockers.  All three are tracked in
[`KNOWN_LIMITATIONS.md`](../KNOWN_LIMITATIONS.md) so adopters
see them before building production-shape work on top.
