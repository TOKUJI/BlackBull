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

# BLA-526 local robustness probe target (loopback only).
# PID and log live in /tmp; see docs/security/fixture-app.md.
vuln-target-up:
    #!/usr/bin/env bash
    set -euo pipefail
    pid_file=/tmp/bb-vuln-target.pid
    log=/tmp/bb-vuln-target.log
    url=http://127.0.0.1:8000
    healthy() {
        .venv/bin/python -c 'import sys, urllib.request; sys.exit(0 if urllib.request.urlopen("http://127.0.0.1:8000/", timeout=2).read() == b"ok" else 1)' 2>/dev/null
    }
    if [ -f "$pid_file" ] && kill -0 "$(cat "$pid_file")" 2>/dev/null; then
        if healthy; then
            echo "vuln-target already running (pid $(cat "$pid_file")): $url"
            exit 0
        fi
        echo "vuln-target: pid $(cat "$pid_file") is alive but $url/ does not serve the fixture; refusing" >&2
        exit 1
    fi
    if healthy || (exec 3<>"/dev/tcp/127.0.0.1/8000") 2>/dev/null; then
        exec 3>&- 2>/dev/null || true
        echo "vuln-target: port 8000 is already in use by another process; refusing to start" >&2
        exit 1
    fi
    uv sync --all-extras
    # Not `uv run`: uv spawns the interpreter as a child, and the PID file
    # must name the server process itself.
    nohup .venv/bin/python tools/security/fixture_app.py --port 8000 >>"$log" 2>&1 &
    echo "$!" >"$pid_file"
    for _ in $(seq 1 50); do
        if healthy; then
            echo "vuln-target up (pid $(cat "$pid_file")): $url"
            exit 0
        fi
        if ! kill -0 "$(cat "$pid_file")" 2>/dev/null; then
            break
        fi
        sleep 0.2
    done
    echo "vuln-target did not serve GET / = ok on $url within 10s; last log lines:" >&2
    tail -n 20 "$log" >&2 || true
    kill "$(cat "$pid_file")" 2>/dev/null || true
    rm -f "$pid_file"
    exit 1

vuln-target-down:
    #!/usr/bin/env bash
    set -euo pipefail
    pid_file=/tmp/bb-vuln-target.pid
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
    if (exec 3<>"/dev/tcp/127.0.0.1/8000") 2>/dev/null; then
        exec 3>&- || true
        echo "vuln-target: port 8000 is still accepting connections" >&2
        exit 1
    fi
    echo "vuln-target down; port 8000 free"

# Run the BLA-526 local robustness probe against a running target
vuln-check base_url="http://127.0.0.1:8000":
    uv run python tools/security/probe.py --base-url "{{base_url}}"

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
