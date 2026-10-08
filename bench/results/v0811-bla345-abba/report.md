# BLA-345 local performance comparison

Baseline: `3025eb95`. Both arms use the same CPython 3.14 environment. A is baseline; B is the bounded common request parser. Positive throughput deltas favor B; positive micro time deltas mean more time.

The public bridge micro comparison shows about 1 microsecond of additional work on small unary calls. Slicing complete messages directly from transport chunks avoids an intermediate buffer and improves large streaming inputs. Wire measurements have limited precision; intervals crossing zero do not establish equivalence.

## Public bridge micro comparison

Six alternating ABBA/BAAB rounds, 4,000 calls per sample, 500 warmup calls per arm; the same no-op response collector is used for both arms. Socket/protocol costs are excluded.

| Shape | Receive | Payload | A ns/call | B ns/call | Time delta |
|---|---|---:|---:|---:|---:|
| Unary | ASGI | 16 | 2928 | 4099 | +40.02% |
| Unary | ASGI | 4096 | 3099 | 4347 | +40.26% |
| Unary | ASGI | 65536 | 4119 | 5510 | +33.79% |
| Unary | next_chunk | 16 | 3012 | 4144 | +37.61% |
| Unary | next_chunk | 4096 | 3050 | 4270 | +40.00% |
| Unary | next_chunk | 65536 | 4101 | 5319 | +29.69% |
| Collect | ASGI | 16 | 3652 | 4313 | +18.09% |
| Collect | ASGI | 4096 | 4037 | 4683 | +16.02% |
| Collect | ASGI | 65536 | 6760 | 5121 | -24.24% |
| Collect | next_chunk | 16 | 3513 | 4099 | +16.67% |
| Collect | next_chunk | 4096 | 4039 | 4253 | +5.30% |
| Collect | next_chunk | 65536 | 6880 | 5198 | -24.46% |

## HTTP/2 small-message wire comparison

Three ABBA/BAAB/ABBA rounds per cell, 5,000 requests per sample, 500 warmup requests, 8 connections and 8 concurrent streams per connection; `h2load` sends one 16-byte LPM message per RPC. Server and client use separate pinned CPUs. Null arms both use the head implementation. All 480,000 measured requests completed without HTTP/transport errors; gRPC statuses are verified separately by integration tests.

Intervals use paired round deltas and a two-sided Student t 95% interval (df=2). Null and real phases ran as separate bounded jobs. These local results do not rule out small regressions.

| Phase | Connection | Shape | Throughput delta | 95% interval |
|---|---|---|---:|---:|
| null | native | Unary | +2.41% | [-12.69, +17.51]% |
| null | native | Collect | +0.65% | [-11.33, +12.64]% |
| null | ASGI | Unary | -0.65% | [-1.09, -0.21]% |
| null | ASGI | Collect | -0.47% | [-2.38, +1.45]% |
| real | native | Unary | -0.19% | [-4.75, +4.37]% |
| real | native | Collect | +0.22% | [-1.57, +2.00]% |
| real | ASGI | Unary | -0.66% | [-4.72, +3.40]% |
| real | ASGI | Collect | -0.12% | [-3.97, +3.72]% |

The null ASGI unary interval excludes zero, indicating label/time drift and limiting interpretation of small effects.

The initial combined 16-byte/64-KiB wire run was stopped because large-message samples did not fit the bounded job; its incomplete data is excluded. Large-message claims above come only from the micro comparison.

Raw samples and exact settings are in `micro.tsv`, `wire.tsv`, and `experiment.json`.
