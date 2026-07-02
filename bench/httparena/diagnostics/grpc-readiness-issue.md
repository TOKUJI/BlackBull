# gRPC readiness gate (`_wait_grpc`) is a no-op: `ghz` exits 0 even when nothing is listening, so gRPC benchmarks start before the server is up

## Summary

The gRPC readiness check keys off **`ghz`'s process exit code**, but `ghz` is a
load tester: it exits `0` whenever the *benchmark run itself* completes, even if
**100% of the RPCs failed** — including `Unavailable` (TCP connection refused,
nothing listening). As a result `_wait_grpc` prints `gRPC server ready` on the
**first attempt against any state**, and the benchmark's first run fires before
the server under test is actually accepting/serving. Frameworks that take a
second or two to bind + warm up score a spurious **0 req/s on run 1** (all
connections refused) while runs 2 and 3 — a few seconds later — are clean.

## The check

`_wait_grpc` (readiness == `ghz` exit code 0):

```bash
for i in $(seq 1 30); do
  if "$GHZ" --insecure --proto "$proto" \
       --call benchmark.BenchmarkService/GetSum -d '{"a":1,"b":2}' \
       -c 1 -n 1 "$target" >/dev/null 2>&1; then
    info "gRPC server ready"; return 0
  fi
  sleep 1
done
```

`ghz … -c 1 -n 1` returns exit code `0` as long as it *ran* the one request —
the RPC's status (`OK`, `Unavailable`, `DeadlineExceeded`, …) is recorded as
benchmark *data*, not reflected in the exit code. So the `if` is always true on
the first iteration and the 30×1s retry loop never actually retries.

## Reproduction (no server required)

```console
$ ghz --insecure --proto benchmark.proto \
      --call benchmark.BenchmarkService/GetSum -d '{"a":1,"b":2}' \
      -c 1 -n 1 127.0.0.1:59999          # nothing is listening on :59999
$ echo "exit=$?"
exit=0                                    # <-- readiness would print "gRPC server ready"
```

`ghz`'s own output shows the RPC failed, while the process still exits `0`:

```
Status code distribution:
  [Unavailable]   1 responses
```

`-O json` makes the discrepancy explicit — exit `0`, but no `OK`:

```jsonc
// nothing listening -> exit 0
{ "count": 1, "statusCodeDistribution": { "Unavailable": 1 } }

// real server       -> exit 0
{ "count": 1, "statusCodeDistribution": { "OK": 1 } }
```

A slightly richer harness (see `ghz_readiness_repro.py`) shows the same exit-0
"ready" verdict for **every** non-serving state:

| server state                              | ghz exit | `_wait_grpc` verdict | time  | RPC status       |
|-------------------------------------------|:-------:|:--------------------:|------:|------------------|
| real (serves `GetSum`)                    |   0     | ready ✅             | 0.03s | `OK`             |
| bind+listen, **never** `accept()`         |   0     | ready ✅ *(false)*   | 20s   | `DeadlineExceeded` |
| `accept()` but never respond              |   0     | ready ✅ *(false)*   | 20s   | `DeadlineExceeded` |
| `accept()` + HTTP/2 SETTINGS, no reply    |   0     | ready ✅ *(false)*   | 20s   | `DeadlineExceeded` |
| **nothing bound at all**                  |   0     | ready ✅ *(false)*   | 0.01s | `Unavailable`      |

`ghz v0.121.0`. The last row is the one that bites in practice: while the
server is still starting, the probe gets connection-refused, `ghz` exits 0, and
the benchmark begins immediately.

## Impact

- The 30-iteration warm-up/retry loop is dead code — it always returns on
  iteration 1, so there is effectively **no readiness gate for gRPC profiles**.
- Frameworks with a non-trivial cold start (bind + fork workers + JIT/allocator
  warm-up) get a **0 req/s run 1** that is a harness artifact, not a property of
  the server. This skews any "min across runs" or "first run" reporting.
- It also masks genuinely broken servers (a server that listens but never
  answers `GetSum` is reported as "ready").

## Fix idea

Gate on the **RPC status**, not the process exit code. Two low-friction options:

**A. Keep `ghz`, check the JSON status distribution (needs `jq`):**

```bash
_wait_grpc() {
  local proto="$1" target="$2"
  for i in $(seq 1 30); do
    if "$GHZ" --insecure --proto "$proto" \
         --call benchmark.BenchmarkService/GetSum -d '{"a":1,"b":2}' \
         -c 1 -n 1 -O json "$target" 2>/dev/null \
       | jq -e '(.statusCodeDistribution.OK // 0) >= 1' >/dev/null; then
      info "gRPC server ready"; return 0
    fi
    sleep 1
  done
  return 1
}
```

**A′. jq-free variant** — grep the human-readable status distribution:

```bash
if "$GHZ" … -c 1 -n 1 "$target" 2>/dev/null | grep -q '\[OK\]'; then
```

**B. Use a probe that fails the process on RPC error** — e.g. `grpcurl`, which
returns a non-zero exit code when the RPC does not return `OK`:

```bash
grpcurl -plaintext -proto "$proto" -d '{"a":1,"b":2}' \
  "$target" benchmark.BenchmarkService/GetSum >/dev/null 2>&1
```

Either way, the `sleep 1` retry loop then does its intended job: it waits until
the server actually answers `GetSum` before the first benchmark run starts.

## Repro files

- `ghz_readiness_repro.sh` — one-liner: `ghz` against a dead port exits 0.
- `ghz_readiness_repro.py` — drives the exact `_wait_grpc` command against 5
  server states (real / never-accept / accept-silent / settings-only /
  no-listener) and prints ghz's exit code + verdict for each.
- `benchmark.proto` — the service definition used by both.
