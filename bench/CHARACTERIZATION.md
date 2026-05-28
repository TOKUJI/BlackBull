# BlackBull characterization plan

A fixed set of scenarios, metrics, and methodology for measuring BlackBull
and comparing it against four peer ASGI servers.

The matrix is the contract: every release should be measurable against it,
and every comparison run produces the same shape of table.

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

Peak req/s comes after all five.  Benchmark comparisons against
granian (Rust) and nginx (static-file server, no ASGI dispatch) are
reference floors / ceilings, not peer targets.

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
| A3 | `/plaintext` | 90,000 | 50 | 50  | Heavy mux — stress, not browser-realistic (real browsers cap at ~10 concurrent streams; kept to characterise the multiplexing ceiling) |
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
| B2 | `/plaintext` | 4       | 1024  | 30s      | 16       | TechEmpower-style with pipelining — synthetic upper bound (HTTP/1.1 pipelining is effectively dead in modern browsers; kept for cross-board comparability) |
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

### Lane E — Connection churn (wrk no-keepalive) — Sprint 24

Applies to: all five servers.  Opt-in: pass `LANES="… E-wrk"`.

| Scenario | Route | Tool | Threads | Conns | Duration | Keepalive | What it stresses |
|---|---|---|---|---|---|---|---|
| E1 | `/plaintext` | wrk + [`bench/wrk/no_keepalive.lua`](wrk/no_keepalive.lua) | 4 | 256 | 60 s | **off** (`Connection: close` per request) | TLS handshake + accept-loop cost |

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

_Status: superseded by Sprint 20 for HTTP/2 multi-worker numbers; still the
baseline-of-record for HTTP/1.1 single-host topology._

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

_Status: confirmed.  The T1/T2/T3/T1-h11 topology suffix convention is the
current methodology for "which layer is paying the cost"._

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

_Status: confirmed (hpack-codec ceiling finding).  The static-table half of
the codec cost was subsequently closed by the Sprint 21 Phase C HPACK
fastpath ([`blackbull/protocol/hpack_fastpath.py`](../blackbull/protocol/hpack_fastpath.py));
the dynamic-table half remains the limiter._

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

_Status: partially superseded by Sprint 20.  The HTTP/2 conclusion
("saturates at w=2") was a single-host load-gen artefact; the HTTP/1.1
scaling story stands.  Read this section only with Sprint 20's correction
section in mind._

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

### Sprint 20 — Split-topology re-measurement

_Status: current understanding for HTTP/2 multi-worker scaling.  Supersedes
Sprint 16's HTTP/2 conclusions._

Sprints 13–16 all ran the load generator on the same `c7i.xlarge`
as the server.  Sprint 16's HTTP/2 finding (#3 above: "w=4 buys
*essentially nothing* (1.02× over w=2)") explicitly called this
out as a possible confounder.  Sprint 20 closes it by adding a
**second EC2 instance dedicated to load generation** — see
[bench/aws/README.md](aws/README.md) for the harness changes
(`TOPO=split`).

Topology (`TOPO=split`):

- **Server**: `c7i.xlarge` (4 vCPU) — same as Sprints 13–16.
- **Load generator**: `c7i.2xlarge` (8 vCPU) — chosen so the
  generator is never the bottleneck.
- Both instances in the same AZ, **same cluster placement group**
  (low-latency intra-rack), talking over VPC private IPs.
- TLS path unchanged: the server's `tests/cert.pem` is regenerated
  at install time with `bench-server.internal` + the server's VPC
  private IP as SANs; both instances install the cert into the CA
  store and put a matching `/etc/hosts` entry, so every load tool
  (h2load / wrk / oha / k6) keeps verifying TLS without
  `--insecure`.

Canonical artefact:
[`bench/results/aws/20260527-135137Z/`](results/aws/20260527-135137Z/)
— full Lanes A + B-wrk + B-oha + C + D pass.  (Two earlier partial
artefacts split across Lanes A+B and Lanes C+D were superseded by
this complete re-run and deleted to keep `bench/results/aws/`
canonical; numbers in the tables below are from that re-run.  The
qualitative finding holds.  Sprint-21-era footnote: the
`BASE` env-var wiring through to `bench/k6/*.js` was the bug that
necessitated the re-run.)

#### Headline — HTTP/2 scaling was masked by load-gen contention

Sprint 16 single-host vs Sprint 20 split, BlackBull Lane C2
(HTTP/2, 500 VU):

| w | Sprint 16 single (req/s) | Sprint 20 split (req/s) | Δ |
|---|---|---|---|
| w=1 | 8 784 | 8 785 | +0.0 % (within noise; single-worker is not CPU-saturated) |
| w=2 | 14 441 | 19 213 | **+33 %** |
| w=4 | 14 775 | 23 184 | **+57 %** |

In-topology scaling ratios:

| Ratio | Sprint 16 | Sprint 20 | What it means |
|---|---|---|---|
| w=2 / w=1 | 1.64× | **2.19×** | Sprint 16's w=2 was already load-gen-bottlenecked. |
| w=4 / w=2 | 1.02× | **1.21×** | Sprint 16's "saturation at w=2" was load-gen saturation, *not* server saturation. |
| w=4 / w=1 | 1.68× | **2.64×** | The actual HTTP/2 multi-worker headroom on BlackBull. |

**The Sprint 16 finding #3 ("HTTP/2 saturates at w=2") was
incorrect** — it was an artefact of the single-host topology, not
a property of BlackBull's HTTP/2 stack.  With a dedicated
loadgen, HTTP/2 worker scaling looks like a noisier version of
HTTP/1.1's, not a hard wall at w=2.

#### HTTP/1.1 (Lane B1) — basically unchanged

| w | Sprint 16 single (req/s) | Sprint 20 split (req/s) | Δ |
|---|---|---|---|
| w=1 | 15 636 | 14 822 | −5.2 % |
| w=2 | 31 606 | 30 804 | −2.5 % |
| w=4 | 37 092 | 37 489 | +1.1 % |

HTTP/1.1 is within ±5 % across the entire `w` ladder — the
Sprint 16 HTTP/1.1 numbers were *not* load-gen-confounded.
The +17 % at w=4 over w=2 from Sprint 16 reproduces here as
+21.7 %.  Sprint 16's HTTP/1.1 scaling story stands; only the
HTTP/2 conclusion needed correction.

#### Lane A (HTTP/2, single-worker) — VPC RTT delta

Lane A is multiplexed but single-worker by construction (the
`blackbull` row, no `-w<N>` suffix).  Comparing Sprint 13 single
vs Sprint 20 split for BlackBull-w1:

| Scenario | Sprint 13 (req/s) | Sprint 20 (req/s) | Δ |
|---|---|---|---|
| A1 mux1 | 10 638 | 10 462 | −1.7 % |
| A2 mux10 | 11 256 | 10 805 | −4.0 % |
| A3 mux50 | 13 342 | 11 627 | **−12.9 %** |
| A4 json mux10 | 12 124 | 10 826 | −10.7 % |
| A5 16 KB mux10 | 10 830 | 9 420 | −13.0 % |
| A6 64 KB mux10 | 7 501 | 6 609 | −11.9 % |
| A7 1 MB mux3 | 1 315 | 1 084 | −17.6 % |

This is the **VPC-overhead floor**: ~150–300 µs of network RTT
plus per-byte EC2 ENA overhead vs kernel loopback.  Larger
payloads (A5–A7) regress more because the per-byte cost
accumulates.  Single-worker Lane A is latency-bound, not CPU-bound,
so there is no contention to remove that would offset this floor.

#### Updated deployment-sizing implication

The Sprint 16 rule-of-thumb ("`BB_WORKERS = N_vCPU / 2` when load
gen shares the box") was correct *for the topology it was measured
on*.  On topology where the server has the CPU to itself:

- **`BB_WORKERS = N_vCPU`** is the natural ceiling for HTTP/2 —
  w=4 on 4 vCPU lands +21 % over w=2 (the Sprint 16 single-host
  +17 % HTTP/1.1 finding generalises to HTTP/2 once the loadgen
  is offloaded).
- For HTTP/1.1 the previous rule still applies: w=2 captures
  most of the win, w=4 adds modest tail-latency improvement.
- The Sprint 15 finding stands — BlackBull's HTTP/2 hot path is
  still `hpack`-dominated; Sprint 20 just shows there's more
  CPU headroom than Sprint 16 could measure.

#### Comparability across topologies

Single-host (Sprints 13–16) and split (Sprint 20) numbers are
**not** directly comparable for absolute latency.  Two specific
calls when reading older artefacts:

- Add the per-request VPC floor (~150–300 µs depending on
  payload) to any single-host A-lane / B-lane absolute-latency
  number before mentally comparing it to split-topology data.
- Throughput-bound rows (Lane C high-VU, multi-worker Lane B)
  are the cleanest cross-topology comparison once that floor is
  amortised over enough requests.

### Sprint 21 Phase B — w=2 → w=4 scaling cliff diagnosis

_Status: current understanding for the w=2→w=4 scaling cliff (Nitro ENA
softirq concentration on vCPU 0 + per-worker scheduler overhead).  The
`asyncio.timeouts.*` half of the finding was closed in Sprint 23; the
deployment-sizing formula in this section is still the current rule-of-thumb._

The Sprint 20 canonical artefact showed only ~1.10–1.21 × scaling
from w=2 to w=4 across both HTTP/1.1 (B1) and HTTP/2 (A1, C2).
An external audit framed this as "scaling collapses past w2 —
likely a BlackBull-specific defect."  Phase B was a cheap
pinned/free-scheduled experiment + a py-spy/mpstat capture to
falsify or confirm.

#### Setup

Same `TOPO=split` topology as Sprint 20.  Server CPU topology
(captured by [bench/aws/install.sh](aws/install.sh) into
[`sprint21-phaseB/server-cpu-topology.txt`](results/aws/sprint21-phaseB/server-cpu-topology.txt)):
c7i.xlarge is 2 physical cores × 2 SMT threads — vCPUs 0,2 are
the SMT siblings of core 0; vCPUs 1,3 of core 1.  Pinning was
done with the new `BB_BENCH_TASKSET` env-var seam through
[bench/peers/server_lifecycle_remote.sh](peers/server_lifecycle_remote.sh).

#### Result

Three pinning configs, BlackBull `blackbull-w2` / `-w4` only,
Lane B-wrk + Lane C, default duration:

| Config | B1 req/s | C1 req/s | C2 req/s |
|---|---|---|---|
| w=2 free (Sprint 20 reference) | 30 848 | 19 628 | 19 246 |
| w=2 pinned to **distinct cores** (`taskset -c 0,1`) | 29 386 | 19 397 | 18 821 |
| w=2 pinned to **SMT siblings** (`taskset -c 0,2`) | 29 314 | 19 320 | 18 868 |
| w=4 free (Sprint 20 reference) | 37 297 | 23 231 | 22 926 |
| w=4 free (re-measure during Phase B) | 35 895 | 22 684 | 22 501 |

Two surprises in one table:

1. **w=2 distinct-cores ≈ w=2 SMT-siblings ≈ 29 350 req/s** on B1
   (within 0.2 %).  If each worker were CPU-saturating its
   thread, sharing a physical core via SMT should cost 30–50 %.
   It doesn't.  Each BlackBull worker is therefore **well below
   1 vCPU's worth of CPU work**.
2. **w=4 free (35 895) > w=2 pinned-distinct (29 386) by +22 %**.
   So adding two more workers still buys throughput even though
   we've already used both physical cores at w=2-pinned.  Adding
   workers on SMT siblings (effectively the w=4 case) helps.

The SMT-ceiling hypothesis is falsified: it predicts (a) w=2
SMT-siblings ≪ w=2 distinct-cores (we measured them equal) and
(b) w=4 free ≈ w=2 distinct-cores (we measured +22 %).  Neither
holds.

#### Where the cycles go

Two captures during a B1-shaped 45 s `wrk -t8 -c256` saturation
(see [`sprint21-phaseB/`](results/aws/sprint21-phaseB/)):

`mpstat -P ALL 3 15` per-CPU averages (steady-state window):

| vCPU | %usr | %sys | %soft | %idle |
|---|---|---|---|---|
| 0 | 44 % | 47 % | 4 % | 6 % |
| 1 | 88 % | 4 % | 3 % | 6 % |
| 2 | 87 % | 4 % | 3 % | 6 % |
| 3 | 87 % | 4 % | 4 % | 6 % |

**vCPU 0 is spending ~half its time in the kernel** (irq + soft).
On Nitro c7i.xlarge the ENA driver pins all RX/TX softirq to
`cpu0` by default — the other three vCPUs see ~4 % kernel time.
Effective Python capacity is `0.44 + 3 × 0.88 = 3.08` vCPU on a
4-vCPU box, not 4.0.  That accounts for most of the
"diminishing return" past w=2.

`py-spy record --rate 200 --duration 30` on worker pid 7048
(see [`sprint21-phaseB/worker_7048.speedscope.json`](results/aws/sprint21-phaseB/worker_7048.speedscope.json)),
top self-time frames:

| self % | function (path) |
|---|---|
| 11.5 % | `run` ([asyncio/runners.py]) — event-loop overhead |
| 9.6 % | `reschedule` / `timeout` / `__aexit__` ([asyncio/timeouts.py]) |
| 7.2 % | `read` + `write` ([python3.12/ssl.py]) — TLS |
| 5.4 % | `write` ([asyncio/streams.py]) |
| 5.4 % | `_send_static` ([bench/peers/asgi_app.py]) — app response |
| 4.9 % | `run` + `_parse` family ([blackbull/server/http1_actor.py]) |

`asyncio.timeouts.*` machinery is **~9.6 % of per-worker CPU**.
That comes from the stacked `async with asyncio.timeout(...)`
contexts around header / body / idle waits (Sprint 17 added
`BB_BODY_TIMEOUT`; the per-request reschedule cost accumulates).
This overhead is per-request and does not amortise across
workers, so it shows up as a per-worker tax that limits the
linear-scaling regime.

#### Conclusion

The cliff is **the sum of**:

1. **Network-side asymmetry** — AWS Nitro ENA softirq concentrated
   on vCPU 0 (~0.5 vCPU of effective capacity lost on a 4 vCPU
   instance).  Not a BlackBull issue.
2. **Per-worker scheduler overhead** — ~10 % of each worker's
   CPU is asyncio-timeout machinery, which doesn't scale away
   with more workers.  Tangible but not a defect: the timeouts
   are load-bearing (RFC-9112 §9.3 idle, slowloris vector closed
   by Sprint 17 `BB_BODY_TIMEOUT`).

No BlackBull-internal serialization point was found: the
profile shows clean per-request work, no shared lock contention,
no single-worker accept funnel.  Phase B closes **without a
code fix**.

Two follow-on observations worth keeping (out of Sprint 21
scope):

- Future deep-perf work targeting the asyncio-timeout cost
  would compound across **all** lanes and workers — it's the
  highest-leverage non-parser optimisation on the profile.  But
  it touches load-bearing protection code (slowloris) and
  needs care.
- Multi-queue ENA configuration (RPS/RFS tuning) would spread
  the softirq across vCPUs and partially recover the lost 0.5
  vCPU.  Pure ops, not a server-code change.

#### Deployment-sizing formula

Direct corollary of the Phase B data — a closed-form prediction
for `R(W)` on Intel Sapphire Rapids (c7i family) with off-box
loadgen.  Validated against an out-of-sample c7i.4xlarge run
(see next subsection).

```
R(W) ≈ R_unit × κ(W; N_cores, N_vcpu)

with
  κ(W) = W                                            if  W ≤ N_cores
       = N_cores × (1 + s × (W − N_cores)/N_cores)    if  N_cores < W ≤ N_vcpu

  R_unit:   single-worker, dedicated-machine rate
            ≈ 15 000 req/s for Lane B1  (HTTP/1.1 plaintext)
            ≈  9 300 req/s for Lane C2  (HTTP/2 high-concurrency)
  s = 0.21  SMT scaling factor — what each extra SMT-sibling
            worker buys beyond N_cores
  N_cores   physical cores; N_vcpu = 2 × N_cores on SMT-enabled
            hardware
```

Rule-of-thumb for deployments: `BB_WORKERS = N_vcpu` whenever
the loadgen / clients are off-box; the gain from N_cores → N_vcpu
is the +21 % SMT bonus.  `W > N_vcpu` adds context-switch tax
without throughput gain.

Caveats baked into the formula:

- `R_unit` includes the ~10 % per-worker `asyncio.timeout` cost
  in the py-spy profile; if that overhead were cut, `R_unit`
  rises proportionally for **every** W.
- `s = 0.21` is workload-dependent.  Lane C2 (hpack-heavy) shows
  closer to `s ≈ 0.19` at W = N_vcpu — execution-unit contention
  on the SMT pair eats more of the bonus.
- ENA softirq spread on larger Nitro instances isn't modelled
  explicitly; the formula is slightly conservative for c7i.2xlarge+
  because more RX queues mean less softirq concentrated on a
  single vCPU.

#### Phase B formula validation — c7i.4xlarge out-of-sample

To confirm the formula generalises beyond the c7i.xlarge box it
was fitted on, an out-of-sample run was done on c7i.4xlarge
(8 physical cores × 2 SMT = 16 vCPU, server-side; c7i.4xlarge
loadgen).  Canonical artefact:
[`bench/results/aws/sprint21-phaseB-validation/`](results/aws/sprint21-phaseB-validation/).

Predicted vs measured:

| W | Lane B1 predicted (req/s) | measured | Δ |
|---|---|---|---|
| 4 (W < N_cores) | 60 000 | 60 918 | **+1.5 %** |
| 8 (W = N_cores) | 120 000 | 120 225 | **+0.2 %** |
| 16 (W = N_vcpu) | 145 200 | 144 840 | **−0.2 %** |

| W | Lane C2 predicted (req/s) | measured | Δ |
|---|---|---|---|
| 4 | 37 200 | 36 703 | −1.3 % |
| 8 | 74 400 | 75 191 | +1.1 % |
| 16 | 90 024 | 85 558 | −5.0 % |

HTTP/1.1 is within ±1.5 % at every W — noise band.  HTTP/2 is
within 5 %, with the small w=16 negative bias consistent with the
"hpack execution-unit contention on SMT siblings" caveat above.
The formula generalises.

### Audit recommendations already in place

Defensive against future external reviews flagging the same items as
gaps.  Each row links to the file:line evidence that the recommendation
is already wired up.

| Recommendation | Where it lives | Closing sprint |
|---|---|---|
| HPACK static-header fastpath (precomputed `:status` bytes, etc.) | [`blackbull/protocol/hpack_fastpath.py`](../blackbull/protocol/hpack_fastpath.py); wire-equivalence tests at [`tests/conformance/http2/test_hpack_fastpath.py`](../tests/conformance/http2/test_hpack_fastpath.py) | Sprint 21 Phase C |
| TLS write coalescing on the HTTP/1.1 hot path (status line + headers + body emitted as a single buffer) | [`blackbull/server/sender.py:307`](../blackbull/server/sender.py#L307) | Sprint 9d |
| HTTP/2 fast-path coalescing (HEADERS + DATA emitted as one buffer when the body fits in one frame) | [`blackbull/server/sender.py:462`](../blackbull/server/sender.py#L462) | Sprint 9d / 15 |
| `asyncio.timeouts.*` cost removed from per-request hot path | [`blackbull/server/deadline.py`](../blackbull/server/deadline.py) (`ConnectionDeadline`) | Sprint 23 |
| Per-second `Date` header cache (avoids `formatdate` on every response) | [`blackbull/server/sender.py`](../blackbull/server/sender.py) `_http_date` | Sprint 9d |
| Inline access-log capture (no wrapper-per-request layer) | [`blackbull/server/http1_actor.py`](../blackbull/server/http1_actor.py) `send._log_record` pattern | Sprint 8 |
| Optional `[speed-h1]` C parser (httptools / llhttp) | Tracked in [README §P4](../README.md) as opt-in; deliberate non-default to keep the from-scratch identity (HPACK first, httptools later). | (planned, not committed) |

### Sprint 23 — `asyncio.timeouts.*` cost removed from the per-request hot path

_Status: current.  The +6.5 % B1 RPS delta is near the EC2 single-worker
noise band — credibility comes from the py-spy result (`asyncio.timeouts.*`
dropped from 9.6 % to 0 samples), not from the RPS number alone._

Sprint 21 Phase B identified `asyncio.timeouts.*` machinery at
~9.6 % inclusive of per-worker CPU under a B1 saturation profile
(`reschedule` / `__aexit__` / per-call cancel-scope registration).
Sprint 23 replaces the per-phase `async with asyncio.timeout(d):`
context managers in [`blackbull/server/connection_actor.py`](../blackbull/server/connection_actor.py),
[`blackbull/server/http1_actor.py`](../blackbull/server/http1_actor.py),
and [`blackbull/server/recipient.py`](../blackbull/server/recipient.py)
with a single rescheduled `loop.call_later()` `TimerHandle` per
connection (new [`blackbull/server/deadline.py`](../blackbull/server/deadline.py)
`ConnectionDeadline`).  Per-phase semantics preserved — the
`body_timeout` re-arms on every chunk read, matching the nginx
`client_body_timeout` "each read has the same bound" rule.

#### Throughput — c7i.xlarge TOPO=split, single-worker

Single-worker isolates the per-request cost from the multi-worker
scaling factor that confounded the original Phase B finding.
Canonical artefact:
[`bench/results/aws/sprint23/compare_servers_single_worker.md`](results/aws/sprint23/compare_servers_single_worker.md).

| Scenario | Sprint 20 split (req/s) | Sprint 23 (req/s) | Δ |
|---|---|---|---|
| **B1 plaintext c=256** | **14 822** | **15 793** | **+6.5 %** |
| B3 json c=256 | ~14 822 (B1 proxy) | 15 372 | — |
| A1 mux1 (HTTP/2) | 10 462 | 10 236 | −2.2 % (within noise) |

The +6.5 % on B1 is consistent with the Phase B caveat that
single-worker c7i.xlarge isn't fully CPU-saturated (line 515
showed w=1 was within noise across Sprint 16 → Sprint 20).  More
of the CPU saving translates to throughput at higher worker
counts where each worker is closer to saturation; that's the
"compounds across all lanes and workers" point Phase B logged.

**Note on the +6.5 % number itself.**  This single-pass delta is
near the EC2 single-worker noise band (Sprint 13's "EC2 noise
floor" subsection put pure-Python ASGI run-to-run variance at
±15 %, dominated by physical-host placement on fresh
`up.sh` provisions).  The credibility of the win comes from the
**py-spy result below** — `asyncio.timeouts.*` dropping from
9.6 % inclusive to 0 samples is a direct, sampling-based
attribution of the saved cycles, not an inference from a noisy
RPS comparison.  Future sub-10 % deltas in this document carry
the Sprint 24 🌫 noise-flag explicitly.

A1 (HTTP/2) is unchanged within noise — expected, since the
HTTP/2 stream-lifetime `asyncio.wait_for` at
`http2_actor.py:678` is gated on `BB_REQUEST_TIMEOUT > 0` (off
by default) and was deliberately out of scope.

#### Profile — `asyncio.timeouts.*` is gone

`py-spy record --rate 200 --duration 85` on the bench worker
during a k6 200-VU stress pass.  Artefact:
[`bench/results/aws/sprint23/profile_b1_vu200.svg`](results/aws/sprint23/profile_b1_vu200.svg).

| Symbol | Sprint 21 Phase B | Sprint 23 |
|---|---|---|
| `asyncio/timeouts.py` (`reschedule` / `__aexit__`) | 9.6 % | **0 samples** |
| `blackbull/server/deadline.py` | n/a | 0 samples |
| `loop.call_later` / `TimerHandle.cancel` | not material | 0 samples |

The cost moved off the profile entirely.  A `TimerHandle.cancel
+ loop.call_later` pair is too cheap to surface at 200 Hz
sampling, where the previous `Timeout`-object allocation +
cancel-scope registration consistently registered as ~10 % of
per-worker time.

(The dominant frames on the Sprint 23 profile are HTTP/2 work
— k6 stress is HTTP/2 by default — plus the async-logging
QueueListener thread.  Not Sprint 23 surface.)

#### Carry-forward

- HTTP/2 per-stream `asyncio.wait_for` at
  [`http2_actor.py:678`](../blackbull/server/http2_actor.py#L678)
  remains untouched.  Default `BB_REQUEST_TIMEOUT=0` keeps it
  off; deferred unless a profile flags it material.
- Multi-queue ENA / RPS-RFS tuning (~0.5 vCPU on vCPU 0) — pure
  ops, unrelated.
- The deployment-sizing formula's `R_unit` baseline rises by the
  Sprint 23 saving for any future predictions; existing entries
  in the table above were measured before the change and remain
  correct as historical references.

### Sprint 24 — audit follow-ups: doc clarity, methodology, Lane E, HPACK extension

_Status: current.  Doc / methodology / harness work.  The Lane E first pass
exposed a loadgen-side TIME_WAIT exhaustion issue (documented below) — the
nginx baseline is the only clean Lane E number this sprint; BlackBull /
uvicorn Lane E numbers are deferred until the harness ships per-stack
cool-down._

External audit reviewed CHARACTERIZATION.md + the Sprint 13–22 perf
record.  Sprint 24 took on five tranches of work:

1. **Doc clarity** — top-of-file cross-topology warning box; per-sprint
   status badges ("confirmed / superseded / current understanding");
   new "What BlackBull optimises for" section near the top; "Audit
   recommendations already in place" subsection (defensive against
   future reviewers flagging items Sprint 21 Phase C already closed);
   targeted clarifications on B2 (synthetic upper bound), A3 mux=50
   (stress not browser-realistic), Sprint 23 +6.5 % framing.
2. **Methodology upgrade** — Sprint 24+ wrk lanes run `RUNS_WRK=3`
   (default) and emit a trailing **"noise (MAD)"** column showing
   `min..max (MAD X, Y%)`; rows where `MAD / median > 10 %` carry a
   🌫 marker.  See [`bench/wrk/_stats.py`](wrk/_stats.py) and the
   per-stack tables in
   [`bench/results/aws/sprint24/compare_servers_lane_e_first_pass.md`](results/aws/sprint24/compare_servers_lane_e_first_pass.md).
   Also: `DURATION` default raised from 30 s to 60 s; new `WARMUP=15`
   env var fires a 15 s `/plaintext c=64` burst per stack before the
   measured runs to settle Python allocator + kernel TCP autotune +
   TLS session cache.  Older numbers were captured without these and
   remain valid for their topology.
3. **Lane E (connection churn)** — new wrk lane forcing
   `Connection: close` per request via
   [`bench/wrk/no_keepalive.lua`](wrk/no_keepalive.lua) +
   [`bench/wrk/lane_e.sh`](wrk/lane_e.sh).  Lane E exposes TLS
   handshake + accept-loop costs that Lane B's keep-alive amortises
   away.  Opt-in: `LANES="… E-wrk"`.
4. **HPACK fastpath extension** — Sprint 21 Phase C cached the seven
   `:status` static-table entries; Sprint 24 extends the same
   single-byte indexed encoding to the request-side pseudo-headers
   ([`blackbull/protocol/hpack_fastpath.py`](../blackbull/protocol/hpack_fastpath.py))
   — `:method GET/POST`, `:path /` / `/index.html`, `:scheme
   http/https` — which fire on the PUSH_PROMISE encode path
   ([`blackbull/protocol/frame_types.py::PushPromise.save`](../blackbull/protocol/frame_types.py)).
   Wire-equivalence + dynamic-table-neutrality covered by 11 new
   tests under
   [`tests/conformance/http2/test_hpack_fastpath.py`](../tests/conformance/http2/test_hpack_fastpath.py).
   The static table is now **exhausted** for single-byte indexed
   encoding on either side; all other static entries are name-only
   (e.g. `content-type` at index 31), so further fastpath work would
   require the literal-with-indexed-name encoding (RFC 7541 §6.2)
   and is out of scope.
5. **HTTP/1.1 baseline-of-record under the new methodology** —
   blackbull single-worker on c7i.xlarge `TOPO=split` with
   `DURATION=20s`, `RUNS_WRK=3`, `WARMUP=15s`.  Numbers below are
   from the **clean baseline pass**
   ([`bench/results/aws/sprint24/compare_servers_clean_baseline.md`](results/aws/sprint24/compare_servers_clean_baseline.md));
   see the "Host-placement variance" subsection below for why a
   second pass was needed.

   | Scenario | req/s (median) | range / MAD |
   |---|---|---|
   | B1 plaintext c=256 | 13 320 | 12 992..13 577 (MAD 257, 1.9 %) |
   | B3 json c=256 | 13 189 | 12 858..13 616 (MAD 331, 2.5 %) |
   | B4 16 KB c=100 | 12 879 | 12 875..12 963 (MAD 4, 0.0 %) |
   | B5 64 KB c=50 | 9 238 | 9 169..9 386 (MAD 70, 0.8 %) |
   | B6 echo 1k | 12 177 | 12 147..12 211 (MAD 30, 0.2 %) |
   | B7 echo 100k | 3 846 | 3 801..3 925 (MAD 46, 1.2 %) |
   | B2 plaintext c=1024 p=16 | 18 041 | 17 870..18 216 (MAD 171, 0.9 %) |
   | A1 mux1 (HTTP/2) | 10 042 | — |

   Within-pass MAD/median ≤ 2.5 % — methodology is solid.  These
   replace the Sprint 23 single-worker B1 row as the post-Sprint-24
   reference baseline.

#### Host-placement variance — Sprint 23's number was a high outlier

The Sprint 24 first pass measured BlackBull B1 at 13 744 req/s,
13 % below Sprint 23's 15 793.  The clean-baseline pass on a
*third* freshly-provisioned host landed at **13 320** — even
slightly lower, and consistent in direction.  Cross-stack data
on the same hosts:

| Stack | Sprint 20 (135137Z) | Sprint 23 (024813Z) | Sprint 24 first (043450Z) | Sprint 24 clean (073008Z) |
|---|---|---|---|---|
| blackbull B1 req/s | 14 822 | **15 793** | 13 704 | **13 320** |
| blackbull B1 mean lat / stdev | — | 15.73 / **1.17 ms** | 18.08 / 3.89 ms | 18.51 / **6.18 ms** |
| uvicorn B1 req/s | 33 075 | — | (died, TIME_WAIT) | 28 260 |
| uvicorn B1 mean lat / stdev | 7.68 / 2.82 ms | — | — | 8.89 / 2.95 ms |
| nginx B1 req/s | 119 691 | — | 123 940 | — |
| nginx B1 mean lat / stdev | 2.25 / 2.90 ms | — | 2.22 / 3.38 ms | — |

Three observations rule out a BlackBull regression:

1. **Both Python stacks regress in lock-step** on the Sprint 24
   hosts.  BlackBull B1 dropped from 14.8k/15.8k → 13.3k/13.7k
   (−10 to −15 %); uvicorn B1 dropped from 33.1k → 28.3k (−15 %).
   A BlackBull-specific code regression would not symmetrically
   slow uvicorn.
2. **nginx is unchanged.**  Same instance class, same kernel,
   same install harness — yet nginx mean latency is identical
   (2.25 → 2.22 ms) and throughput is even slightly higher
   (+3.5 %).  A generally degraded host would slow nginx too.
3. **BlackBull's latency *stdev* grew 3-5×** on Sprint 24 hosts
   (1.17 → 6.18 ms) while uvicorn's stdev barely moved
   (2.82 → 2.95 ms).  The Sprint 24 hosts have a higher jitter
   floor that single-threaded blackbull inherits more than
   uvicorn's multi-thread-per-worker setup.

Mechanism (most likely): the Sprint 24 c7i.xlarge instances sit
in the lower half of the host pool for **sustained single-core
throughput** — typical causes are L3 contention from a CPU-hot
neighbor, lower sustained turbo frequency, or AWS Nitro
microcode mitigations that hit branch-heavy Python interpreter
loops harder than C event-loop code.  This affects:

- blackbull single-worker most (one Python thread on one vCPU)
- uvicorn moderately (default single-worker; one Python loop)
- nginx not at all (multi-process C, work per request is so
  small the cache-eviction tax doesn't register in the mean)

This is the **same Sprint 13 finding** the document warned about
near the top (~±15 % EC2 noise band for slow-Python stacks;
~±5 % for nginx/granian) — Sprint 24's two-host A/B is now the
canonical evidence.  **Sprint 23's 15 793 was the top of the
distribution; ~13 500 is the realistic mid-pool baseline.**

#### HPACK fastpath extension — neutral under the current matrix

Lane A1 mux1 measured 10 042 req/s in the Sprint 24 clean pass
vs 10 236 in Sprint 23: a −2 % difference, within noise.  This
is expected: the new request-side static entries
(`:method GET/POST`, `:path /`, `:scheme http/https`) only fire
on the PUSH_PROMISE encode path, and no scenario in Lanes
A/B/C/D triggers push.  The extension is **correctness-extending**
(coverage of the static table for the push path), not
**performance-extending** in this matrix.  A push-promise lane
would expose the gain; carry-forward.

#### Lane E first pass — methodology lesson

The Sprint 24 Lane E pass ran four stacks in order
(`blackbull blackbull-cleartext uvicorn nginx`).  Only blackbull's
Lane B and nginx's Lanes B + E produced clean numbers; the other six
stack-lane cells all came back `unable to connect`.

Root cause: Lane E (`Connection: close` per request, `c=256` at
high rate) burns through the **loadgen's** ephemeral source-port pool
in seconds.  Each closed connection lands in `TIME_WAIT` for 60 s by
default (`net.ipv4.tcp_fin_timeout` on Linux), and the 1 s
inter-stack settle the harness uses is far short of that.  The
next stack's wrk inherited an exhausted port pool and could not
dial — even though the (new) server was alive (`wait_ready` passed).

Nginx escaped because it was the LAST stack — by the time we got
there, enough TIME_WAITs had aged out.  Its numbers are
representative:

| Scenario | nginx req/s (median) | range / MAD |
|---|---|---|
| B1 plaintext c=256 (keep-alive) | 123 648 | 122 967..123 940 (MAD 291, 0.2 %) |
| **E1 plaintext c=256 (no-keepalive)** | **3 832** | 3 823..3 847 (MAD 9, 0.2 %) |

That's a **32 ×** throughput collapse from B1 to E1 — exactly the
connection-setup-tax signal Lane E was designed to expose.  TLS
handshake + accept-loop work that the keep-alive lanes amortise
across many requests dominates the cost when every request takes
the full path.

**Lane E methodology fix (carry-forward):** the harness needs one
of:

- An explicit per-stack cool-down ≥ `net.ipv4.tcp_fin_timeout`
  (default 60 s) inserted between stacks when any preceding lane
  ran Lane E.
- Loadgen-side sysctl tuning in [`bench/aws/install.sh`](aws/install.sh):
  `net.ipv4.tcp_tw_reuse=1` + widened
  `net.ipv4.ip_local_port_range` so TIME_WAIT sockets can be
  re-used immediately.
- A blanket recommendation to run Lane E in isolation
  (`LANES="E-wrk" STACKS="<one>"`) until the harness fix lands.

For now, BlackBull / uvicorn Lane E numbers are deferred to a
future sprint.  The nginx data point is enough to validate the
methodology and the **32 × ratio** sets the rough expectation for
a real connection-setup tax.

#### Carry-forward

- **Lane E harness fix** (above) — the highest-priority follow-up.
- The Phase B `R_unit` deployment-sizing formula's `R_unit` rows
  in [§Deployment-sizing formula](#deployment-sizing-formula) are
  still calibrated against pre-Sprint-23/24 numbers; refresh when
  a clean multi-worker AWS pass under the new methodology happens.
- HPACK fastpath benefits the PUSH_PROMISE path only; no benchmark
  in the matrix exercises it.  Add a push-promise scenario to Lane
  A in a future sprint if the gain matters to a real user.

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
