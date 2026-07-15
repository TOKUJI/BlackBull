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
log_format    = "json"        # "" (plain, default) or "json"
syslog_addr   = "127.0.0.1:514"  # ship logs via UDP syslog (optional)
batch_size    = 64            # async logging always batches; this is the coalescing width
batch_timeout_ms = 5          # max ms a partial batch waits before flush
file          = "/var/log/blackbull/access.log"  # write to a file instead of stderr (optional)

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

### Declarative startup with `AppConfig`

To declare the startup settings once — rather than threading them
through every `run()` call site — build the app with an
`AppConfig`:

```python
from blackbull import BlackBull, AppConfig

app = BlackBull(config=AppConfig(
    port=8443,
    certfile='cert.pem', keyfile='key.pem',
    workers=4,
))

if __name__ == '__main__':
    app.run()                 # picks up the bound config
```

`AppConfig` is a frozen dataclass holding exactly the parameters
`run()` accepts (`port`, `certfile`, `keyfile`, `unix_path`,
`inherited_fd`, `workers`, `max_connections`, `stream_queue_depth`,
`ws_queue_depth`, `reload`, `reload_paths`).  It is not a
general-purpose settings store — server-tuning knobs stay in the
`BB_*` environment variables.

Resolution order in `run()` is, highest to lowest:

1. an explicit `run(...)` keyword argument — `app.run(port=9000)`
   always wins;
2. a `BLACKBULL_*` environment variable, for the deploy-time
   settings (`BLACKBULL_PORT`, `BLACKBULL_CERT`, `BLACKBULL_KEY`,
   `BLACKBULL_UNIX_PATH`, `BLACKBULL_RELOAD`);
3. the same `BLACKBULL_*` key in a `.env` file in the working
   directory (needs the `[dotenv]` extra — see below);
4. the value declared on the bound `AppConfig`;
5. `serve()`'s built-in default.

```python
app = BlackBull(config=AppConfig(port=8443, certfile='c.pem', keyfile='k.pem'))
app.run()              # binds 8443 with TLS from the config
app.run(port=9000)     # explicit arg overrides the config's 8443
```

```bash
# Same app.py, no code change — the env var overrides the config's 8443:
BLACKBULL_PORT=9000 BLACKBULL_CERT=/etc/ssl/c.pem BLACKBULL_KEY=/etc/ssl/k.pem \
  python app.py
```

`BLACKBULL_*` is the **deployment** namespace (the handful of
settings you change per environment); server-tuning knobs
(`workers`, `max_connections`, queue depths, timeouts) keep their
`BB_*` variables — they are not duplicated under `BLACKBULL_*`. The
provenance of each non-default deploy setting is logged once at
startup on the `blackbull.config` logger, e.g.
`config: port=9000 (from $BLACKBULL_PORT)`.

There is no `host` field / `BLACKBULL_HOST`: BlackBull's socket
layer binds dual-stack on all interfaces, so a per-interface `host`
would silently do nothing.  Use `unix_path` or `inherited_fd` for
non-TCP binds.

#### `.env` files

Install the optional extra to let `app.run()` (and the `blackbull`
CLI) read `BLACKBULL_*` values from a `.env` file in the working
directory:

```bash
pip install 'blackbull[dotenv]'
```

```bash
# .env
BLACKBULL_PORT=8443
BLACKBULL_CERT=/etc/ssl/cert.pem
BLACKBULL_KEY=/etc/ssl/key.pem
```

The real process environment always wins over `.env` (so a
`docker run -e BLACKBULL_PORT=9000` overrides the file). Without the
extra, `.env` files are ignored and only the real environment is
consulted — `BLACKBULL_*` resolution still works.

## Serving static files with `blackbull serve`

For a quick static file server — a drop-in upgrade over
`python -m http.server` — point the `serve` subcommand at a
directory:

```bash
blackbull serve                       # serve ./ on http://127.0.0.1:8000
blackbull serve ./public --bind :8080
blackbull serve ./public --certfile cert.pem --keyfile key.pem  # HTTPS + HTTP/2
```

Unlike `python -m http.server`, it ships:

- **ETag / conditional requests** — repeat fetches get a `304 Not
  Modified` (disable with `--no-etag`);
- **HTTP/2** automatically once `--certfile` / `--keyfile` make it
  HTTPS;
- a **directory index** (`index.html` by default; change with
  `--index NAME`, disable with `--index ''`);
- precompressed-sibling negotiation (`.br` / `.zst` / `.gz`).

No application code is required.  `--cache` holds file bodies in an
in-memory LRU for higher throughput at the cost of picking up
on-disk edits only after the per-entry `stat` TTL.

!!! note
    `blackbull serve` is a development / standalone convenience.
    `StaticFiles` does not serve files when `BLACKBULL_ENV=production`
    (the expectation is a reverse proxy or CDN fronts static traffic
    there) — see [Static files](static-files.md).

## Operational defaults to think about

The default values are tuned for development.  For production,
the knobs most worth a second look:

| Variable | Dev default | Production guidance |
|---|---|---|
| `BLACKBULL_ENV` | `development` | Set to `production`.  Tightens error responses and stops `StaticFiles` from competing with your reverse proxy. |
| `BB_WORKERS` | `1` | Set to `0` (= `cpu_count()`) or a fixed integer matching your CPU budget. |
| `BB_REQUEST_TIMEOUT` | `0` (off) | Set to a positive value (e.g. `30`) so stalled handlers get evicted. |
| `BB_MAX_CONNECTIONS` | `500` | Tune for the worker's memory budget; set `0` only if some upstream caps connection counts. |
| `BB_UVLOOP` | `0` | Set to `1` for a typical 1.5-2× throughput improvement on HTTP/2 hot paths. |

## Next

- [Reference — Environment variables](../reference/env-vars.md)
  — exhaustive table of every `BB_*` knob.
- [Logging](logging.md) — `BB_ACCESS_LOG` and `BB_ASYNC_LOGGING`
  in action.
- [Error handling](error-handling.md) — `BLACKBULL_ENV`'s effect
  on the default error response.
