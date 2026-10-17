# `bench/micro/` — diagnostic instruments

Instruments that isolate one cost: they answer "how much does this piece
cost, and why", not "is the server faster" (that is `bench/ab/`).  None is a
gate except `loop_touches.py`, which CI runs.  Each keeps its numbers in its
run directory under [`bench/results/`](../results/README.md).

## Read every number here as a lower bound

The lasting lesson (`BLA-12` [private]):

> **Executed-instruction counting predicts direction and order of magnitude,
> not magnitude.**  Measured against EC2 it *under*-predicts real throughput
> cost — 1.2–1.5× on the HTTP/2 lane, 1.6–2.4× on `/conn` — because bytecode
> counting is blind to C-level work inside builtins, syscalls, allocation and
> GC.  Treat "N % of the lane" from these tools as a **lower bound**.  Do not
> derive a calibration factor from a single A/B interval; that was tried, and
> the factor came out with the sign backwards.

`count_instructions.py` runs on whatever Python is local; an EC2 A/B may run
another version, with different bytecode and specialisation.  Compare counts
only between runs on the same interpreter.

## The instruments

| script | what it answers |
|---|---|
| `testing_client.py --base PATH --head PATH` | TestClient HTTP calls, WebSocket round trips and lifecycle cost: A/A and ABBA, source proof, static bytecode comparison and executed testing-helper instructions. Rates are operations/second, not wire throughput. |
| `count_instructions.py` | Executed bytecode instructions per request, per file or per function (`BB_BY_FUNC`), via `sys.monitoring`.  Point `BB_REPO` at two worktrees and diff.  Set `BB_WRK_CONNS` / `BB_H2_CONNS` to the A/B's concurrency — the default `c=1` inflates the denominator with per-loop overhead that amortises away under load. |
| `loop_touches.py` | Event-loop interactions per request; `--check` is the CI budget. |
| `parser_microbench.py` | Request-head parse cost against httptools / h11 / sanic, swept over a real browser's header count, after a correctness gate. |
| `router_microbench.py` | `Router._resolve` and the route cache, per regime (fixed, param, miss). |
| `middleware_stack.py` | Per-middleware cost: N no-op middleware in front of a tiny app. |
| `streams_vs_protocol.py` | asyncio streams against a raw `Protocol`, identical wire behaviour. |
| `grpc_bridge.py` | The gRPC bridge's request path (see `grpc-request-verification.md`). |
| `profile_under_load.sh` | py-spy flame graph (speedscope JSON) of the HTTP/2 app under k6 stress. |
| `hotpath/` | The header-line cache: hit rate on captured browser traffic, break-even, worst case. |
