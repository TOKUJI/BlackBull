# Configuration

BlackBull reads its runtime configuration from three sources,
which compose with a deterministic precedence:

> **CLI flags > environment variables > TOML config file > defaults**

A more specific source overrides a less specific one.  This means
you can ship a config file with sensible defaults, override
individual values via env in your container, and override
those again via CLI flags for one-off debugging.

## Environment variables

Every runtime knob has a `BB_*` (or `BLACKBULL_*`) environment
variable.  Setting it on the process environment is the most
common form of configuration:

```bash
BB_WORKERS=4 BB_UVLOOP=1 BB_ACCESS_LOG=1 \
    blackbull myapp:app --bind 0.0.0.0:8080
```

The full table is in [Reference — Environment
variables](../reference/env-vars.md).  Highlights:

- `BLACKBULL_ENV` — `development` (default) / `production` /
  `test`.  Controls the [error handler](error-handling.md)
  posture and whether [static files](static-files.md) are
  served.
- `BB_WORKERS` — pre-fork worker count (`0` resolves to
  `os.cpu_count()`).
- `BB_ACCESS_LOG` — toggle the access log (`1` on, `0` off).
- `BB_UVLOOP` — install `uvloop`'s asyncio policy.
- `BB_SESSION_SECRET` — HMAC secret for the `Session`
  middleware (no insecure default — must be set explicitly).

## TOML config file

For larger deployments, write a TOML file and point the CLI at it:

```bash
blackbull myapp:app --config /etc/blackbull/config.toml
```

```toml title="/etc/blackbull/config.toml"
[server]
workers = 4
uvloop  = true
keep_alive_timeout = 5

h2_max_concurrent_streams = 200
h2_enable_websocket       = false

[limits]
max_connections = 1000
request_timeout = 30
header_timeout  = 10
compression_min_size = 100

[logging]
access_log    = true
async_logging = true

[tls]
cert = "/etc/blackbull/cert.pem"
key  = "/etc/blackbull/key.pem"
```

Section + key names mirror the `BB_*` environment-variable
naming (lower-cased, the `BB_` prefix dropped).  Unknown
sections and keys are silently ignored, so future TOML keys
don't break older binaries.

The TLS keys (`[tls] cert`, `[tls] key`) are CLI-arg fallbacks
applied to `--certfile` / `--keyfile` when those aren't
specified on the command line.

## CLI flags

The `blackbull` console script (and `app.run(...)` in code)
both expose the most-touched knobs as direct flags:

```bash
blackbull myapp:app \
    --bind 0.0.0.0:8443 \
    --certfile cert.pem --keyfile key.pem \
    --workers 4 \
    --max-connections 1000 \
    --stream-queue-depth 128
```

See `blackbull --help` for the complete list.  Anything not
exposed as a flag can still be set via the corresponding
`BB_*` environment variable.

## Precedence in detail

For each knob, the resolved value is the **first** source that
specifies it:

1. **CLI flag** — `--workers 4` wins over both env and TOML.
2. **Environment variable** — `BB_WORKERS=4` wins over TOML.
3. **TOML config file** — `[server] workers = 4` wins over the
   default.
4. **Built-in default** — listed in
   [Reference — Environment variables](../reference/env-vars.md).

The implementation hook is `os.environ.setdefault(...)` — TOML
values are only written into the environment when the variable
isn't already present, so existing env vars always win.  CLI
flags then trump both.

## In-code configuration

When embedding BlackBull (not running via the CLI), `app.run()`
accepts the same parameters as keyword arguments:

```python
app.run(
    port=8443,
    certfile='cert.pem', keyfile='key.pem',
    workers=4,
    max_connections=1000,
    stream_queue_depth=128,
)
```

Keyword arguments take precedence over environment variables in
the same way the CLI flags do.

## Operational defaults to think about

The default values are tuned for development.  For production,
the knobs most worth a second look:

| Variable | Dev default | Production guidance |
|---|---|---|
| `BLACKBULL_ENV` | `development` | Set to `production`.  Tightens error responses and stops `StaticFiles` from competing with your reverse proxy. |
| `BB_WORKERS` | `1` | Set to `0` (= `cpu_count()`) or a fixed integer matching your CPU budget. |
| `BB_REQUEST_TIMEOUT` | `0` (off) | Set to a positive value (e.g. `30`) so stalled handlers get evicted. |
| `BB_MAX_CONNECTIONS` | `500` | Tune for the worker's memory budget; set `0` only if some upstream caps connection counts. |
| `BB_SESSION_SECRET` | unset | Must be set (or pass `secret=` to `Session`).  Generate with `python -c "import secrets; print(secrets.token_urlsafe(32))"`. |
| `BB_UVLOOP` | `0` | Set to `1` for a typical 1.5-2× throughput improvement on HTTP/2 hot paths. |

## Next

- [Reference — Environment variables](../reference/env-vars.md)
  — exhaustive table of every `BB_*` knob.
- [Logging](logging.md) — `BB_ACCESS_LOG` and `BB_ASYNC_LOGGING`
  in action.
- [Error handling](error-handling.md) — `BLACKBULL_ENV`'s effect
  on the default error response.
