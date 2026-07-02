# unary-grpc cold-start fixes — local A/B results

Measured with `readiness_ab.py` on WSL2 (4 workers, no dataset/DB, so the local
cold window is small — ~0.2s to first serve; absolute numbers are not EC2, but
the *relative* behaviour of each fix holds).  See also
[grpc-readiness-issue.md](grpc-readiness-issue.md) for why run-1 fires into the
cold window in the first place (HttpArena's readiness gate is a no-op).

## What "ready" actually means

Time-to-ready = container-start → first **served** request.  It splits into:

- **T_bind** — socket starts *listening* (below it: connections **refused**).
- **T_serve** — a worker *accepts + answers* (between T_bind and T_serve:
  connections **queue** in the backlog).

`BB_EARLY_BIND` collapses **T_bind → ≈0**.  It does **not** move T_serve — the
first answer is always gated by worker startup.  So early-bind's value is
*eliminating refusals during the wait*, not shortening the wait itself.
`BB_GRPC_WARMUP` sits *inside* T_serve (it runs before bind+serve), so it
*lengthens* time-to-ready.

## Part A — regular load: does the waiting time shorten?

Light prober opens a fresh h2c connection every 20ms from launch, issues GetSum;
median of 3 runs.

| config | t_first_ok | fails before first OK |
|---|---:|---:|
| WITHOUT_ANY_FIX (base) | ~0.25 s | ~8 (refused) |
| BB_EARLY_BIND + BACKLOG=4096 | ~0.19 s | ~2 (refused) |

**Reading:** the waiting time itself is barely shorter (worker-gated in both).
What early-bind changes is the **refused attempts during the wait: 8 → 2**.  The
residual ~2 are the launcher's own ~40ms Python startup before it binds.  On EC2
(seconds-long cold window) the refused count is far larger, which is the whole
run-1 collapse.

## Part B — burst of 256 at bind, patient client (wait_for_ready), 2.5s deadline

Fire 256 concurrent GetSum the instant the port is connectable; count losses.
Deadline sits between the no-warmup serve time (~0.3s) and the warmup serve time
(~3.2s) so warm-up's delay surfaces as loss.

| config | bound | ok | refused | **lost (deadline)** | drain_last |
|---|---:|---:|---:|---:|---:|
| BB_EARLY_BIND + BACKLOG | 0.03 s | **256** | 0 | **0** | 0.21 s |
| + BB_ACCEPT_THREAD | 0.03 s | **256** | 0 | **0** | 0.21 s |
| + BB_GRPC_WARMUP=3 | 0.03 s | 0 | 0 | **256** | — |

**Reading:**
- **early-bind + backlog** absorbs the whole 256 burst with **zero loss** — the
  event loop drains it in ~0.2s.
- **BB_ACCEPT_THREAD** changes **nothing** — the event-loop accept path already
  drains the burst; the dedicated accept thread adds no headroom here.
- **BB_GRPC_WARMUP=3** turns the burst into **256 losses**: it blocks serving
  for its 3s budget, so every queued connection times out.  Warm-up *causes*
  burst loss on the cold path (while helping steady-state throughput on runs
  2-3 — a different axis).

## Verdict for users

| fix | shortens time-to-ready? | reduces cold-start loss? | keep? |
|---|---|---|---|
| **BB_EARLY_BIND** | no (worker-gated) but removes refusals | **yes** (refuse → queue) | **yes** |
| **BB_SOCKET_BACKLOG=4096** | — | **yes**, paired with early-bind (holds the burst) | **yes** |
| **BB_ACCEPT_THREAD** | no | no | drop |
| **BB_GRPC_WARMUP** | **no — lengthens it** | **no — causes loss** on cold path | steady-state only; keep for published Best, not for readiness |

The cold-start winner is **BB_EARLY_BIND + BB_SOCKET_BACKLOG**, without accept-thread.
Warm-up stays a steady-state (throughput) tool, in direct tension with readiness —
the EC2 run measures that trade on the real profiles.

## Reproduce

```bash
cd bench/httparena/diagnostics
python readiness_ab.py partA   # time-to-ready
python readiness_ab.py partB   # burst losses
```
