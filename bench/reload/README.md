# Startup and reload diagnostics

`probe.py` launches a temporary BlackBull app and records fresh HTTP/1.1
connections during startup, in steady state, and optionally across reload.
It replaces separate burst, cold-stream, timeline and process-log scripts
with one harness. It uses the standard library and the checkout's BlackBull
dependencies, including `watchfiles` for reload. Run on a POSIX host.

From the repository root:

```sh
uv sync --all-extras
mkdir -p bench/results/reload

# Concurrent arrivals during an artificial six-second startup delay.
uv run python bench/reload/probe.py > bench/results/reload/cold.json

# Reload with five idle keep-alive connections; use --active-warm to
# keep requesting on them until the old worker drains.
uv run python bench/reload/probe.py --reload > bench/results/reload/reload.json

# Schedule independent arrivals every 250 ms instead of one concurrent burst.
uv run python bench/reload/probe.py --reload --warm 0 --interval .25 --requests 80 \
  > bench/results/reload/timeline.json
```

Use `--source /absolute/path/to/checkout` to test another checkout with the
current interpreter. The target must support
`app.run(port=..., unix_path=..., reload_paths=...)`; its dependencies must be installed in that
interpreter. The source path is resolved before the server changes directory.
`--family inet|inet6|unix` selects the client destination: IPv4 loopback,
IPv6 loopback or a temporary Unix socket. TCP uses the server's wildcard
listener via `app.run(port=...)`, also reachable through other interfaces;
use Unix mode when you need a local socket. `--backlog` sets `BB_SOCKET_BACKLOG`.

`--park` delays startup in both generations. `--warm` controls held connections
for reload; `--active-warm` waits 200 ms after each response before sending the
next request on each held connection.
`--client-timeout` bounds connecting and each HTTP exchange separately.
`--budget` bounds readiness and generation verification, with at most one
in-flight request beyond that deadline. A sample phase takes at most
`(requests - 1) * interval + 2 * client-timeout`, plus scheduling overhead.
The harness terminates its server process group on success, failure or interruption.

Each record includes scheduled arrival, actual start (`t`), completion and
fate; successful records also include latency and the response generation.
Times are seconds relative to the start of that phase; reload starts timing
before the held-connection watchers are submitted and the file is rewritten.
`served-fast` requires HTTP 200, the complete expected
body and latency below `--slow` (default 500 ms). Other fates count as affected.
The steady-state control uses the same request count and arrival interval.
Any affected control request makes `valid` false and the exit status nonzero.
Reload also requires a complete response from the new generation on a fresh
connection. Held connection results distinguish observed EOF/errors from
harness shutdown (`stopped`) and client timeout.

`server_log_tail` retains up to 64 KiB of subprocess output for diagnosis.
Generated apps, logs and socket paths use a unique temporary directory which
is removed after teardown. Redirect JSON to `bench/results/` to keep local
measurements out of Git.

These are sampled observations, not an exact outage duration or a throughput
benchmark. A burst can reach the old worker before the watcher reacts, and
successful generation verification can happen after the sampled arrivals.
Use a sufficiently long interval stream to cover the transition. Artificial
startup delays, client concurrency and active keep-alive requests affect the
server being measured; record the configuration when comparing checkouts.
