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
