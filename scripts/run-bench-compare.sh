#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

if [ "${1:-}" = "--plan" ]; then
    jq -nc '{workflow:"bench-compare",command:["bash","bench/compare/compare_servers.sh"]}'
    exit 0
fi
[ "$#" -eq 0 ] || { echo "unknown argument: $1" >&2; exit 2; }

cd "$ROOT"
source bench/lib/results.sh
bench_run_dir bench_compare
bash bench/compare/compare_servers.sh

[ -s "$BENCH_RUN_DIR/report.md" ] || { echo "run-bench-compare: result report not found" >&2; exit 1; }
jq -nc --arg report "$BENCH_RUN_DIR/report.md" --arg revision "$(git rev-parse HEAD)" \
    '{workflow:"bench-compare",complete:true,report:$report,revision:$revision}' \
    > "$BENCH_TMP/complete.json"
mv "$BENCH_TMP/complete.json" "$BENCH_RUN_DIR/complete.json"
echo "$BENCH_RUN_DIR/complete.json"
