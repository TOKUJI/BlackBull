# gRPC request verification

Keep generated samples outside Git. Attach evidence to the relevant issue with
`just yt-attach ISSUE FILE`; `just yt-show ISSUE` lists attachment names and sizes.
The BLA-345 historical samples, settings, reports, and verification logs are in
`BLA-345-benchmark-evidence-20261009.zip` on BLA-345.

## Bridge comparison

Run from the repository root on Linux with the same dependency environment for
both arms. The baseline source is executed locally; use a trusted commit.
Only `grpc/asgi.py` varies, so verify that its shared dependencies have compatible
semantics. This measures the bridge with a no-op response collector, not sockets.

```sh
PYTHONPATH=. timeout 90s uv run python bench/grpc_bridge.py \
  --baseline 76e432cf --rounds 6 --calls 8000 --warmup 1000 \
  --sizes 16 65536 > /tmp/grpc-micro.tsv 2> /tmp/grpc-settings.json
```

For the initial release comparison, check out `76e432cf`, use the reproduction
script from this branch, and set baseline `3025eb95`, calls `4000`, warmup `500`,
and sizes `16 4096 65536`. For the common-completion comparison, compare
`76e432cf` with `06bbc515` using the defaults above. The original environment was
CPython 3.14.6. Samples alternate ABBA/BAAB and pin to the minimum allowed CPU.

## HTTP/2 wire comparison

Use separate worktrees at the baseline and treatment commits. On each, serve a
`BlackBull` app with `GrpcServiceRegistry` methods `/svc/Unary` and `/svc/Collect`,
using the handlers in `grpc_bridge.py`. Enable it with `app.enable_grpc(registry)`.
Copy this branch's `bench/grpc_bridge.py` to `/tmp/grpc_bridge.py` before switching
worktrees; historical commits do not contain it. The startup below imports that
shared copy, while protocol code comes from the current worktree.
Run one bounded server per sample in a separate terminal (replace `SERVER_CPU`
and set `BB_FORCE_ASGI_SCOPE` to `0` or `1`):

```sh
taskset -c SERVER_CPU timeout 30s env PYTHONPATH=.:/tmp BLACKBULL_ENV=production \
  BB_ACCESS_LOG=0 BB_UVLOOP=0 BB_WORKERS=1 BB_FORCE_ASGI_SCOPE=0 uv run python - <<'PY'
from blackbull import BlackBull
from blackbull.grpc import GrpcServiceRegistry
from grpc_bridge import unary, collect
registry = GrpcServiceRegistry()
registry.add_method('/svc/Unary', unary)
registry.add_method('/svc/Collect', collect)
async def stream(request, context):
    if hasattr(request, '__aiter__'):
        async for _ in request:
            pass
    for _ in range(1000):
        yield b'ok'
registry.add_method('/svc/Server', stream, client_streaming=False)
registry.add_method('/svc/Bidi', stream, client_streaming=True)
app = BlackBull()
app.enable_grpc(registry)
app.run(port=8080)
PY
```

Use `BB_FORCE_ASGI_SCOPE=0` and `1` as separate cells. Pin server and load generator
to separate CPUs. Generate the 16-byte framed input once, outside Git:

```sh
PYTHONPATH=. uv run python -c \
  'from blackbull.grpc import encode_message; from pathlib import Path; Path("/tmp/grpc-body.bin").write_bytes(encode_message(b"x" * 16))'
timeout 30s h2load -n 5000 -c 8 -m 8 -d /tmp/grpc-body.bin \
  -H 'content-type: application/grpc' http://127.0.0.1:8080/svc/Unary
```

Warm each sample with 500 requests using the same command, then measure 5,000.
Repeat for `/svc/Collect`. Use three ABBA/BAAB/ABBA rounds, and run a separate null
phase with identical treatment builds for both labels. Reclaim the server and
load process after each sample, including timeout or failure. Reject samples
with HTTP or transport errors; check gRPC statuses separately with integration
tests. The historical wire comparison covers `3025eb95` versus `76e432cf`, not
the later common-completion change. The incomplete 64 KiB wire run is excluded.

For each round, compute `100 * (mean(B) / mean(A) - 1)`. Use paired round deltas
and Student t 95% intervals (df=5 for bridge, df=2 for wire). A micro delta is
time; a wire delta is throughput. An interval crossing zero does not establish
equivalence, and null-label drift limits interpretation of small effects.

Use `--timeout 1S` for a separate bridge comparison with a client deadline.
For deadline changes, compare both with and without this option; use the same
header for both arms.
Use `--shapes Server Bidi --messages 1000 --calls 100 --warmup 10 --sizes 16`
for synchronous streaming bursts; add `--yield-every 100` for a suspending
producer. Each arm validates all response frames before timing. Repeat with
`--null` to measure label drift with identical bridge sources. For BLA-344,
compare against `dd073c9c` (before the fix) and `8a40ae41` (initial fix).
For wire comparison, add `-H 'grpc-timeout: 1S'` to both warmup and measured runs.
The BLA-344 follow-up uses Python 3.11/3.14, all four methods, both scope lanes,
50 warmup requests and 500 measured requests per sample. Streaming methods emit
1000 messages. Compare `dd073c9c` with the candidate, then repeat both labels
using the candidate. Validate `grpc-status: 0` and the complete framed response
before load generation. Exact runner, settings and samples are attached to
BLA-344 as `BLA-344-performance-followup-20261010.zip`.

## Correctness

```sh
just test
just typecheck
just docs-build
timeout 180s uv run pytest -q -m integration tests/integration/test_grpc.py
```

gRPC wire interoperability is HTTP/2. The HTTP/1.1 Content-Length/chunked checks
in `test_native_receive_channel.py` cover the shared body-reader contract.
