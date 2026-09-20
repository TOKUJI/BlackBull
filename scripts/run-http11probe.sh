#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
UPSTREAM_URL="https://github.com/MDA2AV/Http11Probe.git"
UPSTREAM_COMMIT="d4bc93f2843ac77fcaae71f25069ce1534952e1a"
lane=both
show_plan=0

while [ "$#" -gt 0 ]; do
    case "$1" in
        --lane)
            [ "$#" -ge 2 ] || { echo "--lane requires a value" >&2; exit 2; }
            lane="$2"
            shift 2
            ;;
        --plan)
            show_plan=1
            shift
            ;;
        *)
            echo "unknown argument: $1" >&2
            exit 2
            ;;
    esac
done

case "$lane" in
    native) lanes_json='["native"]' ;;
    compat) lanes_json='["compat"]' ;;
    both) lanes_json='["native","compat"]' ;;
    *) echo "lane must be native, compat, or both" >&2; exit 2 ;;
esac

if [ "$show_plan" -eq 1 ]; then
    jq -nc \
        --arg commit "$UPSTREAM_COMMIT" \
        --argjson lanes "$lanes_json" \
        '{workflow:"http11probe",upstream_commit:$commit,lanes:$lanes}'
    exit 0
fi

for command in git dotnet jq curl uv; do
    command -v "$command" >/dev/null 2>&1 || {
        echo "run-http11probe: missing command: $command" >&2
        exit 1
    }
done

cd "$ROOT"
work="$(mktemp -d "${TMPDIR:-/tmp}/blackbull-http11probe.XXXXXX")"
server_pid=""
cleanup() {
    if [ -n "$server_pid" ] && kill -0 "$server_pid" 2>/dev/null; then
        kill "$server_pid" 2>/dev/null || true
        wait "$server_pid" 2>/dev/null || true
    fi
    rm -rf -- "$work"
}
trap cleanup EXIT
trap 'exit 130' HUP INT TERM

probe="$work/Http11Probe"
git init -q "$probe"
git -C "$probe" remote add origin "$UPSTREAM_URL"
git -C "$probe" fetch --quiet --depth=1 origin "$UPSTREAM_COMMIT"
resolved="$(git -C "$probe" rev-parse FETCH_HEAD)"
[ "$resolved" = "$UPSTREAM_COMMIT" ] || {
    echo "run-http11probe: upstream pin did not resolve exactly" >&2
    exit 1
}
git -C "$probe" switch --quiet --detach "$UPSTREAM_COMMIT"
dotnet build "$probe/src/Http11Probe.Cli" --configuration Release --nologo

run_id="$(date -u +%Y%m%dT%H%M%SZ)-$$"
result_dir="${HTTP11PROBE_RESULT_DIR:-$ROOT/bench/results/http11probe/$run_id}"
mkdir -p "$result_dir"
lane_records="$work/lanes.jsonl"
: > "$lane_records"
overall=0

stop_server() {
    if [ -n "$server_pid" ]; then
        kill "$server_pid" 2>/dev/null || true
        wait "$server_pid" 2>/dev/null || true
        server_pid=""
    fi
}

run_lane() {
    local name="$1" force_asgi="$2"
    local output="$result_dir/$name.json"
    local log="$result_dir/$name-server.log"
    local ready=0 probe_rc=0

    env BB_FORCE_ASGI_SCOPE="$force_asgi" uv run examples/probe_target.py \
        >"$log" 2>&1 &
    server_pid=$!
    for _ in $(seq 1 100); do
        if ! kill -0 "$server_pid" 2>/dev/null; then
            break
        fi
        if curl --fail --silent --show-error http://127.0.0.1:8080/ >/dev/null 2>&1; then
            ready=1
            break
        fi
        sleep 0.1
    done
    if [ "$ready" -ne 1 ]; then
        echo "run-http11probe: $name server did not become ready; see $log" >&2
        stop_server
        return 1
    fi

    dotnet run --no-build --configuration Release \
        --project "$probe/src/Http11Probe.Cli" -- \
        --host 127.0.0.1 --port 8080 --output "$output" || probe_rc=$?
    stop_server

    jq -e . "$output" >/dev/null || {
        echo "run-http11probe: $name did not produce valid JSON" >&2
        return 1
    }
    jq -nc \
        --arg lane "$name" \
        --arg result "$output" \
        --argjson exit_code "$probe_rc" \
        '{lane:$lane,result:$result,exit_code:$exit_code}' >> "$lane_records"
    [ "$probe_rc" -eq 0 ] || overall=1
}

case "$lane" in
    native) run_lane native 0 ;;
    compat) run_lane compat 1 ;;
    both)
        run_lane native 0
        run_lane compat 1
        ;;
esac

manifest_tmp="$(mktemp "$result_dir/.manifest.XXXXXX")"
jq -s \
    --arg commit "$UPSTREAM_COMMIT" \
    '{workflow:"http11probe",upstream_commit:$commit,lanes:.,complete:all(.[];.exit_code == 0)}' \
    "$lane_records" > "$manifest_tmp"
mv "$manifest_tmp" "$result_dir/manifest.json"
echo "$result_dir/manifest.json"
exit "$overall"
