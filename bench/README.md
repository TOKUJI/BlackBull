# BlackBull Benchmark Suite

Establishes a single-process asyncio baseline before multiprocessing work.

## Quick start

```bash
# 1. Install tools (once)
bash bench/install.sh

# 2. Start the benchmark server (leave running in a separate terminal)
python bench/app.py

# 3. Verify the server and lag monitor are up
curl -sk https://localhost:8443/ping          # → pong
curl -sk https://localhost:8443/metrics | python -m json.tool

# 4. Run scenarios
k6 run bench/k6/http_rampup.js               # saturation ramp
k6 run bench/k6/http_stress.js               # constant-load stress
k6 run bench/k6/websocket.js                 # WebSocket RTT
bash bench/h2load_run.sh                     # HTTP/2 stream multiplexing
```

## Benchmark routes

| Route | Method | Response | What it measures |
|-------|--------|----------|-----------------|
| `/ping` | GET | `pong` (4 B) | Framework overhead, latency baseline |
| `/1kb` | GET | 1 KiB random | Small-response throughput |
| `/64kb` | GET | 64 KiB random | Large response + HTTP/2 flow control |
| `/echo` | POST | request body | Request parsing overhead |
| `/ws` | WS | echo | WebSocket round-trip latency |
| `/metrics` | GET | JSON | Event loop lag (in-process) |

## Scenarios

### http_rampup.js — saturation ramp
Ramps from 0 → 200 VUs over ~2 minutes.  Shows the VU count at which
p99 latency starts climbing — the single-worker saturation point.

```bash
k6 run bench/k6/http_rampup.js

# Save raw JSON for post-processing
k6 run --out json=bench/results/rampup_$(date +%Y%m%d).json bench/k6/http_rampup.js
```

### http_stress.js — constant-load stress
500 VUs for 60 seconds.  Use this to find the hard error ceiling and to
compare `/ping` vs `/64kb` throughput at the same concurrency.

```bash
k6 run bench/k6/http_stress.js
k6 run -e TARGET=/64kb bench/k6/http_stress.js   # large response variant
```

### websocket.js — WebSocket RTT
50 concurrent connections, each sending a timestamped echo message every
200 ms.  Records round-trip latency as a k6 `Trend` metric.

```bash
k6 run bench/k6/websocket.js
```

### h2load_run.sh — HTTP/2 stream multiplexing
Compares stream depth 1 (HTTP/1.1-equivalent) vs 10 (browser-like) vs 50
(heavy mux) on the same connection count.  Reveals whether stream
multiplexing actually improves throughput in the single-process model.

```bash
bash bench/h2load_run.sh
bash bench/h2load_run.sh 2>&1 | tee bench/results/h2load_$(date +%Y%m%d).txt
```

## What to record

After each run, fill in this baseline table:

| Scenario | VUs/conns | p50 ms | p95 ms | p99 ms | req/s | loop lag p99 ms |
|----------|-----------|--------|--------|--------|-------|-----------------|
| /ping ramp 200 VU | 200 | | | | | |
| /ping stress 500 VU | 500 | | | | | |
| /64kb stress 50 VU | 50 | | | | | |
| h2load mux-1 | 50c | | | | | |
| h2load mux-10 | 50c | | | | | |
| h2load mux-50 | 50c | | | | | |
| WS echo 50 VU | 50 | | | | | |

---

## Known bugs and limitations

### TODO: DATA frame splitting (deadlock on large responses)

`HTTP2Sender._write_data()` sends the entire response body as a single DATA frame.
Two RFC 7540 constraints are violated when responses are large:

- **Flow-control deadlock**: if `len(body) > connection_window_size` (default 65535 bytes),
  the server blocks waiting for a WINDOW_UPDATE that never arrives because the client
  won't send one until it receives at least some data.  Observed: responses ≥ 65536 bytes
  hang indefinitely under h2load.
- **Max-frame-size violation**: frames > `SETTINGS_MAX_FRAME_SIZE` (default 16384 bytes)
  must not be sent (RFC 7540 §4.2).

**Fix required**: split large bodies into chunks of
`min(connection_window_size, stream_window_size, max_frame_size)` bytes,
sending each chunk as a separate DATA frame and awaiting WINDOW_UPDATE between
chunks when flow-control is exhausted.

**Affected file**: `blackbull/server/sender.py` — `HTTP2Sender._write_data()` and
`HTTP2Sender.__call__()`.

## Interpreting results

| Observation | Implication for multiprocessing |
|-------------|--------------------------------|
| Loop lag p99 > 10 ms under moderate load | Loop is being starved; a blocking call or GIL contention exists |
| CPU at 100%, throughput still low | GIL-bound; multiple processes will help |
| CPU < 50% at saturation | I/O or asyncio overhead is the bottleneck; more workers may not help much |
| mux-10 req/s ≈ mux-1 req/s | Stream multiplexing not helping; head-of-line blocking in the server |
| Error rate spikes before CPU saturates | Connection or FD limit hit |

## Event loop lag monitor

`lag_monitor.py` runs a background asyncio task that periodically wakes up
and measures how much later than scheduled the wakeup actually occurred.
The excess is event loop lag — non-zero means the loop was busy processing
other callbacks and could not service this timer on time.

```python
# Reuse in any BlackBull app:
from bench.lag_monitor import LoopLagMonitor

monitor = LoopLagMonitor(interval=0.05, window=400)

@app.on_startup
async def _start():
    monitor.start()
```
