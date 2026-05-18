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
# Output is teed to bench/conformance/results/h2spec_<timestamp>.txt
# and a JUnit XML report to bench/conformance/results/h2spec_<timestamp>.xml.

set -e

PORT="${PORT:-8443}"
HOST="${HOST:-localhost}"
TIMEOUT="${TIMEOUT:-5}"

RESULT_DIR="bench/conformance/results"
mkdir -p "$RESULT_DIR"
TS="$(date +%Y%m%d-%H%M%S)"
OUT="$RESULT_DIR/h2spec_${TS}.txt"
JUNIT="$RESULT_DIR/h2spec_${TS}.xml"

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
