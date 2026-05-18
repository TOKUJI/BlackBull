#!/usr/bin/env bash
# Run one k6 stress pass against the currently-running server on port 8443.
# Prints "req/s | p50 | p95 | p99" or "err" if k6 exits nonzero.

set -e

LABEL="$1"
RESULT_DIR="bench/results"
JSON="$RESULT_DIR/peer_k6_${LABEL}_$(date +%H%M%S).json"

k6 run --quiet \
    --summary-export="$JSON" \
    --summary-trend-stats="p(50),p(95),p(99),max" \
    bench/k6/http_stress.js >/dev/null 2>&1 || true

python3 - "$JSON" <<'PYEOF'
import sys, json
from pathlib import Path
try:
    d = json.loads(Path(sys.argv[1]).read_text())
    dur  = d["metrics"]["http_req_duration"]
    reqs = d["metrics"]["http_reqs"]
    err  = d["metrics"].get("errors", {}).get("value", 0)
    print(f"{reqs['rate']:.0f} | {dur['p(50)']:.2f} | {dur['p(95)']:.2f} | {dur['p(99)']:.2f} | {err*100:.2f}%")
except Exception as e:
    print(f"err: {e}")
PYEOF
