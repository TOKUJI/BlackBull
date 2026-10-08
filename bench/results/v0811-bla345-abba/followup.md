# BLA-345 common completion follow-up

A: PR #482 initial commit `76e432cf`. B: common message completion and removal of redundant unary generator cleanup. Six alternating ABBA/BAAB rounds, 8,000 bridge calls per sample and 1,000 warmup calls per arm; pinned to the minimum allowed CPU. Positive deltas mean more time. Intervals are paired round deltas with Student t 95% (df=5).

| Shape | Receive | Bytes | Time delta | 95% interval |
|---|---|---:|---:|---:|
| Unary | ASGI | 16 | -4.22% | [-7.63, -0.82]% |
| Unary | ASGI | 65536 | -3.42% | [-5.28, -1.55]% |
| Unary | next_chunk | 16 | -5.58% | [-8.66, -2.49]% |
| Unary | next_chunk | 65536 | -1.53% | [-4.37, +1.32]% |
| Collect | ASGI | 16 | -0.93% | [-7.24, +5.39]% |
| Collect | ASGI | 65536 | -0.46% | [-2.94, +2.01]% |
| Collect | next_chunk | 16 | +1.44% | [-1.64, +4.52]% |
| Collect | next_chunk | 65536 | -1.12% | [-1.97, -0.27]% |

Small unary bridge calls improve by about 4–6% versus the initial PR. Streaming differences are mostly within measurement uncertainty. Socket/protocol costs are excluded; the original release baseline comparison still shows added safety-check costs. The prior wire samples cover the initial PR, not this follow-up.

The full-message and fragmented-message paths now share completion/decompression. HTTP/1.1 Content-Length/chunked and HTTP/2 recipients share the tested stream-body contract through native and ASGI channels; gRPC wire interoperability remains HTTP/2. Buffered `read_body` and the public complete-buffer codec retain their distinct contracts; forcing them through asynchronous incremental parsing would add work.

Raw samples: `followup-micro.tsv`. Source and settings: `followup-experiment.json`.
