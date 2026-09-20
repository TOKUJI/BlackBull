#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

if [ "${1:-}" = "--plan" ]; then
    jq -nc '{workflow:"bench-compare",command:["bash","bench/peers/compare_servers.sh"]}'
    exit 0
fi
[ "$#" -eq 0 ] || { echo "unknown argument: $1" >&2; exit 2; }

cd "$ROOT"
bash bench/peers/compare_servers.sh

report="$(ls -1t bench/results/compare_servers_*.md 2>/dev/null | head -1 || true)"
[ -n "$report" ] || { echo "run-bench-compare: result report not found" >&2; exit 1; }
marker="bench/results/bench-compare-$(date -u +%Y%m%dT%H%M%SZ)-$$.complete.json"
marker_tmp="$(mktemp "bench/results/.bench-compare.XXXXXX")"
trap 'rm -f -- "$marker_tmp"' EXIT HUP INT TERM
jq -nc \
    --arg report "$report" \
    --arg revision "$(git rev-parse HEAD)" \
    '{workflow:"bench-compare",complete:true,report:$report,revision:$revision}' \
    > "$marker_tmp"
mv "$marker_tmp" "$marker"
trap - EXIT HUP INT TERM
echo "$marker"
