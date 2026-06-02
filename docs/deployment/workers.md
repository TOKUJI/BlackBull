# Workers

By default `app.run()` runs **one** worker process with the
standard asyncio event loop.  This page covers how to scale to
multiple workers, what `uvloop` buys you, and the abuse defences
that protect each worker.

## Pre-fork multi-worker

```bash
# Pre-fork N worker processes, each with its own event loop.
# 0 → os.cpu_count() workers.
BB_WORKERS=8 python app.py

# Equivalently via the CLI:
blackbull myapp:app --workers 8

# Or in code:
app.run(port=8000, workers=8)
```

Multi-worker uses **pre-fork multiprocessing** — each worker is
a separate OS process, not a thread.  The master process binds
the socket, forks workers, and then sleeps until SIGTERM/SIGINT.

On Linux / modern BSDs the workers also share the listening
port via `SO_REUSEPORT` (`BB_SOCKET_REUSEPORT=1` by default), so
the kernel hashes incoming connections across workers' accept
queues — no thundering-herd, per-connection CPU affinity for
free.

### Shared-nothing model

Workers share **nothing** mutable — no shared dict, no shared
cache, no shared lock.  Anything that needs cross-worker
coordination (session storage, response cache, rate-limit
counters) belongs in an external store: Redis, Postgres, or a
sticky-session reverse proxy.

The built-in `Cache` and `Session` middleware explicitly state
their per-worker limits — see [Middleware](../guide/middleware.md).

### Which setting

| Setting | Use case |
|---|---|
| `BB_WORKERS=1` (default) | Development, tests, debugging.  Easiest to reason about; everything in one process. |
| `BB_WORKERS=N` (`N > 1`) | Production CPU-bound workload — each worker saturates one core. |
| `BB_WORKERS=0` | Production "use whatever the box has" — resolves to `os.cpu_count()` at start. |

## `uvloop`

`uvloop` is a drop-in libuv-based replacement for the standard
asyncio event loop.  BlackBull installs it as the loop policy
when `BB_UVLOOP=1` and the `blackbull[speed]` extra is present.

```bash
pip install 'blackbull[speed]'
BB_UVLOOP=1 python app.py
```

If `BB_UVLOOP=1` is set but `uvloop` isn't installed, BlackBull
logs a warning and falls back to the standard loop — your app
keeps running.

Combining both for a production default:

```bash
BB_WORKERS=0 BB_UVLOOP=1 python app.py
```

See [Configuration](../guide/configuration.md) and
[Reference — Environment variables](../reference/env-vars.md) for
the full list of runtime tunables.

## Abuse defences

Three independent limits protect a worker from misbehaving or
malicious clients.  All three apply *before* the application is
reached and are on by default — production deployments should
leave them on and only tune the numbers.

### Slowloris — partial-headers attack

A slowloris attack keeps a TCP connection open by sending the
request header block one byte at a time, indefinitely.  Without
a deadline, every such connection holds one worker slot forever
— a single attacker can exhaust the server's connection pool
with no abnormal traffic volume.

BlackBull enforces a deadline on the HTTP/1.1 header read.
When the client hasn't delivered the complete header block
(request-line + headers + `CRLFCRLF`) within
`BB_HEADER_TIMEOUT` seconds (default `10`), the server answers
`408 Request Timeout` and closes the socket.

```bash
# Default — already on
python app.py

# Tighten to 3 seconds (recommended behind a reverse proxy that already buffers)
BB_HEADER_TIMEOUT=3 python app.py

# Disable (only safe on a trusted local socket)
BB_HEADER_TIMEOUT=0 python app.py
```

HTTP/2 has no equivalent header timeout because the protocol
doesn't allow a peer to drip header bytes one at a time —
`HEADERS` and `CONTINUATION` frames carry their length in the
frame header, and the `MAX_HEADER_LIST_SIZE` SETTING bounds the
total.

### Oversized headers — memory exhaustion

Two limits stop a 1 GB `X-Foo: <random bytes>` header from
sitting in `readuntil`'s buffer until the line terminator
arrives:

| Limit | Default | Triggered when |
|---|---|---|
| `BB_HEADER_MAX_LINE` | `8192` | A single header line (or the request line) exceeds this |
| `BB_HEADER_MAX_TOTAL` | `65536` | The full header block exceeds this |

A request that exceeds either limit gets
`431 Request Header Fields Too Large` and the connection is
closed.  The defaults match Apache's `LimitRequestLine` /
`LimitRequestFieldsize` and nginx's `large_client_header_buffers`.

### Connection cap and per-request timeout

| Limit | Default | Behaviour |
|---|---|---|
| `BB_MAX_CONNECTIONS` | `500` per worker | Connections beyond the cap are refused at accept time.  Combine with `BB_SOCKET_BACKLOG` for graceful overload.  `0` = unlimited. |
| `BB_REQUEST_TIMEOUT` | `0` (off) | Per-HTTP/2-stream deadline in seconds.  Set in production (e.g. `30`) so an ASGI handler hung on an upstream call can't keep its stream slot indefinitely.  Stream is cancelled via `RST_STREAM CANCEL`. |

## Production checklist

A reasonable production environment:

```bash
BB_WORKERS=0 \
BB_UVLOOP=1 \
BB_HEADER_TIMEOUT=3 \
BB_REQUEST_TIMEOUT=30 \
BB_MAX_CONNECTIONS=1000 \
BB_ACCESS_LOG=1 \
python app.py --cert /etc/ssl/site.pem --key /etc/ssl/site.key
```

What each line buys you:

- `BB_WORKERS=0` — fills the CPU budget without hand-counting.
- `BB_UVLOOP=1` — typical 1.5-2× throughput on HTTP/2 hot paths.
- `BB_HEADER_TIMEOUT=3` — slowloris defence; 3 s is plenty
  behind a buffering reverse proxy.
- `BB_REQUEST_TIMEOUT=30` — evicts stalled handlers from HTTP/2
  stream slots.
- `BB_MAX_CONNECTIONS=1000` — caps memory at a known ceiling.
- `BB_ACCESS_LOG=1` — leave on unless a separate log aggregator
  is consuming structured logs.

Equivalent TOML for a config file — see
[Configuration](../guide/configuration.md#toml-config-file).

## Next

- [TLS](tls.md) — HTTPS, HTTP/2 via ALPN, mTLS.
- [Behind nginx](behind-nginx.md) — running multi-worker
  BlackBull behind a load-balancing reverse proxy.
- [Reference — Environment variables](../reference/env-vars.md)
  — the full `BB_*` table.
