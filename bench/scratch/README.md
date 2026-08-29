# `bench/scratch/` — diagnostic instruments

One-off measurement tooling, kept because the Sprint 103/104 regression
investigation rests on it and two open proposals cite it.  These are
*instruments*, not benchmarks: none of them is a gate, none runs in CI, and
several hardcode absolute paths for this checkout.

The lasting lesson from the work that produced them is recorded in
`BLA-12` [private] and is worth repeating here, because it
governs how every number these print should be read:

> **Executed-instruction counting predicts direction and order of magnitude,
> not magnitude.**  Measured against EC2 it *under*-predicts real throughput
> cost — 1.2–1.5× on the HTTP/2 lane, 1.6–2.4× on `/conn` — because bytecode
> counting is blind to C-level work inside builtins, syscalls, allocation and
> GC.  Treat "N % of the lane" from these tools as a **lower bound**.  Do not
> derive a calibration factor from a single A/B interval; that was tried, and
> the factor came out with the sign backwards.

## The instruments

| script | what it answers |
|---|---|
| `count_instructions.py` | **The primary one.**  Executed bytecode instructions per request, per file or per function (`BB_BY_FUNC`), via `sys.monitoring`.  Deterministic where this box's timing is not.  Set `BB_WRK_CONNS`/`BB_H2_CONNS` to match the A/B's concurrency — the default `c=1` inflates the denominator with per-loop overhead that amortises away under load. |
| `split_cost.py` | Per-function bytecode *size* on the receive path, for attributing where a refactor added code. |
| `disasm_hot.py` | Bytecode size of the hot-path functions, two refs, diffed by hand. |
| `park_shapes.py` | Cost of one connection park, four shapes — used to separate "the bug fix" from "the cost of the split" when `wait_for_data` became two coroutines. |
| `attr_shapes.py` | Instance attribute vs class attribute vs one-hop indirection, read and write.  Decided levers B and C (both rejected). |
| `b_shape.py` | The same question in the shape lever B would actually have taken — PEP 412 key sharing means a class default still allocates the slot. |
| `send-model-c.py` | The send-path model `NativeResponse` was built from — option C, unified response object with `header`/`body` views.  Measures what the DX properties cost against the per-request budget.  Cited by `blackbull/native.py` as the source of its design invariants. |
| `log_site_cost.py` | What one disabled `logger.debug(...)` costs, and what each guarding shape recovers.  Produced the change in `e1a0236`. |
| `profile_server.py` | Runs `native_app` under cProfile, dumping on SIGTERM. |
| `profile_residual.sh` | Drives the above across two refs × two lanes, load generator outside the profile. |
| `diff_profiles.py` | Diffs two cProfile dumps by cumulative time, normalised by total CPU time — **not** by the sum of cumulative times, which double-counts children and was the bug that once made a real difference read as zero. |

## Caveat that bit once

`count_instructions.py` runs on whatever Python is local (3.14 here); the EC2
A/B ran 3.12.  Different bytecode, different `LOAD_ATTR` specialisation,
different `logging`/`enum` internals.  That discrepancy is unresolved and is a
live candidate for part of the under-prediction above.
