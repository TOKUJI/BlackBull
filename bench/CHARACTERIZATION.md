# BlackBull characterization plan

A diagnostic and regression-tracking document for BlackBull.  The
scenarios, metrics, and methodology here exist to **attribute cost**
(which layer of the stack pays the cycles?), **detect regressions**
(does a release behave like the previous one?), and **isolate
topology effects** from code effects.  This is not a competitive
benchmark report — external servers appear only as diagnostic
references and architectural contrasts (see "Reference servers and
diagnostic roles" below).

The matrix is the contract: every release should be measurable against
it, and every measurement run produces the same shape of table so
deltas are visible.

> ⚠️ **Cross-topology absolute-latency comparisons are invalid.**
> Only throughput trends and relative scaling *within the same
> topology* should be interpreted quantitatively.  WSL2, EC2
> single-host, and EC2 split topologies each carry a different
> per-request floor (loopback syscall cost vs VPC RTT); mixing them
> produces apparent regressions that aren't real.  See the
> "Comparability across topologies" notes under each sprint that
> changed topology.

## What BlackBull optimises for

In order of priority:

1. **Correctness under malformed traffic.**  RFC 9112 §15 slowloris
   defence, RFC 7541 framing-violation rejection, smuggling-vector
   coverage (CL.CL, CL+TE, unknown TE).  The differential corpus
   under [`tests/conformance/http1/fuzz/user-corpus/`](../tests/conformance/http1/fuzz/user-corpus/)
   is the ratchet.
2. **Explicit timeout semantics.**  Per-phase deadlines (header /
   body-per-chunk / keep-alive idle), all env-knob configurable.
   No "magic" defaults.
3. **HTTP/2 protocol stack implemented in Python.**  Frame parser,
   stream state machine, flow control, and (via the `hpack` package)
   HPACK codec all live in Python.  The event loop may still be
   `uvloop` (C); the *protocol* layer is what we mean here.  Sets
   BlackBull apart from granian (Rust runtime + Rust parser) in
   this matrix.
4. **Predictable multi-worker scaling** on commodity Linux —
   characterised by Sprint 21 Phase B's closed-form `R(W)` formula.
5. **Implementation clarity / from-scratch identity.**

Peak req/s comes after all five.  External servers in the matrix are
diagnostic references — see "Reference servers and diagnostic roles"
below — not targets to beat.

## Goals

1. **Self-characterization.** Know how BlackBull behaves across
   protocol, payload size, and load.  The matrix is the
   regression-tracking baseline; the per-sprint findings below are
   the bottleneck-attribution log.
2. **Cost decomposition.** Same ASGI app on multiple servers, same
   load generator, same hardware.  Per-stack deltas help attribute
   cost to specific layers (TLS termination, HTTP/1.1 parsing, ASGI
   dispatch, HPACK codec).  Numbers are *relative* and only
   citeable against another number from the same topology.
3. **AWS-ready.** Methodology is stable enough that re-running on EC2
   produces directly comparable numbers under the same topology
   (only the host changes).

## Reference servers and diagnostic roles

Five servers in the matrix.  Each plays a specific diagnostic role —
not a competitor — chosen so that BlackBull-vs-reference deltas
isolate a specific layer of cost.

| Server     | Version pinned in | HTTP/1.1 | HTTP/2 | WebSocket | Diagnostic role |
|------------|---|---|---|---|---|
| BlackBull  | repo head        | ✅ | ✅ | ✅ | Implementation under regression-tracking. |
| uvicorn    | `bench/peers/`   | ✅ | ❌ | ✅ | **Python ASGI decomposition reference.**  Pure-Python (h11/wsproto) HTTP/1.1 with a mature parser pipeline; BlackBull-vs-uvicorn deltas isolate per-request framework overhead on the same runtime substrate. |
| hypercorn  | `bench/peers/`   | ✅ | ✅ | ✅ | **Python ASGI decomposition reference (HTTP/2).**  Pure-Python h2-library based; BlackBull-vs-hypercorn deltas isolate HTTP/2-specific framework overhead independent of hpack codec cost (both use the same `hpack` package). |
| granian    | `bench/peers/`   | ✅ | ✅ | ✅ | **Architectural contrast reference.**  Rust runtime + Rust parser; BlackBull-vs-granian deltas attribute cost to the pure-Python runtime as a whole, not to any specific BlackBull layer. |
| nginx      | `bench/peers/`   | ✅ | ❌ | ❌ | **Architectural contrast reference (static).**  C event-driven, no ASGI dispatch; BlackBull-vs-nginx deltas indicate the floor cost of doing Python-application work at all. |
| daphne     | `bench/peers/`   | ✅ | ❌ | ✅ | **Compatibility reference.**  Django's canonical ASGI implementation — included for compatibility tracking, not cost comparison. |

ASGI app is identical across uvicorn/hypercorn/granian/daphne;
BlackBull uses its own `bench/app.py` with the same routes.  See
*Test target* below.

## Test target

One **shared minimal ASGI module** at `bench/peers/asgi_app.py` is loaded
by uvicorn / hypercorn / granian / daphne. BlackBull's `bench/app.py` is
extended with the same routes. Same handler bodies, different runner.

Two route sets — public-comparable and internal-only:

### Comparable set (matches TechEmpower / granian)

| Route        | Method | Body                                       | Content-Type        | Matches |
|--------------|--------|--------------------------------------------|---------------------|---------|
| `/plaintext` | GET    | `b'Hello, World!'` (13 B)                  | `text/plain`        | TechEmpower Plaintext |
| `/json`      | GET    | `b'{"message":"Hello, World!"}'` (27 B)    | `application/json`  | TechEmpower JSON |
| `/echo`      | POST   | echo request body                          | (echo or octet)     | granian echo (1 KiB + 100 KiB body sizes) |

These routes lead to numbers directly comparable to the public
benchmark boards. Bodies are pre-encoded at import time.

### Internal set (BlackBull characterization)

| Route       | Method | Body                              | Purpose |
|-------------|--------|-----------------------------------|---------|
| `/ping`     | GET    | `b'pong'` (4 B)                   | Framework overhead floor (smaller than /plaintext) |
| `/1kb`      | GET    | 1 KiB constant random             | Small response, fits one frame |
| `/16kb`     | GET    | 16 KiB constant random            | Medium, single DATA frame |
| `/64kb`     | GET    | 64 KiB constant random            | Crosses default max frame size |
| `/1mb`      | GET    | 1 MiB constant random             | Exercises flow-control window |
| `/ws`       | WS     | echo                              | WebSocket RTT |

Constant bodies are generated once at import time so allocation cost is not
in the request path. No compression, no auth, no logging.

## Scenarios

Each scenario fixes (load tool, protocol, route, concurrency, duration).
The Lane A / B / C / D / E grouping below reflects the **load-tool
and protocol** dimension and is how the harness invokes them.  The
table immediately under this paragraph re-projects the same scenarios
onto their **diagnostic purpose** — which question each one helps
answer — for readers who care about *why* a row exists, not *which
binary produced it*.

### Scenarios by diagnostic purpose

| Diagnostic purpose | What it isolates / attributes | Scenarios |
|---|---|---|
| **Per-request overhead** | The floor cost of routing + dispatch + framing for a tiny payload — isolates framework overhead from body-handling cost. | A1 (HTTP/2, 1 stream), B1 (HTTP/1.1, c=256), B3 (HTTP/1.1, c=256, JSON), C1 (k6 200 VU at /ping) |
| **Payload scaling** | How per-request cost grows with body size — attributes cost to serialization, flow control, and framing chunking. | A5 / A6 / A7 (16 KB / 64 KB / 1 MB over HTTP/2), B4 / B5 (16 KB / 64 KB over HTTP/1.1), B6 / B7 (POST 1 KB / 100 KB) |
| **Concurrency scaling** | How throughput and tail latency respond to more parallelism — separates stream multiplexing (intra-connection) from connection parallelism (inter-connection). | A2 / A3 (HTTP/2 mux=10 / 50), B1 / B2 (c=256 / c=1024 + pipeline 16), C2 (k6 500 VU at saturation) |
| **Connection lifecycle** | Cost of `accept` + TLS handshake + close per request — attributes the connection-setup tax separately from steady-state work.  Adjacent to the slowloris-defence test suite in `tests/conformance/http1/test_rfc9112_slowloris.py`. | E1 (HTTP/1.1, no-keepalive, Sprint 24) |
| **Worker scaling** | How throughput and tail latency respond to additional workers on the same instance — attributes scaling behaviour to per-worker vs per-host costs. | Stack-name suffix `-w<N>`: all of Lane A + B + C re-run at `w=1 / w=2 / w=4` (Sprints 16, 20, 21) |
| **Pipeline behavior** | How HTTP/1.1 pipelining changes the per-request floor — synthetic upper bound; pipelining is effectively dead in modern browsers. | B2 (c=1024 + pipeline 16) |
| **WebSocket round-trip** | Per-message frame-encode + frame-decode cost on a persistent connection. | D1 (k6 ws, 50 conns at 5 msg/s/conn) |

### Lane A — HTTP/2 multiplexing (h2load)

Applies to: BlackBull, hypercorn, granian.

| Scenario | Route        | -n (requests) | -c (conns) | -m (streams/conn) | Diagnostic purpose |
|---|---|---|---|---|---|
| A1 | `/plaintext` | 50,000 | 50 | 1   | Per-request HTTP/2 overhead with mux=1 — isolates frame parser + ASGI dispatch from stream-multiplexing cost. |
| A2 | `/plaintext` | 90,000 | 50 | 10  | Browser-realistic multiplexing — attributes per-stream cost. |
| A3 | `/plaintext` | 90,000 | 50 | 50  | Heavy multiplexing — not browser-realistic (browsers cap at ~10 concurrent streams); kept to characterise multiplexing scaling behaviour at high stream counts. |
| A4 | `/json`      | 50,000 | 50 | 10  | Tiny-JSON path on HTTP/2 — attributes JSON serialization cost vs A2. |
| A5 | `/16kb`      | 50,000 | 50 | 10  | Medium-body payload-scaling — exposes per-byte framing/copy cost. |
| A6 | `/64kb`      | 30,000 | 50 | 10  | Large body crossing default `max_frame_size` — attributes frame-chunking cost. |
| A7 | `/1mb`       |  3,000 | 20 |  3  | Body large enough to exercise the connection flow-control window — attributes window-update cost. |

### Lane B — HTTP/1.1 keep-alive (wrk + oha)

Applies to: all five servers. Two tools run side-by-side: **wrk** (with Lua
script for pipelining + POST bodies, TechEmpower-style) and **oha** (single
binary, granian-style). Each tool measures independently — the spread
between them is itself a data point.

| Scenario | Route        | Threads | Conns | Duration | Pipeline | Diagnostic purpose |
|---|---|---|---|---|---|---|
| B1 | `/plaintext` | 4       | 256   | 30s      | none     | Per-request HTTP/1.1 overhead baseline (TechEmpower-style; usable as an external comparability anchor). |
| B2 | `/plaintext` | 4       | 1024  | 30s      | 16       | Pipelining behaviour — synthetic upper bound on per-request floor; HTTP/1.1 pipelining is effectively dead in modern browsers, kept for cross-board comparability only. |
| B3 | `/json`      | 4       | 256   | 30s      | none     | Tiny-JSON path on HTTP/1.1 — attributes JSON serialization cost vs B1. |
| B4 | `/16kb`      | 4       | 100   | 30s      | none     | Medium-body payload-scaling — exposes per-byte copy/write cost. |
| B5 | `/64kb`      | 4       |  50   | 30s      | none     | Large-body payload-scaling — saturates the TLS write path. |
| B6 | `/echo`      | 4       | 100   | 30s      | none     | POST 1 KiB body — attributes request-body-read cost (granian-comparable). |
| B7 | `/echo`      | 4       |  50   | 30s      | none     | POST 100 KiB body — exposes chunked-read / large-body-read cost (granian-comparable). |

### Lane C — k6 stress (VU-based latency distribution)

Applies to: all five servers. Each VU holds one persistent connection.
HTTP/2-capable servers negotiate via ALPN; others fall back to HTTP/1.1.
The `proto` field in the summary records which actually ran.

| Scenario | Route | VUs | Duration | Diagnostic purpose |
|---|---|---|---|---|
| C1 | `/ping`  | 200 | 60s | Rampup-realistic concurrent VUs — characterises latency distribution at moderate load. |
| C2 | `/ping`  | 500 | 60s | Saturation behaviour — exposes per-event-loop-turn cost and tail-latency growth under contention. |

### Lane D — WebSocket RTT (k6 ws)

Applies to: all five servers.

| Scenario | Route | Conns | Message rate | Duration |
|---|---|---|---|---|
| D1 | `/ws` | 50 | 5 msg/s/conn | 60s |

### Lane E — Connection churn (wrk no-keepalive) — Sprint 24

Applies to: all five servers.  Opt-in: pass `LANES="… E-wrk"`.

| Scenario | Route | Tool | Threads | Conns | Duration | Keepalive | Diagnostic purpose |
|---|---|---|---|---|---|---|---|
| E1 | `/plaintext` | wrk + [`bench/wrk/no_keepalive.lua`](wrk/no_keepalive.lua) | 4 | 256 | 60 s | **off** (`Connection: close` per request) | Connection-lifecycle cost: isolates TLS handshake + accept-loop cost from steady-state work.  Comparing E1 against B1 attributes the connection-setup tax. |

Lane E exposes accept-loop + TLS-handshake costs that the
keep-alive-dominated Lane B hides.  Run against `*-cleartext` stacks
in addition to standalone-TLS to isolate the handshake cost from the
accept-loop cost (cleartext drops TLS but keeps the accept-per-request
cost).  See the Sprint 24 results section for first numbers.

## Metrics

Per scenario:

| Metric | Source | Notes |
|---|---|---|
| req/s (or msg/s)         | tool report                            | Throughput |
| mean / sd / max latency  | tool report                            | h2load and wrk only |
| P50 / P95 / P99 / P99.9  | k6 summary (`http_req_duration`)       | Latency distribution |
| Error rate               | tool report                            | Non-2xx + transport errors |
| Loop lag P99 (ms)        | BlackBull `/metrics`                   | BlackBull only — peers don't expose it |
| RSS peak (MB)            | `/usr/bin/time -v` on the server proc  | Memory ceiling under load |
| CPU%                     | `/usr/bin/time -v`                     | Whether the worker saturated its core |

Numbers reported as median of N runs (default N=3 for h2load and wrk;
N=1 for k6 since each run is 60s+). Sprint 24+ wrk lanes also report
**min..max** range and **MAD** (median absolute deviation) in a
trailing "noise (MAD)" column; rows where `MAD / median > 10 %` carry
a 🌫 marker — a visible flag that the number is in the noise band and
should not be cited for sub-10 % comparisons.

## Methodology

**Warmup.** Each server gets 5,000 `/ping` requests before the measured
run to amortize TLS session-cache warmup, dict resizing, and import-time
work. Warmup output is discarded.

**Server config.** One worker, one core. uvloop on where available
(BlackBull, hypercorn, uvicorn). Same TLS cert (`tests/cert.pem`) across
all five so handshake cost is identical. HTTP/2 flow-control windows on
BlackBull set to match hypercorn's h2 defaults (65535 / 65535) for
apples-to-apples, then re-measured with BlackBull's own defaults.

**Load gen.** Same load-gen binary across runs (h2load nghttp2, wrk
master, k6 stable). No client-side TLS session cache reuse between
scenarios.

**Isolation.** Servers brought up and torn down between stacks
(`compare.sh`'s `kill_existing` + `wait_ready`). 1s settle between runs.

**Environment recording.** Every report records: `uname -a`, CPU model,
Python version, server version, load-gen version, kernel TCP settings
(`somaxconn`, `tcp_tw_reuse`).

### Cascade-multiplier rule (gating optimisation sprints)

Microbench numbers translate to throughput numbers at a multiplier that
depends on the optimised path's share of py-spy samples on the target
lane (typically B1 or B2).  Sprint 25 measured ~2-3× (path was a
dominant slice); Sprint 26 measured ~0.21× (path had been reduced past
dominance).  Predict the multiplier from the share **before** committing
sprint scope:

| Profile share of optimised path (B1/B2 self-time + inclusive) | Expected multiplier | Decision rule |
|---|---|---|
| ≥ 30 % | 2-3× (cascade) | A ≥ 10 % per-call microbench reduction is worth a sprint |
| 10-30 % | ~1× | Microbench % ≈ throughput % |
| < 10 % | sub-cascade (~0.2-0.5×) | Don't pursue unless the change is trivial or buys something other than throughput (clarity, correctness) |

Capture the share with `bench/aws/profile_lanes.sh` (Sprint 27 — wrk-driven
py-spy on EC2 split topology, one speedscope JSON per lane).  The
"Left Heavy" view in speedscope.app reads off the share directly.

## Known limitations

| Limitation | Mitigation |
|---|---|
| WSL2 loopback hides real-NIC behaviour (no DMA, no driver, no syscall cost matching a real card) | Numbers are explicitly *relative*; absolute measurements on EC2 |
| **WSL2 noise floor on B2r (~-R 5000)**: BlackBull single-stack 5-run spread is 1.3-5.1 ms mean with no code change. Any optimisation < ~15 % is invisible. | Run ≥ 5 runs per stack and report the *median*. Do not cite a B2r improvement from any single A/B unless the medians differ by > 15 %. Sub-ms-precision work belongs on EC2. |
| Single-host load gen + server share CPU | k6 and the server pinned to different CPU sets where possible; or run load gen on a second EC2 instance |
| Different servers use different HTTP/2 libraries (h2 vs Rust impl in granian) | Documented per result; mux scaling differences are mostly library-driven |
| uvicorn and daphne can't run lane A | Marked as N/A in the matrix; cross-protocol comparison handled by lane C |
| `/echo` payload not standardized across HTTP/1.1 vs HTTP/2 lanes | Same 4 KiB body in both lanes; differences are protocol overhead, not body cost |


## Sprint history

Per-sprint findings, baselines-of-record, profile traces, and
methodology lessons are kept as internal development notes
**outside this public repository** (gitignored under
`bench/sprint-logs/`).  Each file is the historical record of one
sprint; this document stays current-state.

Why internal-only: those logs contain point-in-time numbers for
external reference servers (uvicorn / hypercorn / granian / daphne /
nginx) measured under tuning chosen for BlackBull's diagnostic
shape.  As internal bottleneck-attribution evidence they are
correct and load-bearing; detached from this document's framing,
the numbers misrepresent the peer projects.  The methodology and
lane definitions here are the public artifact; the per-sprint
bottleneck-attribution log is not.

Cross-sprint coverage:

- **Sprint 13**: WSL + EC2 baselines of record; WSL → EC2 delta;
  EC2 noise-floor characterisation.
- **Sprint 14**: Layer-attribution topologies (standalone vs
  nginx-fronted vs cleartext).
- **Sprint 15**: High-concurrency profile finding (Lane C2 codec
  dominance pre-HPACK-fastpath).
- **Sprint 16**: Multi-worker scaling finding (SO_REUSEPORT, w=N
  curves).
- **Sprint 20**: Split-topology re-measurement (separate server /
  loadgen instances).
- **Sprint 21 Phase B**: w=2 → w=4 scaling-cliff diagnosis;
  closed-form `R(W)` deployment-sizing formula.
- **Sprint 23**: `asyncio.timeouts.*` cost removed from per-request
  hot path.
- **Sprint 24**: Doc clarity, methodology (MAD noise column), 
  Lane E, HPACK extension.
- **Sprint 25**: `_parse` URL splitter + header-loop regex
  validators; multi-pair EC2 harness (`full_ab.sh`).
- **Sprint 26**: deadline subsystem rework — per-arm
  `loop.call_later` replaced by per-process tick scanner.
- **Sprint 27**: methodology pin — cascade-multiplier rule codified
  (`## Methodology`); per-lane py-spy harness
  (`bench/aws/profile_lanes.sh`); ZeroVer + CHANGELOG seed;
  HttpArena local-only environment scaffold (`bench/httparena/`);
  ASGI correctness fixes (H/2 `:path` split, Compression
  `Content-Length` rewrite) surfaced via HttpArena prep.  No
  optimisation Phase 2 — Phase 1 data placed the original Sprint 28
  candidate under deployment-posture conditions.
- **Sprint 28**: Early Alpha readiness — `KNOWN_LIMITATIONS.md`;
  1-hour soak harness (`bench/soak/`) confirming leak-free posture
  across 19.5 M requests on single- + 4-worker runs; EC2 c7i.xlarge
  HttpArena cross-check vs FastAPI; `StaticFiles` mtime/size-keyed
  in-memory cache; default error handler `BLACKBULL_ENV`-aware
  (DEV traceback, PROD terse); CLI `--version`.

## File layout (target)

```
bench/
  CHARACTERIZATION.md          # this file
  app.py                       # BlackBull bench target (exists)
  benchmark.sh                 # BlackBull-only orchestrator (exists)
  h2load_run.sh                # lane A driver (exists)
  k6/                          # lanes C, D (exists)
  wrk/                         # lane B driver (to add)
    ping.lua
    echo.lua
    run.sh
  peers/
    blackbull_app.py           # symlink / re-export of bench.app for parity (optional)
    uvicorn_app.py             # to add
    hypercorn_app.py / quart_app.py / starlette_app.py  # exists
    granian_app.py             # to add
    daphne_app.py              # to add
    compare.sh                 # to extend
    run_one.sh                 # to extend
  results/
    baseline_v0.md             # v0 WSL2 baseline (to add after implementation)
```

## Reading the matrix

A scenario produces actionable signal when at least three servers in
the same lane finish without errors.  A single outlier may be a tuning
issue or noise; a pattern (e.g., BlackBull shows lower overhead than
hypercorn on Lane A but higher overhead on Lane B) is real signal
that helps attribute cost to specific layers.  AWS re-measurement is
the final arbiter under the cross-topology warning at the top of this
document.

## Public benchmark reference

Scenarios B1 (plaintext no-pipeline), B2 (plaintext with pipelining),
and B3 (JSON) map onto **TechEmpower Plaintext and JSON** tests. Our
concurrency (256, 1024) is lower than TechEmpower's full sweep (256 → 16,384)
because WSL2 loopback can't sustain that range; on EC2 we'll widen it.

Scenarios B6 (echo 1 KiB) and B7 (echo 100 KiB) map onto **granian's
echo benchmark**. Granian uses `oha`; we run both `oha` and `wrk` so the
tool itself doesn't become a confounding variable.

The internal set (A5–A7, B4–B5, and Lane D) has no public counterpart —
it exists to inform BlackBull's own optimisation work.

References:
- TechEmpower Framework Benchmarks — <https://www.techempower.com/benchmarks/>
- Granian benchmarks — <https://github.com/emmett-framework/granian/tree/master/benchmarks>
- Vibora benchmarks — <https://github.com/vibora-io/benchmarks>
