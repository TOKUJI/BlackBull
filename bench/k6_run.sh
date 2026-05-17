#!/usr/bin/env bash
# Run k6 scenarios (200 VU rampup + 500 VU stress) and append results to the
# given h2load markdown summary file.
#
# Usage:
#   bash bench/k6_run.sh bench/results/20260517-205529.md   # append to existing
#   bash bench/k6_run.sh                                     # create new file

set -e

BASE="https://localhost:8443"
RESULT_DIR="bench/results"
mkdir -p "$RESULT_DIR"

# Use the supplied md file or create a new one
if [ -n "$1" ] && [ -f "$1" ]; then
    MD_FILE="$1"
    TIMESTAMP=$(basename "$MD_FILE" .md)
else
    TIMESTAMP="$(date +%Y%m%d-%H%M%S)"
    MD_FILE="$RESULT_DIR/${TIMESTAMP}.md"
    echo "# k6 results — ${TIMESTAMP}" > "$MD_FILE"
fi

RAMPUP_JSON="$RESULT_DIR/k6_rampup_${TIMESTAMP}.json"
STRESS_JSON="$RESULT_DIR/k6_stress_${TIMESTAMP}.json"

SEP="$(printf '=%.0s' {1..60})"

echo "$SEP"
echo "k6 benchmark — BlackBull"
echo "$(date)"
echo "$SEP"
echo ""

echo "--- rampup: 0 → 200 VU over 3 min, /ping ---"
k6 run \
    --summary-export="$RAMPUP_JSON" \
    --summary-trend-stats="p(50),p(95),p(99),max" \
    bench/k6/http_rampup.js

echo ""
echo "--- stress: 500 VU × 60 s, /ping ---"
k6 run \
    --summary-export="$STRESS_JSON" \
    --summary-trend-stats="p(50),p(95),p(99),max" \
    bench/k6/http_stress.js

echo ""
python bench/summarize.py k6 "$RAMPUP_JSON" "$STRESS_JSON" "$MD_FILE"
