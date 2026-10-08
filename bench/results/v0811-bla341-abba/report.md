# BLA-341 performance verification

Baseline: `1faf0d57`. Original PR: `827a6013`. Final runtime: `65fb9794`.

Local CPython 3.14, identical venv, pure asyncio, one worker, access logging off.
Server CPU 1, load CPUs 4–5, microbench CPU 0. Checks ran on CPUs 8–11.

## Method

Each phase alternates ABBA/BAAB rounds; the null phase uses final bytes under both labels.
Wire: three rounds per phase, h2load 1.59.0, two load threads, 16 connections,
1,000 warmup requests and 10,000 measured requests per sample. HTTP/1.1 uses
`--h1 -m1`; HTTP/2 uses h2c `-m8`. Both native and `BB_FORCE_ASGI_SCOPE=1` are tested.
Fresh server per arm/boundary; source hashes are checked before every arm.

Equivalent workloads: `/ok` returns bytes; `/missing` invokes a custom 404;
`/bad` raises an explicitly handled HTTPException subclass; `/default` raises
HTTPException(400) and uses the framework renderer. Logging is disabled in the workload.

384 samples, 3,840,000 measured requests; no transport errors/timeouts. Expected
4xx responses are verified as completed requests with the expected status class.
All owned server process groups stopped; completion marker is retained locally.

Micro: six rounds per phase, 10,000 lookup warmups, 100,000 calls × five repeats;
dispatch uses 5,000 calls × five repeats after a 5,000-request warmup. Each sample
is the median repeat. Each round averages the two measurements per arm.
The tables report mean paired percentage changes and Student t 95% intervals
(df=2 wire, df=5 micro). These are descriptive intervals, not equivalence tests.

## Wire throughput

Positive delta means faster. A/A is the identical-code control.

| Protocol / boundary / path | Delta | 95% interval | A/A delta |
|---|---:|---:|---:|
| h1/native/ok | +0.04% | -2.12% to +2.20% | -1.46% |
| h2/native/ok | -4.40% | -15.14% to +6.34% | -3.82% |
| h1/native/missing | -1.32% | -4.46% to +1.82% | -1.89% |
| h2/native/missing | -0.89% | -9.24% to +7.46% | -3.80% |
| h1/native/bad | -0.56% | -3.08% to +1.96% | +0.99% |
| h2/native/bad | -1.03% | -14.59% to +12.52% | -1.32% |
| h1/native/default | -0.69% | -6.35% to +4.97% | -0.89% |
| h2/native/default | +1.76% | -18.16% to +21.68% | -1.89% |
| h1/asgi/ok | +0.01% | -2.85% to +2.88% | +0.23% |
| h2/asgi/ok | +2.21% | -6.32% to +10.73% | -2.27% |
| h1/asgi/missing | -0.90% | -3.15% to +1.34% | +0.89% |
| h2/asgi/missing | +0.93% | -4.12% to +5.98% | -1.28% |
| h1/asgi/bad | +1.47% | -2.63% to +5.57% | +0.90% |
| h2/asgi/bad | -1.22% | -7.52% to +5.09% | +1.92% |
| h1/asgi/default | -0.49% | -3.13% to +2.15% | -0.80% |
| h2/asgi/default | +0.30% | -1.92% to +2.52% | -3.93% |

All 16 intervals include zero: no significant throughput regression detected.
HTTP/2 intervals are wide; the table shows the sensitivity limits. These
measurements do not establish equivalence. The normal dispatch path is unchanged.

## Lookup costs

Initial PR status lookup increased from 56.5 to 69.4 ns (+22.9%,
95% interval +22.1% to +23.8%). The extra delegation was removed.
A trial that replaced the converter exact-match fast path increased its cost
from 63.6 to 85.7 ns; that trial was rejected.

Final versus baseline (negative means less time):

| Lookup | Baseline ns | Final ns | Delta | 95% interval |
|---|---:|---:|---:|---:|
| status | 63.6 | 58.9 | -6.15% | -17.17% to +4.88% |
| converter_exact | 68.0 | 64.7 | -4.55% | -10.02% to +0.92% |
| converter_mro | 120.6 | 119.2 | -0.97% | -7.24% to +5.30% |
| exception_exact | 142.4 | 93.2 | -34.24% | -39.47% to -29.01% |
| exception_mro | 149.1 | 146.7 | -1.55% | -5.49% to +2.39% |
| exception_default | 174.0 | 77.4 | -55.53% | -57.69% to -53.38% |

Exception microbench calls the application entry: final `resolve(type(exc), status)`
versus baseline instance indexing. Exact exception lookup and empty-registry
fallback improve; status, inherited exception and converter lookups show no
significant regression. `dispatch_guard` selects different renderers across
baseline/fix and is excluded from equivalent-workload performance claims.

## Verification

`just test` and `just typecheck`: 10,301 passed each. Strict docs build passed.
Eight instrumented TLS integration cases cover HTTP/1.1/HTTP/2 × native/forced
ASGI × status/explicit exception registration. They check error state, each
lifecycle event once, peer-port identity on follow-up, and preservation of a
completed 200 response after a late exception. Independent review approved.

A preliminary wrk session failed its A/A gate with 16 timeout reports and was
excluded entirely. It supplies no regression evidence. The complete replacement
uses h2load for both protocols. This is local verification; no cloud run.

`wire.tsv`, `micro.tsv`, and `experiment.json` retain measurements and settings.
Full tool logs, import proofs, checks and completion markers are in
`/tmp/bla341-perf/` in the shared workspace.
