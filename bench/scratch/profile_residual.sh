#!/usr/bin/env bash
# bench/scratch/profile_residual.sh — cProfile cumulative-time attribution of
# the Sprint 104 residual, two refs x two lanes.  Load generator runs outside
# the profile; cumulative time is exact (immune to this box's bimodality).
#
#   fix  = main tree (6ed22fa, the committed tip)
#   base = e736c10, a git worktree so the editable install is never disturbed
#
# Output: bench/results/profile-residual-<TS>/<ref>_<lane>.prof + load logs.
set -uo pipefail

REPO=/home/toshio/work/BlackBull
cd "$REPO" || exit 1
PORT="${PORT:-8450}"
H1_N="${H1_N:-5000}"       # cProfile is ~100x slow; exact counts need no more
H2_N="${H2_N:-5000}"
CONNS=32
TS=$(date -u +%Y%m%d-%H%M%SZ)
OUT="${OUT:-bench/results/profile-residual-$TS}"
mkdir -p "$OUT"
cp "$0" "$OUT/profile_residual.sh"
cp bench/scratch/profile_server.py "$OUT/profile_server.py"

WT="${WT:-/home/toshio/work/bb-e736c10}"
if [ ! -d "$WT/.git" ]; then
    git worktree add "$WT" e736c10 || { echo "worktree failed" >&2; exit 1; }
fi

profile_lane() {
    local label="$1" tree="$2" lane="$3" n
    local prof="$OUT/${label}_${lane}.prof" log="$OUT/${label}_${lane}.server.log"
    local load="$OUT/${label}_${lane}.load.log"
    if [ "$lane" = h2 ]; then n="$H2_N"; else n="$H1_N"; fi
    echo "=== $label / $lane (n=$n) ==="

    PYTHONPATH="$REPO:$tree" BB_UVLOOP=0 taskset -c 0-1 \
        "$REPO/.venv/bin/python" bench/scratch/profile_server.py "$PORT" "$prof" \
        >"$log" 2>&1 &
    local srv=$!

    local ready=0
    for _ in $(seq 1 150); do
        if curl -s -o /dev/null "http://127.0.0.1:$PORT/conn"; then ready=1; break; fi
        sleep 0.2
    done
    if [ "$ready" != "1" ]; then
        echo "  server not ready" >&2
        tail -15 "$log" >&2
        kill -TERM "$srv" 2>/dev/null; wait "$srv" 2>/dev/null
        return 1
    fi

    if [ "$lane" = h2 ]; then
        taskset -c 4-9 h2load -c "$CONNS" -m 16 -n "$n" \
            "http://127.0.0.1:$PORT/1kb" >"$load" 2>&1
    else
        taskset -c 4-9 h2load --h1 -c "$CONNS" -n "$n" \
            "http://127.0.0.1:$PORT/conn" >"$load" 2>&1
    fi

    kill -TERM "$srv" 2>/dev/null
    wait "$srv" 2>/dev/null
    echo "  done: $(grep -oE '[0-9.]+ req/s' "$load" | tail -1 || echo n/a)"
}

profile_lane fix "$REPO" h1 || exit 1
profile_lane fix "$REPO" h2 || exit 1
profile_lane base "$WT" h1 || exit 1
profile_lane base "$WT" h2 || exit 1

echo "profiles: $OUT"
