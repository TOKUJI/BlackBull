# BlackBull Benchmark Suite

HTTP/2 performance benchmarks for the BlackBull ASGI server.

## Quick start

```bash
# 1. Install tools (once)
bash bench/install.sh
pip install "blackbull[speed]"      # uvloop (optional, recommended)

# 2. Start the benchmark server
#    Set the same env vars for both the server and the bench script.
BB_WORKERS=12 BB_UVLOOP=1 python bench/app.py --port 8443 \
    --cert tests/cert.pem --key tests/key.pem

# 3. Verify
curl -sk https://localhost:8443/ping          # → pong
curl -sk https://localhost:8443/metrics | python -m json.tool

# 4. Run scenarios (in a second terminal — no env vars needed; server config is queried live)
bash bench/h2load_run.sh                             # → bench/results/YYYYMMDD-HHMMSS.md
bash bench/k6_run.sh bench/results/YYYYMMDD-HHMMSS.md  # appends k6 section to same file
k6 run bench/k6/websocket.js
```

`h2load_run.sh` saves raw output to `bench/results/raw_YYYYMMDD-HHMMSS.txt` and generates a
markdown summary at `bench/results/YYYYMMDD-HHMMSS.md` automatically.
`k6_run.sh` runs the 200-VU rampup and 500-VU stress scenarios and appends a k6 section to the
same summary file.  Pass the md path as the first argument; omit it to create a standalone file.

To regenerate a summary from an existing raw log:

```bash
python bench/summarize.py bench/results/raw_20260517-201141.txt
```

## Server configuration

| Env var | Default | Effect |
|---|---|---|
| `BB_WORKERS` | `1` | Worker process count (0 = cpu_count) |
| `BB_UVLOOP` | `0` | Use uvloop event loop (`1` = enable) |
| `BB_H2_ACTIVE_STREAMS_1W` | `20` | Per-connection concurrent-handler cap for 1-worker |
| `BB_H2_ACTIVE_STREAMS` | `20` | Per-connection concurrent-handler cap for N-worker |
| `BB_H2_INITIAL_WINDOW_SIZE` | `1048576` | Per-stream flow-control window (bytes) |
| `BB_H2_CONNECTION_WINDOW_SIZE` | `4194304` | Connection-level flow-control window (bytes) |

## Benchmark routes

| Route | Method | Response | What it measures |
|-------|--------|----------|-----------------|
| `/ping` | GET | `pong` (4 B) | Framework overhead, latency baseline |
| `/1kb` | GET | 1 KiB random | Small-response throughput |
| `/16kb` | GET | 16 KiB random | Medium response, fits in one max-size DATA frame |
| `/64kb` | GET | 64 KiB random | Large response, exercises DATA frame splitting |
| `/1mb` | GET | 1 MiB random | Very large response, exercises flow-control window |
| `/echo` | POST | request body | Request parsing overhead |
| `/ws` | WS | echo | WebSocket round-trip latency |
| `/metrics` | GET | JSON | Event loop lag (in-process) |

## Scenarios

### Tool comparison

| Tool | Protocol | What it measures |
|---|---|---|
| h2load | HTTP/2 only | Raw HTTP/2 multiplexing throughput — streams per connection, frame throughput, flow-control behaviour |
| k6 | HTTP/2 via ALPN | VU-based latency — p50/p95/p99 under realistic concurrent-user load; each VU holds one persistent HTTP/2 connection |

k6 negotiates HTTP/2 automatically via ALPN (`h2` advertised in the TLS handshake).  Both k6
scripts track this with a custom `http2_ok` Rate metric and a `threshold: rate > 0.99`.  The
`proto` column in the summary shows `HTTP/2 ✓` when all requests used HTTP/2.

### h2load_run.sh — HTTP/2 stream multiplexing

Compares stream depth 1 (HTTP/1.1-equivalent), 10 (browser-like), and 50 (heavy mux) on the
same connection count.  Outputs a markdown summary to `bench/results/`.

Request counts are sized so every scenario runs ≥ 1.4 s at 12-worker+uvloop throughput,
keeping TLS-setup fraction below 2%.  The summary file flags any scenario where TLS% > 2% with ⚠.

### http_rampup.js — saturation ramp (HTTP/2)

Ramps from 0 → 200 VUs over ~3 minutes.  Each VU holds one persistent HTTP/2 connection.
Shows the VU count at which p99 latency starts climbing — the saturation point for the given
worker count.

```bash
k6 run bench/k6/http_rampup.js
```

### http_stress.js — constant-load stress (HTTP/2)

500 VUs for 60 seconds.  Each VU holds one persistent HTTP/2 connection.
Finds the hard error ceiling and compares `/ping` vs `/64kb` throughput at the same concurrency.

```bash
k6 run bench/k6/http_stress.js
k6 run -e TARGET=/64kb bench/k6/http_stress.js
```

### websocket.js — WebSocket RTT

50 concurrent connections, each sending a timestamped echo message every 200 ms.
Records round-trip latency as a k6 `Trend` metric.

## Interpreting results

| Observation | What it means |
|-------------|---------------|
| mux-1 req/s ≈ mux-10 req/s | Stream multiplexing not helping; head-of-line blocking in the server |
| mux-50 req/s < mux-10 req/s | Per-worker event loop saturated by too many concurrent tasks; lower `BB_H2_ACTIVE_STREAMS` |
| high ±sd on req/s per connection | SO_REUSEPORT distributed connections unevenly across workers |
| TLS% ⚠ in summary | Run was too short; increase `-n` in h2load_run.sh for that scenario |
| Loop lag p99 > 10 ms | Event loop starved; blocking call or GIL contention |
| CPU at 100%, throughput low | GIL-bound; more workers will help |
| CPU < 50% at saturation | I/O or asyncio overhead; more workers may not help |
| Error rate spikes before CPU saturates | Connection or FD limit hit |
| k6 summary shows `HTTP/2 ⚠` | ALPN negotiation fell back to HTTP/1.1; check server TLS config |

## Event loop lag monitor

`lag_monitor.py` runs a background asyncio task that measures how much later than scheduled
each wakeup actually occurred.  Non-zero lag means the loop was busy and could not service
the timer on time.

```python
from bench.lag_monitor import LoopLagMonitor

monitor = LoopLagMonitor(interval=0.05, window=400)

@app.on_startup
async def _start():
    monitor.start()
```
