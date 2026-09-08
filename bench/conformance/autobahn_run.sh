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
#   EXCLUDE_CASES='13.*' bash bench/conformance/autobahn_run.sh  # all but 13.x
#   AUTOBAHN_IMAGE=crossbario/autobahn-testsuite:latest bash …  # test an upgrade
#
# CASES is a comma-separated list of Autobahn case patterns (default '*');
# EXCLUDE_CASES (optional) is the same shape and fills "exclude-cases"
# (wstest runs the case set minus the exclusions — caseset.py resolves both
# with the same pattern syntax, so '12.*.10' and '13.*' both work).
# The static autobahn_fuzzingclient.json is a template: a per-run copy with
# "cases" / "exclude-cases" substituted is rendered into the results dir and
# mounted instead (P.2 — CASES was once documented but ignored, and subset
# runs silently ran all 517 cases).
#
# Reports land in bench/conformance/results/autobahn_<timestamp>/.

set -euo pipefail

PORT="${PORT:-9001}"
CONFIG_DIR="$(cd "$(dirname "$0")" && pwd)"
. "$CONFIG_DIR/autobahn_common.sh"

# Pinned by digest, not by :latest.  A conformance suite that can change
# under us turns "the wire behaviour regressed" and "the tester changed" into
# the same red X, and the run that proved 517/517 last week is then not a run
# anyone can repeat.  Bump this deliberately, in its own commit, with the
# before/after case counts in the message.
#   digest of crossbario/autobahn-testsuite:latest as of 2026-08-10

RESULT_BASE="bench/conformance/results"
mkdir -p "$RESULT_BASE"
TS="$(date +%Y%m%d-%H%M%S)"
# A retry must not inherit a report even when both starts share a timestamp.
OUT="$(mktemp -d "$RESULT_BASE/autobahn_${TS}.XXXXXX")"
CIDFILE="$OUT/container.cid"

cleanup() {
    local original_status=$?
    local final_status=$original_status
    local cleanup_failed=0
    trap - EXIT
    set +e
    if [ -e "$CIDFILE" ]; then
        local cid=''
        # Docker writes --cidfile without a trailing newline. `read` assigns
        # the ID but returns non-zero at EOF, so validity is the nonempty
        # value rather than read's status.
        IFS= read -r cid < "$CIDFILE" || true
        if [ -n "$cid" ]; then
            # Preserve only State, not the container's environment or config.
            # --rm would discard the OOM/exit evidence before inspection.
            if timeout 10 docker inspect --format '{{json .State}}' "$cid" \
                > "$OUT/container-state.json"; then
                cat "$OUT/container-state.json"
            else
                echo "WARNING: could not inspect Autobahn container $cid" >&2
                cleanup_failed=1
            fi
            if ! timeout 10 docker rm -f "$cid" >/dev/null; then
                echo "WARNING: could not remove Autobahn container $cid" >&2
                cleanup_failed=1
            fi
        else
            echo 'WARNING: could not read Autobahn container ID' >&2
            cleanup_failed=1
        fi
    else
        echo 'WARNING: Autobahn runner produced no container ID file' >&2
        cleanup_failed=1
    fi
    # Preserve the tester's diagnostic exit (including TERM/OOM). Cleanup
    # failures only turn an otherwise successful run into a failure.
    if [ "$original_status" -eq 0 ] && [ "$cleanup_failed" -ne 0 ]; then
        final_status=1
    fi
    printf '%s\n' "$final_status" > "$OUT/exit-code.txt"
    echo "Autobahn runner exit code: $final_status"
    exit "$final_status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

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

# Render the per-run config: the checked-in JSON is the template, only
# "cases" is replaced.  With CASES unset the rendered "cases" is ["*"] —
# identical to the template — so the CI job (which sets no CASES) keeps
# running the full suite.
CASES="${CASES:-*}" EXCLUDE_CASES="${EXCLUDE_CASES:-}" python3 - \
    "$CONFIG_DIR/autobahn_fuzzingclient.json" "$OUT/fuzzingclient.json" <<'PYEOF'
import json, os, sys
src, dst = sys.argv[1], sys.argv[2]
with open(src) as f:
    cfg = json.load(f)
cases = [c.strip() for c in os.environ['CASES'].split(',') if c.strip()]
cfg['cases'] = cases or ['*']
excl = [c.strip() for c in os.environ['EXCLUDE_CASES'].split(',') if c.strip()]
if excl:
    cfg['exclude-cases'] = excl
with open(dst, 'w') as f:
    json.dump(cfg, f, indent=3)
PYEOF

echo "Autobahn|Testsuite vs ws://localhost:$PORT"
echo "Cases:   ${CASES:-*}"
echo "Results: $OUT"
echo ""

docker run --cidfile "$CIDFILE" \
    -e PYTHONUNBUFFERED=1 \
    --add-host=host.docker.internal:host-gateway \
    -v "$(realpath "$OUT/fuzzingclient.json"):/config/fuzzingclient.json:ro" \
    -v "$(realpath "$OUT"):/results" \
    "$AUTOBAHN_IMAGE" \
    wstest -m fuzzingclient -s /config/fuzzingclient.json 2>&1 | tee "$OUT/tester.log"

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
