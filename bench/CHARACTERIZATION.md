# BlackBull characterization plan

A fixed set of scenarios, metrics, and methodology for measuring BlackBull
and comparing it against four peer ASGI servers.

The matrix is the contract: every release should be measurable against it,
and every comparison run produces the same shape of table.

## Goals

1. **Self-characterization.** Know how BlackBull behaves across protocol,
   payload size, and load. Today there are scattered numbers in
   `wise-conjuring-sundae.md` and ad-hoc `bench/results/` files; no single
   document defines the regression baseline.
2. **Peer comparison.** Same ASGI app on five servers, same load generator,
   same hardware. Numbers are *relative* — the absolute values depend on
   the host and matter mostly when reproduced on identical hardware.
3. **AWS-ready.** Methodology is stable enough that re-running on EC2
   produces directly comparable numbers (only the host changes).

## Comparison matrix

Five servers. Not all speak HTTP/2; the matrix splits by protocol.

| Server     | Version pinned in | HTTP/1.1 | HTTP/2 | WebSocket | Notes |
|------------|---|---|---|---|---|
| BlackBull  | repo head        | ✅ | ✅ | ✅ | Subject under test |
| uvicorn    | `bench/peers/`   | ✅ | ❌ | ✅ | Industry default; pure-Python via h11/wsproto |
| hypercorn  | `bench/peers/`   | ✅ | ✅ | ✅ | Direct peer — also h2 library based |
| granian    | `bench/peers/`   | ✅ | ✅ | ✅ | Rust-backed; shows the ceiling of this niche |
| daphne     | `bench/peers/`   | ✅ | ❌ | ✅ | Django reference impl; slow but canonical |

ASGI app is identical across all five — see *Test target* below.

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

### Lane A — HTTP/2 multiplexing (h2load)

Applies to: BlackBull, hypercorn, granian.

| Scenario | Route        | -n (requests) | -c (conns) | -m (streams/conn) | What it stresses |
|---|---|---|---|---|---|
| A1 | `/plaintext` | 50,000 | 50 | 1   | Per-request overhead w/o mux (TechEmpower-comparable) |
| A2 | `/plaintext` | 90,000 | 50 | 10  | Browser-realistic mux |
| A3 | `/plaintext` | 90,000 | 50 | 50  | Heavy mux |
| A4 | `/json`      | 50,000 | 50 | 10  | Tiny JSON over HTTP/2 |
| A5 | `/16kb`      | 50,000 | 50 | 10  | Medium body throughput |
| A6 | `/64kb`      | 30,000 | 50 | 10  | Large body, single frame |
| A7 | `/1mb`       |  3,000 | 20 |  3  | Flow-control window |

### Lane B — HTTP/1.1 keep-alive (wrk + oha)

Applies to: all five servers. Two tools run side-by-side: **wrk** (with Lua
script for pipelining + POST bodies, TechEmpower-style) and **oha** (single
binary, granian-style). Each tool measures independently — the spread
between them is itself a data point.

| Scenario | Route        | Threads | Conns | Duration | Pipeline | What it stresses |
|---|---|---|---|---|---|---|
| B1 | `/plaintext` | 4       | 256   | 30s      | none     | TechEmpower-style baseline, comparable to public numbers |
| B2 | `/plaintext` | 4       | 1024  | 30s      | 16       | TechEmpower-style with pipelining — extreme per-request overhead |
| B3 | `/json`      | 4       | 256   | 30s      | none     | Tiny-JSON throughput, comparable to TechEmpower JSON |
| B4 | `/16kb`      | 4       | 100   | 30s      | none     | Body throughput (internal) |
| B5 | `/64kb`      | 4       |  50   | 30s      | none     | Large response (internal) |
| B6 | `/echo`      | 4       | 100   | 30s      | none     | POST 1 KiB body (granian-comparable) |
| B7 | `/echo`      | 4       |  50   | 30s      | none     | POST 100 KiB body (granian-comparable) |

### Lane C — k6 stress (VU-based latency distribution)

Applies to: all five servers. Each VU holds one persistent connection.
HTTP/2-capable servers negotiate via ALPN; others fall back to HTTP/1.1.
The `proto` field in the summary records which actually ran.

| Scenario | Route | VUs | Duration | What it stresses |
|---|---|---|---|---|
| C1 | `/ping`  | 200 | 60s | Rampup-realistic load |
| C2 | `/ping`  | 500 | 60s | Saturation behaviour |

### Lane D — WebSocket RTT (k6 ws)

Applies to: all five servers.

| Scenario | Route | Conns | Message rate | Duration |
|---|---|---|---|---|
| D1 | `/ws` | 50 | 5 msg/s/conn | 60s |

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
N=1 for k6 since each run is 60s+). Spread > 10% across runs is flagged.

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

## Known limitations

| Limitation | Mitigation |
|---|---|
| WSL2 loopback hides real-NIC behaviour (no DMA, no driver, no syscall cost matching a real card) | Numbers are explicitly *relative*; absolute measurements on EC2 |
| **WSL2 noise floor on B2r (~-R 5000)**: BlackBull single-stack 5-run spread is 1.3-5.1 ms mean with no code change. Any optimisation < ~15 % is invisible. | Run ≥ 5 runs per stack and report the *median*. Do not claim a B2r win from any single A/B unless the medians differ by > 15 %. Sub-ms-precision work belongs on EC2. |
| Single-host load gen + server share CPU | k6 and the server pinned to different CPU sets where possible; or run load gen on a second EC2 instance |
| Different servers use different HTTP/2 libraries (h2 vs Rust impl in granian) | Documented per result; mux scaling differences are mostly library-driven |
| uvicorn and daphne can't run lane A | Marked as N/A in the matrix; cross-protocol comparison handled by lane C |
| `/echo` payload not standardized across HTTP/1.1 vs HTTP/2 lanes | Same 4 KiB body in both lanes; differences are protocol overhead, not body cost |

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

A scenario is meaningful when at least three servers in the same lane
finish without errors. A single outlier may be a tuning issue; a pattern
(e.g., BlackBull beats hypercorn on lane A but loses on lane B) is real
signal. AWS re-measurement is the final arbiter.

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
