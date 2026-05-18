#!/usr/bin/env bash
# bench/conformance/autobahn_run.sh — drive Autobahn|Testsuite against a
# locally-running BlackBull WebSocket echo server.
#
# Prereqs:
#   - docker on PATH
#   - the WS echo server listening on ws://localhost:9001 (start with:
#       python bench/conformance/autobahn_app.py --port 9001)
#
# Usage:
#   bash bench/conformance/autobahn_run.sh            # full fuzzingclient
#   CASES='1.*' bash bench/conformance/autobahn_run.sh  # subset
#
# Reports land in bench/conformance/results/autobahn_<timestamp>/.

set -e

PORT="${PORT:-9001}"

RESULT_BASE="bench/conformance/results"
mkdir -p "$RESULT_BASE"
TS="$(date +%Y%m%d-%H%M%S)"
OUT="$RESULT_BASE/autobahn_${TS}"
mkdir -p "$OUT"

if ! command -v docker >/dev/null 2>&1; then
    echo "ERROR: docker not on PATH" >&2
    exit 1
fi

# Sanity-check the server is up
if ! python3 -c "
import socket, sys
s = socket.socket()
s.settimeout(1)
try:
    s.connect(('localhost', $PORT))
except Exception as e:
    print(f'ERROR: cannot reach localhost:$PORT — {e}', file=sys.stderr)
    sys.exit(1)
s.close()
"; then
    echo "Start the echo server first:" >&2
    echo "  python bench/conformance/autobahn_app.py --port $PORT" >&2
    exit 1
fi

CONFIG_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "Autobahn|Testsuite vs ws://localhost:$PORT"
echo "Results: $OUT"
echo ""

docker run --rm \
    --add-host=host.docker.internal:host-gateway \
    -v "$CONFIG_DIR/autobahn_fuzzingclient.json:/config/fuzzingclient.json:ro" \
    -v "$(realpath "$OUT"):/results" \
    crossbario/autobahn-testsuite \
    wstest -m fuzzingclient -s /config/fuzzingclient.json

echo ""
echo "Index report: $OUT/index.html"
if command -v jq >/dev/null 2>&1 && [ -f "$OUT/index.json" ]; then
    echo ""
    echo "Pass/fail summary:"
    jq -r '."BlackBull" | to_entries
      | group_by(.value.behavior, .value.behaviorClose)
      | map({k: (.[0].value.behavior + "/" + .[0].value.behaviorClose), n: length})
      | sort_by(.k)
      | .[] | "  \(.k): \(.n)"' "$OUT/index.json"
fi
