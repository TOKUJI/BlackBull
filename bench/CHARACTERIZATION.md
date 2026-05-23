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

### WSL baseline of record (as of 2026-05-23)

The current BlackBull WSL2 baseline is
[`bench/results/compare_servers_20260523-014828.md`](results/compare_servers_20260523-014828.md),
taken at HEAD = `638f56c`.  Approximate single-run shape on the
reference WSL host:

| Lane | BlackBull req/s | Notes |
|---|---|---|
| A1 mux=1 (h2load) | 16.5–16.8 k | HTTP/2 |
| B1 plaintext c=256 (wrk) | 19.7–21.6 k | HTTP/1.1 |
| B3 json c=256 (wrk) | 22–24 k | HTTP/1.1 |
| B6 echo-1k (wrk) | 19.8–23.4 k | HTTP/1.1 |
| C2 500-VU (k6) | 12.1 k | p99 ~72 ms (host-noise-dependent) |
| D WebSocket avg RTT | 0.21 ms | k6 ws |

Runs in [`bench/results/baselines/pre_sprint9d/`](results/baselines/pre_sprint9d/)
**pre-date the Sprint 9c/9d hot-path landing (commit `4e28116`,
2026-05-22 13:47 JST)** and should not be used for comparisons
against current code — the post-9d B1/B3/B6 numbers are ~25–40 %
higher than the pre-9d baseline.  See the README in that directory.

### EC2 baseline of record (Sprint 13, 2026-05-23)

First off-WSL measurement pass.  Canonical AWS report:
[`bench/results/aws/20260523-095507Z/results/compare_servers_20260523-095508.md`](results/aws/20260523-095507Z/results/compare_servers_20260523-095508.md).
The earlier partial run (`20260523-064323Z/`) is kept for archaeology
only — it lacks oha (install bug) and nginx (omitted from STACKS).
The later run (`20260523-111726Z/`) is the targeted nginx Lane C re-run
that verified the `/ping` fix; for nginx C1/C2, prefer those rows over
the 095507Z values.

| Hardware  | Single `c7i.xlarge` in `us-east-1`, Ubuntu 24.04 LTS, kernel 6.17 |
|---|---|
| CPU       | Intel Xeon Platinum 8488C (Sapphire Rapids), 4 vCPU |
| Topology  | Loopback only — server + load gen on the same instance |
| Methodology | Identical to WSL: same lane matrix, `RUNS=5`, `DURATION=30s` |

Approximate single-run shape on EC2 (HEAD = `638f56c`):

| Lane | BlackBull req/s | Notes |
|---|---|---|
| A1 mux=1 (h2load) | ~10–11 k | HTTP/2 |
| B1 plaintext c=256 (wrk) | ~12.6–16.5 k | HTTP/1.1, 2 runs |
| B3 json c=256 (wrk) | ~11.4–13.5 k | HTTP/1.1 |
| B6 echo-1k (wrk) | ~12.6–13.2 k | HTTP/1.1 |
| C2 500-VU (k6) | ~6.6–7.2 k | p99 ~100 ms |
| D WebSocket avg RTT | ~0.35 ms | k6 ws |

### WSL → EC2 delta (Sprint 13 finding)

The off-WSL pass answered both questions the sprint was scoped to ask.

**Q1 — how distorted are WSL numbers?** Answer: the *ranking* is, at
the top end.  WSL loopback acts as a syscall-floor ceiling that caps
fast servers and leaves slow ones alone.

Lane B1 plaintext c=256 (single run from each environment, blackbull
HEAD = `638f56c`):

| Stack | WSL post-9d | EC2 (095507Z) | EC2 / WSL |
|---|---|---|---|
| blackbull | ~20.7 k | 16.5 k | **0.80×** |
| uvicorn   | ~22 k   | 30.4 k | 1.38× |
| hypercorn | ~5 k    | 5.8 k  | 1.16× |
| granian   | ~26 k   | 91.4 k | **3.52×** |
| daphne    | ~5 k    | 6.2 k  | 1.24× |
| nginx     | n/a (not in WSL baseline) | 103 k (B1 wrk) | — |

Pure-Python ASGI servers (BlackBull/hypercorn/daphne) move ±20 % on
EC2; the C/Rust peers (granian especially) gain 1.5–3.5×.  BlackBull
moves *down* on EC2.  Likely reason: pure-Python ASGI throughput is
clock-bound per request, and a c7i.xlarge vCPU runs at ~3.0 GHz base
(~3.8 GHz turbo) — lower than typical desktop single-core clocks where
the WSL host sits.  Granian/nginx are I/O-bound, not clock-bound, so
they win the better syscall path on real Linux.

**Q2 — apples-to-apples ranking?** On EC2 B1 the order is
**granian ≫ uvicorn > blackbull > daphne ≈ hypercorn**, with nginx
above all four as the static-file reference.  This contradicts the
WSL-only view that had BlackBull within 25 % of granian.

### EC2 noise floor (Sprint 13 finding)

Two full AWS passes on the same instance class, same scripts:

| Stack | B1 run-1 (064323Z) | B1 run-2 (095507Z) | run-to-run delta |
|---|---|---|---|
| blackbull | 12.6 k | 16.5 k | **+31 %** |
| uvicorn   | 28.3 k | 30.4 k | +8 % |
| hypercorn | 5.1 k  | 5.8 k  | +13 % |
| granian   | 87.5 k | 91.4 k | +4.5 % |
| daphne    | 5.6 k  | 6.2 k  | +11 % |

Median-of-5 inside `compare_servers.sh` did not tame the inter-run
variance — same instance class, different physical hosts (each
`up.sh` lands on a fresh instance).  Until a 3rd+ data point lands,
**treat the EC2 noise band as ~±15 %** for the slow-Python stacks
(BlackBull's spread was the worst at +31 %, partly because its
absolute numbers are smaller so each ms swing reads bigger).

Implication for future tuning A/Bs: a sub-15 % win on EC2 is below
the current noise floor and not citeable.  Either tighten the noise
(more medians, placement groups, BB-pinned CPU sets) or look for
≥20 % deltas.

### Sprint 14 — layer-attribution topologies

Sprint 14 augments the existing stack list with **topology variants**
that hold the server constant and vary the deployment shape.  The goal
is to attribute the HTTP/1.1 gap (BlackBull at ~½ uvicorn, ~⅙ granian)
to a specific layer of the ASGI stack — TLS termination, accept/conn
management, HTTP/1.1 parsing, or ASGI dispatch — by **configuration
only** (no BlackBull code changes).

Stack-name suffix convention (recognised by
[`bench/peers/run_peer.sh`](peers/run_peer.sh) and
[`bench/peers/compare_servers.sh`](peers/compare_servers.sh)):

| Suffix | Topology | Client target | What `T1 − Tn` isolates |
|---|---|---|---|
| (none) | **T1** — standalone HTTPS (Sprint 13 default) | `https://localhost:8443` | (baseline) |
| `-cleartext` | **T2** — standalone HTTP, no TLS | `http://localhost:8443` | TLS termination cost |
| `-nginx` | **T3** — nginx HTTPS frontend + cleartext HTTP upstream | `https://localhost:8443` (nginx) | TLS+accept-loop offload (upstream on `:8444`) |
| `-h11` (uvicorn only) | **T1** with `--http h11` instead of `--http auto` | `https://localhost:8443` | C-parser advantage (httptools vs h11) |

Topology variants are defined for **blackbull, uvicorn, granian only**;
hypercorn/daphne already trail the matrix in Sprint 13 and the A/B
would not be informative for them.

T3 uses [`bench/peers/nginx_proxy.conf`](peers/nginx_proxy.conf) (new
in Sprint 14) — a pure reverse-proxy config with upstream keepalive
(`keepalive 256` + `Connection ""`) so we measure steady-state request
cost rather than connection-establishment overhead.

Sprint 14 lanes run **B-wrk only** (B1, B3, B4, B6); Lane A doesn't
apply (T3 upstream is HTTP/1.1 by construction).  Standard invocation:

```bash
STACKS="blackbull blackbull-cleartext blackbull-nginx \
        uvicorn uvicorn-h11 uvicorn-cleartext uvicorn-nginx \
        granian granian-cleartext granian-nginx" \
  LANES="B-wrk" \
  bash bench/aws/run.sh
```

### Sprint 15 — high-concurrency profile finding

py-spy flame graphs captured on a fresh c7i.xlarge under k6 stress
load via [`bench/profile_under_load.sh`](profile_under_load.sh)
(BlackBull single-worker, HTTPS, HTTP/2 via ALPN — k6 default).
Two passes at K6_VUS=200 and K6_VUS=500.  Profile artefacts:
`bench/results/aws/20260523-162617Z/profile/`.

| K6_VUS | req/s | p50 | p99 | event-loop lag p50 / p99 |
|---|---|---|---|---|
| 200 | 6 665 | 28.4 ms | 46.6 ms | 13 ms / 30 ms |
| 500 | 5 730 (−14 %) | 80.5 ms | 133.9 ms | 31 ms / 81 ms |

**Finding 1 — the "C2 widening" against uvicorn is a protocol
mismatch, not a BlackBull bug.**  k6 negotiates HTTP/2 against
BlackBull (which supports ALPN h2); uvicorn does not support HTTP/2
at all, so k6 falls back to HTTP/1.1 there.  The Sprint 13 ratio
"BlackBull→uvicorn gap widens from 1.8× on B1 to 2.4× on C2" was
comparing BlackBull-on-HTTP/2 against uvicorn-on-HTTP/1.1.  The C2
row now records the actual negotiated protocol in the `proto`
column so this can't be misread again.

**Finding 2 — the HTTP/2 hot path is dominated by pure-Python hpack
+ our own sender.**  Top inclusive-sample frames at both VU levels:

| Frame | vu200 samples | vu500 samples |
|---|---|---|
| `hpack/hpack.py:encode` | 902 | 901 |
| `hpack/hpack.py:decode` | 738 | 731 |
| `blackbull/server/sender.py:__call__` | 739 | 722 |
| `blackbull/server/http2_actor.py:_on_headers_frame` | 642 | 730 |
| `hpack` add / decode_indexed / struct.new combined | ~1 300 | ~1 430 |

hpack encode + decode together account for roughly a third of the
busy frames at both VU levels.  The `hpack` package is third-party
pure-Python; we don't control the codec directly.

**Finding 3 — BlackBull's HTTP/2 stack saturates at ~500 VU but is
still the fastest pure-Python HTTP/2 ASGI server measured.**
Throughput drops 14 % from 200→500 VU and event-loop lag rises 2.4×.
For comparison, Sprint 13's Lane C2 single-worker numbers were:

| Stack | Lane C2 req/s | proto | ratio to BlackBull |
|---|---|---|---|
| BlackBull | 7 179 | HTTP/2 | 1.00× |
| uvicorn   | 17 331 | HTTP/1.1 | 2.41× (different protocol) |
| granian   | 26 403 | HTTP/2 | 3.68× (Rust core) |
| hypercorn |  2 433 | HTTP/2 | 0.34× |
| daphne    |  2 320 | HTTP/1.1 | 0.32× |

Among pure-Python HTTP/2 ASGI servers (just BlackBull and hypercorn
in this matrix), BlackBull leads by ~3×.

**No tuning sprint queued from this finding.**  The dominant cost is
in the third-party `hpack` codec; closing it would mean replacing or
working around `hpack`, which goes beyond the configuration-only
scope this measurement work has stayed in.  Tracked separately if a
real user need appears (analogous to the `[speed-h1]` story in
README §P4).

#### Methodology caveat — access logging contamination
The first Sprint 15 profile (run-log under `20260523-162617Z/`) ran
with `BB_ACCESS_LOG=1` because [`bench/profile_under_load.sh`](profile_under_load.sh)
launches `bench/app.py` directly rather than going through
[`bench/peers/run_peer.sh`](peers/run_peer.sh) (which sets
`BB_ACCESS_LOG=0` for benchmark parity).  The flame graph contains a
~400–800-sample contribution from the async-logging `QueueListener`
thread that doesn't appear in the compare_servers reports.
Subsequent runs of `bench/profile_under_load.sh` set
`BB_ACCESS_LOG=0` by default; the conclusions above stand because
the contamination touches a background thread, not the event loop.

### Sprint 16 — multi-worker scaling finding

Single-worker BlackBull was the entire Sprint 13/14/15 baseline.
Sprint 16 measured **BlackBull with `BB_WORKERS=1/2/4` on the same
c7i.xlarge (4 vCPU)** to characterise production-shape scaling.
**BlackBull-only — no peer comparison in this matrix.**

Stack-name convention: `blackbull-w<N>` (see
[`bench/peers/run_peer.sh`](peers/run_peer.sh) suffix parser).
Canonical artefact:
[`bench/results/aws/20260523-163317Z/results/compare_servers_20260523-163319.md`](results/aws/20260523-163317Z/results/compare_servers_20260523-163319.md).

#### Throughput at w=1 / w=2 / w=4

| Lane | w1 req/s | w2 req/s | w4 req/s | w2/w1 | w4/w1 | w4/w2 |
|---|---|---|---|---|---|---|
| B1 plaintext c=256 (HTTP/1.1) | 15 636 | 31 606 | 37 092 | **2.02×** | 2.37× | 1.17× |
| B3 json c=256       (HTTP/1.1) | 14 639 | 30 454 | 36 384 | 2.08× | 2.49× | 1.19× |
| B4 16 KB c=100      (HTTP/1.1) | 14 767 | 27 376 | 32 755 | 1.85× | 2.22× | 1.20× |
| B6 echo-1k c=100    (HTTP/1.1) | 13 918 | 26 599 | 31 253 | 1.91× | 2.25× | 1.18× |
| C2 500-VU           (HTTP/2)   |  8 784 | 14 441 | 14 775 | **1.64×** | **1.68×** | **1.02×** |

#### p99 latency at w=1 / w=2 / w=4

| Lane | w1 p99 | w2 p99 | w4 p99 |
|---|---|---|---|
| B1 (HTTP/1.1) | 30.0 ms | 15.6 ms | 13.7 ms |
| C2 (HTTP/2)   | 76.6 ms | 75.1 ms | 86.4 ms |

#### Findings

1. **HTTP/1.1 scales near-linearly from w=1 to w=2.**  Every Lane B
   scenario hits ~2× throughput at w=2 vs w=1.  B1 lands at 2.02×,
   B3 at 2.08× — within the EC2 noise band of ideal.

2. **HTTP/1.1 saturates between w=2 and w=4.**  w=4 only adds ~17 %
   over w=2 for HTTP/1.1.  This is partly real saturation and partly
   a single-instance-loopback artefact: load gen (wrk) shares the
   4 vCPUs with the server, so at w=4 the box is genuinely
   over-subscribed.

3. **HTTP/2 (Lane C) saturates earlier and worse.**  w=2 buys 1.64×
   throughput; w=4 buys *essentially nothing* (1.02× over w=2).
   Consistent with the Sprint 15 finding that HTTP/2 in BlackBull
   is CPU-bound on pure-Python `hpack` — workers hit the per-worker
   GIL ceiling sooner.  C2 p99 at w=4 (86 ms) is **higher** than
   w=2 (75 ms) — CPU contention with k6 dominates.

4. **More workers cut HTTP/1.1 tail latency by half.**  B1 p99
   drops from 30 ms (w=1) to 14 ms (w=4) at the same client-side
   concurrency (c=256).  Per-worker queueing shrinks as work is
   distributed.

5. **Headline production data point.**  At w=2 BlackBull's
   HTTP/1.1 throughput (31.6 k req/s B1) is in the same range as
   Sprint 13's single-worker uvicorn (30.4 k req/s, recorded on
   the same instance class with the same script).  No comparative
   claim — just a useful sizing reference.

#### Deployment-sizing implication

On a c7i.xlarge (4 vCPU) with load gen on the same box:
- **`BB_WORKERS=2` is the sweet spot for HTTP/1.1**: 2× throughput
  and half the p99 latency vs w=1.
- **`BB_WORKERS=4` adds little throughput on HTTP/1.1** and nothing
  on HTTP/2; it does keep tail latency low under load.
- The natural rule-of-thumb is **`BB_WORKERS = N_vCPU / 2`** when the
  load generator shares the box.  A two-instance topology (load gen
  separate) would probably let w=4 scale further; we don't have
  that data.

#### Methodology caveat — Lane A was silently skipped
The Sprint 16 run requested `LANES="A B-wrk C D"` but Lane A
(h2load HTTP/2) was skipped for all three w-variants.  Cause: the
`SUPPORTS_H2` capability check at
[`bench/peers/compare_servers.sh`](peers/compare_servers.sh) did not
strip the `-w<N>` suffix before matching against the base list, so
`blackbull-w2` didn't match `blackbull` and Lane A was bypassed.
A `strip_worker_suffix()` helper now sits at the same call site so
future Sprint-16-shape runs include Lane A.  The Lane C2 column
above already covers the HTTP/2 multi-worker story; no re-run is
queued just to backfill Lane A.

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
