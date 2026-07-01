#!/usr/bin/env bash
# compare_bb_fastapi.sh — BlackBull vs FastAPI benchmark comparison.
#
# Parses HttpArena driver.log files and produces a Markdown table comparing
# throughput (req/s) for every profile where BOTH frameworks have results.
#
# Usage:
#   bash bench/httparena/compare_bb_fastapi.sh [bb_dir] [fastapi_dir]
#
# Maintenance: when this script is modified, update bench/httparena/README.md.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
BB_DIR="${1:-$REPO_ROOT/bench/results/httparena/sprint57-crud-v12-20260630-014356Z}"
FA_DIR="${2:-$REPO_ROOT/bench/results/httparena/sprint57-fixed-20260629-182851Z}"

for d in "$BB_DIR" "$FA_DIR"; do
    if [ ! -f "$d/driver.log" ]; then
        echo "ERROR: $d/driver.log not found" >&2
        exit 1
    fi
done

# Extract framework/profile/conns -> req/s from a driver.log.
parse_results() {
    local dir="$1" fw="$2" out="$3"
    python3 - "$dir/driver.log" "$fw" > "$out" << 'PYEOF'
import sys, re

logfile, fw = sys.argv[1], sys.argv[2]
header_re = re.compile(r'=== ' + re.escape(fw) + r' / (.+?) / (\d+c) \(tool=')
best_re = re.compile(r'Best: (\d+) req/s')

results = []
current = None
with open(logfile) as f:
    for line in f:
        m = header_re.search(line)
        if m:
            current = (m.group(1).strip(), m.group(2).replace('c', ''))
            continue
        m = best_re.search(line)
        if m and current:
            results.append((current[0], current[1], int(m.group(1))))
            current = None

for prof, conns, rps in sorted(results, key=lambda x: (x[0], -x[2])):
    print(f"{prof}|{conns}|{rps}")
PYEOF
}

parse_results "$BB_DIR" "blackbull" /tmp/bb_results.txt
parse_results "$FA_DIR" "fastapi"  /tmp/fa_results.txt

echo ""
echo "## BlackBull vs FastAPI - c7i.2xlarge"
echo ""
printf "| %-25s | %4s | %8s | %8s | %6s |\n" "Profile" "c" "BB r/s" "FA r/s" "BB/FA"
printf "|%-27s|------|----------|----------|--------|\n" "---------------------------"

while IFS='|' read -r prof conns bb_rps; do
    fa_rps=$(awk -F'|' -v p="$prof" -v c="$conns" '$1==p && $2==c {print $3; exit}' /tmp/fa_results.txt)
    if [ -n "$fa_rps" ] && [ "$fa_rps" != "0" ]; then
        ratio=$(awk "BEGIN {printf \"%.2f\", $bb_rps/$fa_rps}")
        printf "| %-25s | %4s | %8s | %8s | %5sx |\n" "$prof" "$conns" "$bb_rps" "$fa_rps" "$ratio"
    fi
done < /tmp/bb_results.txt

echo ""
echo "_BB: $(basename "$BB_DIR")_  "
echo "_FA: $(basename "$FA_DIR")_"

rm -f /tmp/bb_results.txt /tmp/fa_results.txt