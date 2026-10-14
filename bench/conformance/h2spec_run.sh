#!/usr/bin/env bash
# bench/conformance/h2spec_run.sh — run h2spec against a running BlackBull server.
#
# Prerequisites:
#   - h2spec binary on PATH (https://github.com/summerwind/h2spec/releases)
#   - BlackBull server listening on https://localhost:8443/ (any handler)
#
# Usage:
#   bash bench/conformance/h2spec_run.sh                 # full suite
#   bash bench/conformance/h2spec_run.sh hpack          # HPACK section only
#   bash bench/conformance/h2spec_run.sh http2/6.5      # specific section
#
# Output is teed to h2spec.txt and a JUnit XML report to h2spec.xml in the run
# directory (bench/lib/results.sh).

set -e

PORT="${PORT:-8443}"
HOST="${HOST:-localhost}"
TIMEOUT="${TIMEOUT:-5}"

. "$(dirname "$0")/../lib/results.sh"
bench_run_dir h2spec host="$HOST" port="$PORT" sections="$*"
OUT="$BENCH_RUN_DIR/h2spec.txt"
JUNIT="$BENCH_RUN_DIR/h2spec.xml"

command -v h2spec >/dev/null 2>&1 || {
    echo "ERROR: h2spec not on PATH. Install from:"
    echo "  https://github.com/summerwind/h2spec/releases"
    exit 1
}

echo "h2spec $(h2spec --version 2>&1 | head -1 | awk '{print $2}') vs $HOST:$PORT"
echo "Result: $OUT"
echo "JUnit:  $JUNIT"
echo ""

# h2spec exits non-zero when any test fails; preserve that as our exit code
set +e
h2spec --tls --insecure \
    --host "$HOST" --port "$PORT" \
    --timeout "$TIMEOUT" \
    --junit-report "$JUNIT" \
    "$@" 2>&1 | tee "$OUT"
RC=$?
set -e

echo ""
echo "h2spec exit: $RC"
exit $RC
