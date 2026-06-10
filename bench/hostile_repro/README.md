# Hostile-load reproduction harness

Sprint 30 Task 1 (Tier 1.5 design phase) — counterpart to
`bench/static_repro/`.  Exercises BlackBull's event-loop integrity
under deliberately slow / adversarial clients, not against burst
benign load.

The premise: any change to BlackBull's connection-lifecycle code
must keep the event loop responsive when N concurrent clients
behave pathologically.  This harness is the yardstick.

## Attack shapes implemented

Each is a separate Python script under `attacks/`.  Run against a
local BlackBull server (use `bench/static_repro/run.sh` to boot
one; PID exported as `$SERVER_PID`).

| Script | Attack shape | What it exercises |
|---|---|---|
| `slowloris_headers.py` | Open N connections, send request-line + 1 header byte every K seconds | `BB_HEADER_TIMEOUT` (default 10 s) |
| `slowloris_body.py` | Complete headers, then dribble request body | `BB_BODY_TIMEOUT` (default 30 s) |
| `slow_read.py` | Issue request, then read response 1 byte/sec | Slow-read defence — **gap today**, fix in Sprint 30 Task 2 |
| `idle_park.py` | Open N keepalive connections, send one request, then go silent | `BB_KEEP_ALIVE_TIMEOUT` (default 60 s — too generous; Sprint 30 lowers to 5 s) |

## What each probe measures

For each attack, we want the answer to **"is the event loop
healthy?"** while the attack is active:

1. **Task count in target server** — via the SIGUSR1 dumper from
   `bench/static_repro/server_probe.py`.  How many tasks are
   parked on suspended awaits during the attack.
2. **Time-to-recovery** — issue a fresh legitimate request from a
   second client.  Measure the latency.  Healthy: < 100 ms.  Sick:
   seconds, or timing out.
3. **FD count peak** — via `/proc/$SERVER_PID/fd | wc -l`.
4. **Memory peak** — via `/proc/$SERVER_PID/status` `VmRSS`.

## Methodology rules

- **Compare to a legitimate baseline run.**  Without the attack,
  the legitimate client should respond < 10 ms.  The same client
  during an active attack tells us how much the event loop is
  squeezed.
- **Run attacks long enough to hit steady state** — at least 2×
  the relevant timeout (e.g. 60 s for slowloris-header which is
  defended in 10 s, so the attacker reconnects every 10 s).
- **Test before and after each Sprint 30 fix.**  Tier 1 should
  show clear improvement on `idle_park` (lower keepalive timeout
  reaps idle tasks faster) and `slow_read` (write timeout closes
  the slot).  Tier 1.5 (custom protocol) should show universal
  improvement on time-to-recovery because the event loop has
  fewer parked tasks under any attack shape.

## Quick start

```bash
# Boot a local BlackBull server (uses bench/static_repro/server_probe.py
# so SIGUSR1 task dumping works)
STATIC_DIR=$(pwd)/bench/httparena/_local/data/static/ \
DATASET_PATH=$(pwd)/bench/httparena/_local/data/dataset.json \
BB_ACCESS_LOG=0 \
  python3 bench/static_repro/server_probe.py --port 8080 \
  >/tmp/hostile_server.log 2>&1 &
export SERVER_PID=$!
sleep 1

# Run an attack — adjust CONCURRENCY / DURATION via env vars
python3 bench/hostile_repro/attacks/idle_park.py \
    --host localhost --port 8080 \
    --connections 4096 --duration 30

# Mid-attack: snapshot task count + check baseline latency
kill -USR1 $SERVER_PID
time curl -s http://localhost:8080/healthz

# Cleanup
kill $SERVER_PID
```

## What this is NOT

- **Not a benchmark.**  No throughput numbers.  The question is
  binary: is the loop responsive under attack, yes or no?
- **Not a wrk / gcannon replacement.**  Those are for fair-load
  measurement.  This harness explicitly tries to break things.
- **Not a security audit.**  Real adversarial testing involves
  professional tooling and varied attack shapes (HTTP/2 RST flood,
  malformed-frame fuzz, etc.).  This is the minimum bar BlackBull
  should hold during Sprint 30.
