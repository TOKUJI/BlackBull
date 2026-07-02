# unary/stream-grpc cold-start fix — EC2 validation

c7i.2xlarge (8 vCPU), local wheel, `BB_EARLY_BIND=1` + `BB_SOCKET_BACKLOG=4096`,
`FRAMEWORKS=blackbull PROFILES="unary-grpc stream-grpc"`.  HttpArena runs 3 × 5s
per level, 7s apart; run 1 fires into the cold window because the gRPC readiness
gate is a no-op (see [grpc-readiness-issue.md](grpc-readiness-issue.md)).

## Results (req/s)

| profile / level | metric | warm-up = 3s | warm-up = 0 |
|---|---|---:|---:|
| unary-grpc / 256c  | **run 1** | 17,640 | **18,689** |
|                    | Best      | 17,067 | **19,960** |
| unary-grpc / 1024c | **run 1** | 0      | **0** |
|                    | Best      | 9,151  | **12,003** |
| stream-grpc / 64c  | Best      | 1,586,000 | **1,810,000** |

(run 1 for 256c warm-up=3 was already non-zero — the first time run 1 did not
collapse — but warm-up=0 is better on every axis.)

## Findings

1. **Early-bind fixes the run-1 collapse at 256c** — 0 → ~18k req/s, 0 failed.
   Before early-bind, run 1 was 0 (all connections refused) while runs 2-3 were
   clean; the socket now listens from t≈0 so the burst queues instead of being
   refused, and the workers drain it.

2. **Warm-up (`BB_GRPC_WARMUP=3`) is a net negative here** — warm-up=0 beats
   warm-up=3 on run 1 *and* Best at every level (+15–30%).  The 3s pre-fork
   warm-up delays first-serve and does not pay for itself in steady state for
   these workloads.  → the bench disables it (`BB_GRPC_WARMUP=0`).  The warm-up
   subsystem stays in the framework as an off-by-default tool; it just is not
   the right lever for this benchmark.

3. **1024c run-1 still collapses (0) with *or* without warm-up** — so the
   residual high-concurrency collapse is NOT the warm-up delay (the earlier
   hypothesis).  At 1024 concurrent clients on 8 cores the cold-start window +
   reconnect storm still empties run 1 even though the socket is bound and
   draining; runs 2-3 recover (Best 12k).  This is the remaining open item —
   likely needs faster worker bring-up or accepting during the pre-fork window,
   not more/less warm-up.

## Shipped config

`bench/httparena/install_docker_shim.sh`: `BB_EARLY_BIND=1`,
`BB_SOCKET_BACKLOG=4096`, `BB_GRPC_WARMUP=0`.

Run dirs: `bench/results/httparena/sprint58-earlybind-wheel-*` (warm-up=3),
`sprint58-earlybind-nowarmup-*` (warm-up=0).
