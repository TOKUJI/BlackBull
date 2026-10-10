# BlackBull development commands — run with `just` (https://github.com/casey/just)
# Install: uv tool install just

# Install all dependencies including optional extras
install:
    uv sync --all-extras

# Run the full test suite
test:
    uv run pytest -q -n auto

# Type-check with beartype instrumentation
typecheck:
    uv run pytest --beartype-packages=blackbull --timeout=30 -q --tb=short -n auto

# Build and serve docs locally
docs:
    DISABLE_MKDOCS_2_WARNING=true uv run mkdocs serve

# Build docs strictly (CI mode)
docs-build:
    DISABLE_MKDOCS_2_WARNING=true uv run mkdocs build --strict

# Run the pinned HTTP/1.1 probe in native, compatibility, or both lanes
http11probe lane='both':
    scripts/run-http11probe.sh --lane "{{lane}}"

# Run the repository-authoritative local peer comparison
bench-compare:
    scripts/run-bench-compare.sh

# Run one approved A/B cloud lifecycle, including teardown
ab-verify:
    scripts/run-ab-verify.sh

# Run one approved HTTPArena cloud lifecycle, including teardown
httparena-bench:
    scripts/run-httparena-bench.sh

# BLA-526 probe target (loopback only): HTTP/1.1 on 8000 and TLS+ALPN h2 on 8443 in one process; PID, log, and the generated TLS cert live in the per-user private runtime dir (tools/security/paths.py) — see docs/security/fixture-app.md
vuln-target-up:
    #!/usr/bin/env bash
    set -euo pipefail
    pid_file=$(.venv/bin/python tools/security/paths.py pid)
    log=$(.venv/bin/python tools/security/paths.py log)
    healthy() {
        .venv/bin/python tools/security/fixture_app.py --health >/dev/null 2>&1
    }
    port_held() {
        (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null
    }
    if [ -f "$pid_file" ] && kill -0 "$(cat "$pid_file")" 2>/dev/null; then
        if healthy; then
            echo "vuln-target already running (pid $(cat "$pid_file")): http://127.0.0.1:8000 + https://127.0.0.1:8443"
            exit 0
        fi
        echo "vuln-target: pid $(cat "$pid_file") is alive but the fixture does not serve both lanes; refusing" >&2
        exit 1
    fi
    for port in 8000 8443; do
        if port_held "$port"; then
            echo "vuln-target: port $port is already in use by another process; refusing to start" >&2
            exit 1
        fi
    done
    # Starting the target must not mutate the environment (review L3): fail
    # with a clear message instead of re-running `uv sync` behind the user.
    if ! .venv/bin/python -c 'import blackbull' 2>/dev/null; then
        echo "vuln-target: .venv is not ready (cannot import blackbull); run 'just install' first" >&2
        exit 1
    fi
    # Not `uv run`: uv spawns the interpreter as a child, and the PID file
    # must name the server process itself.
    nohup .venv/bin/python tools/security/fixture_app.py --port 8000 --tls-port 8443 >>"$log" 2>&1 &
    echo "$!" >"$pid_file"
    for _ in $(seq 1 50); do
        if healthy; then
            echo "vuln-target up (pid $(cat "$pid_file")): http://127.0.0.1:8000 + https://127.0.0.1:8443"
            exit 0
        fi
        if ! kill -0 "$(cat "$pid_file")" 2>/dev/null; then
            break
        fi
        sleep 0.2
    done
    echo "vuln-target did not serve both lanes within 10s; last log lines:" >&2
    tail -n 20 "$log" >&2 || true
    kill "$(cat "$pid_file")" 2>/dev/null || true
    rm -f "$pid_file"
    exit 1

vuln-target-down:
    #!/usr/bin/env bash
    set -euo pipefail
    pid_file=$(.venv/bin/python tools/security/paths.py pid)
    if [ ! -f "$pid_file" ]; then
        echo "vuln-target: no PID file; nothing to stop"
        exit 0
    fi
    pid="$(cat "$pid_file")"
    if kill -0 "$pid" 2>/dev/null; then
        case "$(ps -p "$pid" -o args= 2>/dev/null || true)" in
            *fixture_app*) ;;
            *)
                echo "vuln-target: pid $pid is not the fixture app; refusing to kill it" >&2
                rm -f "$pid_file"
                exit 1
                ;;
        esac
        kill "$pid" 2>/dev/null || true
        for _ in $(seq 1 50); do
            kill -0 "$pid" 2>/dev/null || break
            sleep 0.2
        done
        if kill -0 "$pid" 2>/dev/null; then
            kill -9 "$pid" 2>/dev/null || true
        fi
    fi
    rm -f "$pid_file"
    rm -rf "$(.venv/bin/python tools/security/paths.py tls)"
    for port in 8000 8443; do
        if (exec 3<>"/dev/tcp/127.0.0.1/$port") 2>/dev/null; then
            exec 3>&- || true
            echo "vuln-target: port $port is still accepting connections" >&2
            exit 1
        fi
    done
    echo "vuln-target down; ports 8000 and 8443 free"

# Run the BLA-526 robustness probe against running targets (both lanes)
vuln-check base_url="http://127.0.0.1:8000" h2_url="https://127.0.0.1:8443" lane="all" tier="quick":
    uv run python tools/security/probe.py --base-url "{{base_url}}" --h2-url "{{h2_url}}" --lane "{{lane}}" --tier "{{tier}}"

# G2-1 gate: every check against a do-nothing peer must earn zero PASS
vuln-stub-gate:
    uv run python tools/security/silent_stub.py

# G2-2 gate: checks after a mid-run server death must record canary failures
vuln-canary-gate:
    uv run python tools/security/silent_stub.py --dying

# G2-3 gate: a stub that holds every accepted socket must show residuals
vuln-proc-gate:
    uv run python tools/security/silent_stub.py --proc-gate

# G2-4: container tier — limited server container + sibling probe container
# (shared net/pid namespaces, no published ports), verdict parity with native
vuln-container:
    #!/usr/bin/env bash
    set -euo pipefail
    out=bench/results/security
    mkdir -p "$out"
    uv run python tools/security/container_run.py --verdicts-out "$out/container.verdicts"
    just vuln-target-up >/dev/null
    just vuln-check > "$out/container-native.txt" 2>&1 || true
    just vuln-target-down >/dev/null
    grep -E '^(BASELINE|H1-|SMUGGLE|CHUNK|TRAILER|STATE|RANGE|EXPECT|HOST|STATIC|SYMLINK|WS|HDR|H2-|TLS)' \
        "$out/container-native.txt" | awk '{print $1 "=" $3}' | sort > "$out/container-native.verdicts"
    if diff -q "$out/container-native.verdicts" "$out/container.verdicts" >/dev/null; then
        echo "G2-4 OK: container verdicts match the native run ($(wc -l < "$out/container.verdicts") rows)"
    else
        echo "G2-4 DIFF: container vs native verdicts:"
        diff "$out/container-native.verdicts" "$out/container.verdicts" || true
        exit 1
    fi

# G2-4 detector verification: a tiny --memory must be detected as an OOM
# (24m already serves the fixture at rest; 12m makes the kernel OOM-kill it)
vuln-container-oom mem="12m":
    uv run python tools/security/container_run.py --expect-oom --memory {{mem}}

# G7: quick tier across the configuration matrix (uvloop on/off x 1/2 workers)
# plus the explicit-caps operational config; verdicts must agree (G7-1/G7-2).
vuln-matrix:
    #!/usr/bin/env bash
    set -euo pipefail
    out=bench/results/security
    mkdir -p "$out"
    run_cfg() {
        local cfg="$1"; shift
        env "$@" just vuln-target-up >/dev/null
        just vuln-check > "$out/matrix-$cfg.txt" 2>&1 || true
        just vuln-target-down >/dev/null
        grep -E '^(BASELINE|LANE|H1-|SMUGGLE|CHUNK|TRAILER|STATE|RANGE|EXPECT|HOST|STATIC|SYMLINK|WS|HDR|H2-|TLS)' \
            "$out/matrix-$cfg.txt" | awk '{print $1 "=" $3}' | sort > "$out/matrix-$cfg.verdicts"
        echo "matrix $cfg: $(wc -l < "$out/matrix-$cfg.verdicts") verdict rows"
    }
    run_cfg uvloop0-workers1 BB_UVLOOP=0 BB_WORKERS=1
    run_cfg uvloop1-workers1 BB_UVLOOP=1 BB_WORKERS=1
    run_cfg uvloop0-workers2 BB_UVLOOP=0 BB_WORKERS=2
    run_cfg uvloop1-workers2 BB_UVLOOP=1 BB_WORKERS=2
    # G7-2: operational config with explicit caps — record the result
    run_cfg explicit-caps BB_MAX_CONNECTIONS=8 BB_REQUEST_TIMEOUT=30
    base="$out/matrix-uvloop0-workers1.verdicts"
    status=0
    for cfg in uvloop1-workers1 uvloop0-workers2 uvloop1-workers2; do
        if diff -q "$base" "$out/matrix-$cfg.verdicts" >/dev/null; then
            echo "G7-1 OK: $cfg matches uvloop0-workers1"
        else
            echo "G7-1 DIFF ($cfg vs uvloop0-workers1):"
            diff "$base" "$out/matrix-$cfg.verdicts" || true
            status=1
        fi
    done
    exit $status

# --- G3-3: defense-site reachability (E1 AST method) -------------------
# Extract defense sites from blackbull/ via AST, run the probe against a
# fixture under branch coverage, and report which sites the runs reached.

vuln-reachability tier="quick":
    #!/usr/bin/env bash
    set -euo pipefail
    export UV_CACHE_DIR=$PWD/.uv-cache XDG_RUNTIME_DIR=/tmp/xdg-runtime
    rm -f .coverage .coverage.*
    mkdir -p bench/results/security /tmp/bla526-research
    # every preforked fixture worker measures itself (covproc/sitecustomize.py);
    # the env stays scoped to the fixture so probe-side code is not counted
    (setsid env PYTHONPATH=$PWD/tools/security/covproc \
        COVERAGE_PROCESS_START=$PWD/.coveragerc-reach \
        uv run python tools/security/fixture_app.py --port 8123 \
        --tls-port 8444 > /tmp/bla526-research/reach-fixture.log 2>&1 &)
    for _ in $(seq 1 40); do
        uv run python -c "import socket; socket.create_connection(('127.0.0.1', 8123), 1).close()" 2>/dev/null && break
        sleep 0.5
    done
    uv run python tools/security/probe.py --base-url http://127.0.0.1:8123 \
        --h2-url https://127.0.0.1:8444 --tier {{tier}} \
        > bench/results/security/reachability-probe.txt 2>&1 || true
    pgrep -f "fixture_app.py --port 8123" | xargs -r kill -INT
    sleep 2
    uv run coverage combine || true
    uv run coverage json -o /tmp/bla526-research/coverage.json
    uv run python tools/security/reachability.py \
        --coverage-json /tmp/bla526-research/coverage.json \
        | tee bench/results/security/reachability.txt

# --- M5-6: existing Atheris harnesses, time-bounded -------------------
# The committed corpora (tests/conformance/http{1,2}/fuzz/corpus) are the
# seeds; fuzz-seed re-emits opcode-tagged seeds for the current Scenario
# codec.  Each run is capped at $t seconds of fuzzing inside an outer
# timeout, and crash artifacts land in /tmp so the worktree stays clean.

fuzz-seed:
    #!/usr/bin/env bash
    set -euo pipefail
    export UV_CACHE_DIR=$PWD/.uv-cache XDG_RUNTIME_DIR=/tmp/xdg-runtime
    (cd tests/conformance/http1/fuzz && uv run python make_seeds.py)
    (cd tests/conformance/http2/fuzz && uv run python make_seeds.py)
    echo "seed corpora refreshed"

fuzz-http1 t="60":
    #!/usr/bin/env bash
    set -euo pipefail
    export UV_CACHE_DIR=$PWD/.uv-cache XDG_RUNTIME_DIR=/tmp/xdg-runtime
    if ! uv run python -c 'import atheris' 2>/dev/null; then
        echo "atheris is not installed: uv pip install atheris (docs/security/probe.md, G6)"
        exit 3
    fi
    mkdir -p /tmp/bla526-fuzz-http1
    timeout -k 5 $(( {{t}} + 30 )) uv run python tools/security/fuzz_run.py http1 \
        -max_total_time={{t}} -artifact_prefix=/tmp/bla526-fuzz-http1/ corpus/ || [ $? -eq 124 ]

fuzz-http2 t="60":
    #!/usr/bin/env bash
    set -euo pipefail
    export UV_CACHE_DIR=$PWD/.uv-cache XDG_RUNTIME_DIR=/tmp/xdg-runtime
    if ! uv run python -c 'import atheris' 2>/dev/null; then
        echo "atheris is not installed: uv pip install atheris (docs/security/probe.md, G6)"
        exit 3
    fi
    mkdir -p /tmp/bla526-fuzz-http2
    timeout -k 5 $(( {{t}} + 30 )) uv run python tools/security/fuzz_run.py http2 \
        -max_total_time={{t}} -artifact_prefix=/tmp/bla526-fuzz-http2/ corpus/ || [ $? -eq 124 ]

fuzz-all t="60": (fuzz-http1 t) (fuzz-http2 t)

# YouTrack REST access. Credentials are read only by scripts/youtrack.sh.
yt-search query='project: BLA #Unresolved':
    scripts/youtrack.sh search "{{query}}"

yt-show issue:
    scripts/youtrack.sh show "{{issue}}"

yt-version name:
    scripts/youtrack.sh version "{{name}}"

yt-version-release name date:
    scripts/youtrack.sh version-release "{{name}}" "{{date}}"

yt-create summary description:
    scripts/youtrack.sh create "{{summary}}" "{{description}}"

yt-comment issue text:
    scripts/youtrack.sh comment "{{issue}}" "{{text}}"

yt-attach issue file:
    scripts/youtrack.sh attach "{{issue}}" "{{file}}"

# Replace an issue's description with the contents of a file
yt-update issue description_file:
    scripts/youtrack.sh update "{{issue}}" "{{description_file}}"

# Read a Knowledge Base article (BLA-A-<n>); no id lists them all
yt-article article='':
    scripts/youtrack.sh article {{article}}

# Replace a Knowledge Base article's body.  ASK THE USER FIRST — see AGENTS.md
yt-article-update article content_file:
    scripts/youtrack.sh article-update "{{article}}" "{{content_file}}"

yt-command issue command:
    scripts/youtrack.sh command "{{issue}}" "{{command}}"

yt-close issue:
    scripts/youtrack.sh close "{{issue}}"
