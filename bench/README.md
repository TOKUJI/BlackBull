# bench/ — index

Start here before measuring anything.  Find your question below and use its
tool; extend an existing tool rather than writing a second one for the same
question.  A new tool is added to this index, given a test, and run in CI
(see *Keeping it maintained*).

- **Output**: every tool writes only to its run directory,
  `bench/results/<tool>/<UTC>-<rev>/`, through `bench/lib/` ([details](results/README.md)) —
  never `/tmp`, the repository root or `$HOME`.
- **Evidence**: nothing under `bench/results/` is committed.  Attach the files
  a decision rests on to its YouTrack issue (`just yt-attach ISSUE FILE`) and
  summarise them in a comment.  Documents here keep methods, not numbers.
- **Cloud**: measurements on EC2 run as one session, `bench/cloud/session.sh`,
  and only with approval (`APPROVE_CLOUD=1`); see [cloud/README.md](cloud/README.md).

## Which tool answers your question

| Question | Start with | Read |
|---|---|---|
| Did my commit change throughput? | `bench/ab/ab_commit.sh` (HTTP/1.1, wrk), `bench/ab/ab_commit_h2.sh` (HTTP/2, h2load), `bench/ab/ab_commit_ws.sh` (WebSocket, k6); on EC2 `just ab-verify` | [ab/AB-HIGH-PRECISION.md](ab/AB-HIGH-PRECISION.md) |
| How fast is BlackBull against peer servers? | `just bench-compare` (`bench/compare/compare_servers.sh`); on EC2 `bench/cloud/session.sh compare` | [compare/README.md](compare/README.md) |
| Where does BlackBull stand on HttpArena? | `just httparena-bench`; correctness first with `bench/httparena/validate_local.sh` | [httparena/README.md](httparena/README.md) |
| How does one server behave under HTTP/2 multiplexing and k6 load? | `bench/ab/h2load/benchmark.sh` | [ab/h2load/README.md](ab/h2load/README.md) |
| What does one piece cost — parser, router, middleware, bytecode? | `bench/micro/` instruments | [micro/README.md](micro/README.md) |
| Did TestClient lifecycle or request overhead change? | `bench/micro/testing_client.py --base PATH --head PATH` | [micro/README.md](micro/README.md) |
| Where does the time go under load? | `bench/micro/profile_under_load.sh` | [micro/README.md](micro/README.md) |
| Did the hot path gain event-loop touches? (CI gate) | `bench/micro/loop_touches.py --check` | [micro/README.md](micro/README.md) |
| Is the protocol implementation conformant? | `bench/conformance/h2spec_run.sh`, `bench/conformance/autobahn_run.sh`, `just http11probe` | [conformance/README.md](conformance/README.md) |
| Does the server stay responsive under hostile clients? | `bench/probes/hostile/probe.sh` | [probes/hostile/README.md](probes/hostile/README.md) |
| How do connections fare across startup and reload? | `bench/probes/reload/probe.py` | [probes/reload/README.md](probes/reload/README.md) |
| Does memory or FD use grow over an hour? | `bench/probes/soak/soak.sh` | header of the script |
| Static-file serving: latency, drain, reproduction | `bench/probes/static/run.sh`, `bench/probes/static/latency_probe.sh`, `bench/probes/static/drain_time.sh` | headers of the scripts |
| MQTT tap throughput | `bench/probes/mqtt/tap_throughput.py` | `--help` |

## Lessons that govern the numbers

Each is recorded once, where linked:

- Executed-instruction counts are lower bounds on cost — [micro/README.md](micro/README.md).
- A local WSL2 box is bimodal and can only screen; EC2 decides — [ab/AB-HIGH-PRECISION.md](ab/AB-HIGH-PRECISION.md) §2.
- A real Δ means nothing until it clears the session's own null phase — [ab/AB-HIGH-PRECISION.md](ab/AB-HIGH-PRECISION.md) §3, §7.
- A micro-benchmark gain reaches throughput only in proportion to the path's profile share — [CHARACTERIZATION.md](CHARACTERIZATION.md), cascade-multiplier rule.

## Every file

Entry points are in **bold**; everything else is a part of the tool on its row.

### `bench/ab/` — A/B of two commits

| File | Role |
|---|---|
| **`bench/ab/ab_commit.sh`**, **`bench/ab/ab_commit_h2.sh`**, **`bench/ab/ab_commit_ws.sh`** | ABBA A/B on HTTP/1.1 (wrk), HTTP/2 (h2load), WebSocket (k6) |
| `bench/ab/abba.sh` | their shared core: swap, import proof, schedule, run directory |
| **`bench/ab/report.py`** | verdict for a `raw.tsv`: paired / per-mode Δ, bootstrap CI, null floor |
| `bench/ab/static_ab.js` | a static asset the A/B lanes serve (e.g. `URL_PATH=/static/static_ab.js`) |
| **`bench/ab/response_wire.py`**, **`bench/ab/hpack_encode.py`** | in-process ABBA of HTTP/2 response builders across two worktrees |
| **`bench/ab/h2load/benchmark.sh`** | one server under h2load + k6; `bench/ab/h2load/h2load_run.sh`, `bench/ab/h2load/k6_run.sh`, `bench/ab/h2load/summarize.py` are its stages |
| `bench/ab/h2load/app.py`, `bench/ab/h2load/lag_monitor.py` | the HTTP/2 bench app and its event-loop lag monitor |

### `bench/apps/` — apps under test

| File | Role |
|---|---|
| `bench/apps/native_app.py`, `bench/apps/asgi_app.py` | BlackBull, native and ASGI forms |
| `bench/apps/fastapi_app.py`, `bench/apps/quart_app.py`, `bench/apps/starlette_app.py`, `bench/apps/sanic_app.py` | the same routes on peer frameworks |
| `bench/apps/gate_stamp.py`, `bench/apps/gc_stats.py`, `bench/apps/loop_stamp.py`, `bench/apps/response_timing.py`, `bench/apps/no_events.py` | opt-in instruments the apps load from environment variables |

### `bench/compare/` — BlackBull against peer servers

| File | Role |
|---|---|
| **`bench/compare/compare_servers.sh`** | the peer matrix (lanes A–E, stacks) into one report |
| `bench/compare/run_peer.sh`, `bench/compare/server_lifecycle_remote.sh`, `bench/compare/scenario_capture.sh` | start one stack; the same over ssh (split topology); per-scenario CPU capture |
| `bench/compare/summarize.py` | the report's side-by-side summary |
| `bench/compare/nginx.conf`, `bench/compare/nginx_proxy.conf`, `bench/compare/hypercorn.toml` | peer configurations |

### `bench/cloud/` — EC2 sessions

| File | Role |
|---|---|
| **`bench/cloud/session.sh`** | one session: provision → run → pull → verified teardown |
| `bench/cloud/jobs/ab.sh`, `bench/cloud/jobs/client-ab.sh`, `bench/cloud/jobs/compare.sh`, `bench/cloud/jobs/httparena.sh` | the jobs a session runs |
| `bench/cloud/remote.sh`, `bench/cloud/config.sh` | detached launch, resilient poll, pull, git deploy; settings |
| `bench/cloud/up.sh`, `bench/cloud/install.sh`, `bench/cloud/down.sh` | provision, install, verified teardown |

### `bench/conformance/` — protocol conformance

| File | Role |
|---|---|
| **`bench/conformance/h2spec_run.sh`** | h2spec against `bench/conformance/h2spec_app.py` |
| **`bench/conformance/autobahn_run.sh`**, **`bench/conformance/autobahn_heavy.sh`** | Autobahn (fast lane; heavy lane in batches) against `bench/conformance/autobahn_app.py` |
| `bench/conformance/autobahn_assert.sh`, `bench/conformance/autobahn_cases.py`, `bench/conformance/autobahn_common.sh` | verdict gate, case manifests, the pinned tester image |
| **`bench/conformance/repro_autobahn_close.py`** | reproduces one close-handshake case without the tester |

### `bench/httparena/` — HttpArena

| File | Role |
|---|---|
| **`bench/httparena/validate_local.sh`** | HttpArena's validate.sh locally against a wheel (`bench/httparena/build_wheel.sh`) |
| `bench/httparena/run_httparena.sh`, `bench/httparena/ready_check.sh` | run on the instance by the `httparena` cloud job |
| `bench/httparena/app.py`, `bench/httparena/launcher.py`, `bench/httparena/db.py`, `bench/httparena/grpc_bench.py`, `bench/httparena/Dockerfile`, `bench/httparena/Dockerfile.dev` | the BlackBull entry vendored into HttpArena |
| `bench/httparena/sanic/app.py`, `bench/httparena/sanic/launcher.py`, `bench/httparena/sanic/build.sh`, `bench/httparena/sanic/Dockerfile` | this repo's sanic entry (endpoint parity, not evidence about sanic) |
| `bench/httparena/aiohttp/app.py`, `bench/httparena/aiohttp/launcher.py`, `bench/httparena/aiohttp/build.sh`, `bench/httparena/aiohttp/Dockerfile` | this repo's aiohttp entry (likewise) |
| `bench/httparena/install_docker_shim.sh`, `bench/httparena/patch_cpuset.sh`, `bench/httparena/patch_httparena.py`, `bench/httparena/patch_wsl2_docker.sh` | harness patches: docker tuning shim, small-instance cpusets, env knobs, WSL2 mounts |
| **`bench/httparena/compare_table.py`** | COMPARISON.md from a run's HttpArena results |

### `bench/lib/` — shared code

| File | Role |
|---|---|
| `bench/lib/results.py`, `bench/lib/results.sh` | the run directory every tool writes to |

### `bench/load/` — load generators

| File | Role |
|---|---|
| **`bench/load/install.sh`** | installs h2load, wrk, wrk2, oha, k6 and the peer servers |
| `bench/load/wrk/run.sh`, `bench/load/wrk/lane_e.sh`, `bench/load/wrk2/run.sh`, `bench/load/oha/run.sh`, `bench/load/wrk/_stats.py` | lane drivers used by `compare_servers.sh` |
| `bench/load/wrk/pipeline.lua`, `bench/load/wrk/post_echo.lua`, `bench/load/wrk/no_keepalive.lua`, `bench/load/wrk/httparena_baseline.lua`, `bench/load/wrk/httparena_json_comp.lua` | wrk scripts |
| `bench/load/k6/http_rampup.js`, `bench/load/k6/http_stress.js`, `bench/load/k6/websocket.js`, `bench/load/k6/websocket_echo_throughput.js` | k6 scripts |

### `bench/micro/` — what one piece costs

| File | Role |
|---|---|
| **`bench/micro/count_instructions.py`** | executed bytecode instructions per request |
| **`bench/micro/testing_client.py`** | TestClient A/A and ABBA, bytecode comparison and executed helper instructions |
| **`bench/micro/loop_touches.py`** | event-loop touches per request; `--check` is the CI budget |
| **`bench/micro/parser_microbench.py`**, **`bench/micro/router_microbench.py`**, **`bench/micro/middleware_stack.py`**, **`bench/micro/streams_vs_protocol.py`**, **`bench/micro/grpc_bridge.py`** | parser, router, middleware, transport, gRPC bridge |
| **`bench/micro/profile_under_load.sh`** | py-spy flame graph under k6 stress |
| **`bench/micro/hotpath/line_cache_hitrate.py`**, **`bench/micro/hotpath/line_cache_churn.py`**, **`bench/micro/hotpath/line_cache_miss.py`**, **`bench/micro/hotpath/line_cache_realistic.py`**, **`bench/micro/hotpath/line_cache_worst_case.py`** | the header-line cache: hit rate, break-even, worst case |
| `bench/micro/hotpath/capture_browser_headers.py`, `bench/micro/hotpath/provenance.py` | captured browser traffic for the above; their environment stamp |

### `bench/probes/` — behaviour under specific conditions

| File | Role |
|---|---|
| **`bench/probes/hostile/probe.sh`**, **`bench/probes/hostile/characterize_slowloris.py`** | responsiveness under attack; slowloris characterisation |
| `bench/probes/hostile/attacks/idle_park.py`, `bench/probes/hostile/attacks/slow_read.py`, `bench/probes/hostile/attacks/slowloris_body.py`, `bench/probes/hostile/attacks/slowloris_headers.py` | the attacks |
| **`bench/probes/reload/probe.py`** | connections across startup and reload |
| **`bench/probes/soak/soak.sh`** | one-hour soak; `bench/probes/soak/sample.py` samples, `bench/probes/soak/mixed.lua` loads |
| **`bench/probes/static/run.sh`**, **`bench/probes/static/latency_probe.sh`**, **`bench/probes/static/drain_time.sh`** | static serving: reproduction, latency from the access log, drain |
| `bench/probes/static/server_probe.py`, `bench/probes/static/access_log_server.py`, `bench/probes/static/parse_access_log.py`, `bench/probes/static/rotate.lua` | their server wrappers, log parser, wrk script |
| `bench/probes/static/stats_access_log.py` | phase-trace stats (p50/p99 per phase) for `BB_PHASE_TRACE=1` access logs |
| **`bench/probes/http11probe/probe_scan.py`** | local stand-in for Http11Probe: fires its vectors at a live server (the pinned probe itself: `just http11probe`) |
| **`bench/probes/mqtt/tap_throughput.py`** | MQTT tap throughput |

### `scripts/` — the workflow entry points the `just` recipes call

| File | Role |
|---|---|
| `scripts/run-ab-verify.sh`, `scripts/run-httparena-bench.sh` | `just ab-verify`, `just httparena-bench` (cloud; approval gate) |
| `scripts/run-bench-compare.sh`, `scripts/run-http11probe.sh` | `just bench-compare`, `just http11probe` |

## Keeping it maintained

- `tests/architecture/test_bench_tools.py` — every tool is in this index, every indexed path exists, every tool parses or compiles, every Python command line answers `--help`.
- `tests/architecture/test_bench_outputs.py` — no `/tmp`, no results directory but the helper's, nothing committed under `bench/results/`, no loose files in `bench/`.

Both run in CI's fast tier.
