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
#   CASES='1.*,2.*,6.*,7.*' bash bench/conformance/autobahn_run.sh  # PR lane
#
# CASES is a comma-separated list of Autobahn case patterns (default '*').
# The static autobahn_fuzzingclient.json is a template: a per-run copy with
# "cases" substituted is rendered into the results dir and mounted instead
# (P.2, Sprint 72 — previously CASES was documented but ignored and subset
# runs silently ran all 517 cases).
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

# Render the per-run config: the checked-in JSON is the template, only
# "cases" is replaced.  With CASES unset the rendered "cases" is ["*"] —
# identical to the template — so the CI job (which sets no CASES) keeps
# running the full suite.
CASES="${CASES:-*}" python3 - "$CONFIG_DIR/autobahn_fuzzingclient.json" \
    "$OUT/fuzzingclient.json" <<'PYEOF'
import json, os, sys
src, dst = sys.argv[1], sys.argv[2]
with open(src) as f:
    cfg = json.load(f)
cases = [c.strip() for c in os.environ['CASES'].split(',') if c.strip()]
cfg['cases'] = cases or ['*']
with open(dst, 'w') as f:
    json.dump(cfg, f, indent=3)
PYEOF

echo "Autobahn|Testsuite vs ws://localhost:$PORT"
echo "Cases:   ${CASES:-*}"
echo "Results: $OUT"
echo ""

docker run --rm \
    --add-host=host.docker.internal:host-gateway \
    -v "$(realpath "$OUT/fuzzingclient.json"):/config/fuzzingclient.json:ro" \
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
