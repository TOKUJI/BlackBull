#!/usr/bin/env bash
# bench/soak/soak.sh — Sprint 28 Task 2 soak orchestrator.
#
# Starts bench/app.py with BB_TRACEMALLOC=1, kicks off bench/soak/
# sample.py to capture RSS / FD / connection / tracemalloc snapshots
# every INTERVAL seconds, drives wrk -t4 -cC -dDURATION against
# bench/soak/mixed.lua, then waits COOLDOWN seconds with sampling
# still running, then tears everything down and emits a summary.
#
# Output dir:
#   bench/results/soak/sprint28-<ts>-w<WORKERS>/
#     server.log              stderr+stdout of bench/app.py
#     wrk.txt                 raw wrk output
#     sample.csv              one row per sample (60 s default)
#     tracemalloc-final.json  final top-N alloc snapshot
#     summary.md              human-readable verdict
#
# Env knobs:
#   DURATION   wrk load duration (seconds, default 3600)
#   COOLDOWN   post-load wait before teardown (default 300)
#   INTERVAL   sample interval (default 60)
#   C          wrk connections (default 256)
#   WORKERS    BlackBull worker count (default 1)
#   PORT       server listen port (default 8000)
#
# Usage:
#   DURATION=120 COOLDOWN=30 bash bench/soak/soak.sh   # 2-minute pilot
#   bash bench/soak/soak.sh                            # full 1-hour run
#   WORKERS=4 bash bench/soak/soak.sh                  # 4-worker variant

set -euo pipefail

DURATION="${DURATION:-3600}"
COOLDOWN="${COOLDOWN:-300}"
INTERVAL="${INTERVAL:-60}"
C="${C:-256}"
WORKERS="${WORKERS:-1}"
PORT="${PORT:-8000}"

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
TS="$(date -u +%Y%m%d-%H%M%SZ)"
OUT_DIR="$REPO_ROOT/bench/results/soak/sprint28-${TS}-w${WORKERS}"
mkdir -p "$OUT_DIR"

PY="${PY:-$REPO_ROOT/.venv/bin/python}"

echo "=== bench/soak/soak.sh ==="
echo "  output:    $OUT_DIR"
echo "  duration:  ${DURATION}s + ${COOLDOWN}s cooldown"
echo "  interval:  ${INTERVAL}s"
echo "  load:      wrk -t4 -c${C} -s mixed.lua http://localhost:${PORT}"
echo "  workers:   ${WORKERS}"
echo

cd "$REPO_ROOT"

# 1. Defensively clear lingering processes from previous runs.
pkill -f '[b]ench/app.py' 2>/dev/null || true
pkill -f '[s]ample.py' 2>/dev/null || true
sleep 1

# 2. Start the server (cleartext, no TLS, mixed-traffic target).
BB_ACCESS_LOG=0 BB_TRACEMALLOC=1 \
    "$PY" bench/app.py --port "$PORT" --no-tls --workers "$WORKERS" \
    > "$OUT_DIR/server.log" 2>&1 &
SERVER_PID=$!
echo "server pid=$SERVER_PID"

# Wait for /ping to answer (≤ 30 s).
ready=0
for i in $(seq 1 30); do
    if curl -sf --max-time 2 "http://localhost:${PORT}/ping" >/dev/null 2>&1; then
        ready=1; echo "  server ready (${i}s)"; break
    fi
    sleep 1
done
if [ "$ready" = "0" ]; then
    echo "ERROR: server not ready after 30s" >&2
    tail -20 "$OUT_DIR/server.log" >&2 || true
    kill -9 $SERVER_PID 2>/dev/null || true
    exit 1
fi

# When WORKERS>0 the master forks; sample the first worker for the
# soak (master has trivial mem; worker is where leaks would show).
# When WORKERS=1 there's no master/worker split — the server PID is
# itself the process to sample.
SAMPLE_PID="$SERVER_PID"
if [ "$WORKERS" -gt 1 ] 2>/dev/null; then
    # Find a child of $SERVER_PID running bench/app.py.
    sleep 2
    SAMPLE_PID="$(pgrep -P "$SERVER_PID" -f '[b]ench/app.py' | head -n1)"
    if [ -z "$SAMPLE_PID" ]; then SAMPLE_PID="$SERVER_PID"; fi
    echo "sampling worker pid=$SAMPLE_PID (master pid=$SERVER_PID)"
fi

# 3. Start the sampler.  Tracks the worker (or master if WORKERS=1).
"$PY" bench/soak/sample.py \
    --pid "$SAMPLE_PID" --port "$PORT" \
    --out "$OUT_DIR" --interval "$INTERVAL" \
    > "$OUT_DIR/sample.log" 2>&1 &
SAMPLE_PID_CHILD=$!
echo "sampler pid=$SAMPLE_PID_CHILD"

# 4. Drive load.
echo "load phase (${DURATION}s) ..."
wrk -t4 -c"$C" -d"${DURATION}s" \
    -s "$REPO_ROOT/bench/soak/mixed.lua" \
    "http://localhost:${PORT}/plaintext" \
    > "$OUT_DIR/wrk.txt" 2>&1 || true

echo "load complete; cooldown ${COOLDOWN}s ..."
sleep "$COOLDOWN"

# 5. Stop sampler (so we keep the cooldown sample), then server.
kill "$SAMPLE_PID_CHILD" 2>/dev/null || true
wait "$SAMPLE_PID_CHILD" 2>/dev/null || true

kill "$SERVER_PID" 2>/dev/null || true
sleep 2
kill -9 "$SERVER_PID" 2>/dev/null || true
pkill -f '[b]ench/app.py' 2>/dev/null || true

# 6. Summary — quick pass/fail using the CSV.
"$PY" - <<EOF > "$OUT_DIR/summary.md"
import csv, os, sys
out = "$OUT_DIR"
rows = list(csv.DictReader(open(os.path.join(out, "sample.csv"))))
def f(r, k):
    v = r.get(k, "")
    try: return int(v)
    except (TypeError, ValueError): return None

print("# Soak run summary —", os.path.basename(out))
print()
print(f"- Samples:  {len(rows)}")
if not rows:
    print("- VERDICT:  ✗ no samples captured")
    sys.exit(0)
rss = [f(r, "vmrss_kb") for r in rows if f(r, "vmrss_kb") is not None]
tm  = [f(r, "tm_current_bytes") for r in rows if f(r, "tm_current_bytes") is not None]
conns = [f(r, "established_conns") for r in rows if f(r, "established_conns") is not None]
fds = [f(r, "open_fds") for r in rows if f(r, "open_fds") is not None]

def plateau(xs):
    """Returns (start, mid, end) — first, midpoint, last sample, and the
    second-half slope expressed as (end - mid) / mid as a percentage."""
    if len(xs) < 4: return (xs[0] if xs else None, None, xs[-1] if xs else None, None)
    n = len(xs)
    return (xs[0], xs[n//2], xs[-1], 100*(xs[-1]-xs[n//2])/max(xs[n//2], 1))

r0, rmid, rlast, rslope = plateau(rss)
print(f"- VmRSS:    start={r0} kB, mid={rmid} kB, end={rlast} kB, 2nd-half drift={rslope:+.1f}%" if rslope is not None else f"- VmRSS:    {r0}/.../{rlast} kB")
if tm:
    t0, tmid, tlast, tslope = plateau(tm)
    print(f"- tracemalloc.current: start={t0} B, mid={tmid} B, end={tlast} B, 2nd-half drift={tslope:+.1f}%" if tslope is not None else f"- tracemalloc.current: {t0}/.../{tlast} B")
if conns:
    print(f"- conns:    start={conns[0]}, peak={max(conns)}, final={conns[-1]}")
if fds:
    print(f"- open_fds: start={fds[0]}, peak={max(fds)}, final={fds[-1]}")

print()
verdict_ok = True
notes = []
if rslope is not None and rslope > 5:
    verdict_ok = False
    notes.append(f"VmRSS grew {rslope:+.1f}% in the second half (>5% drift)")
if conns and conns[-1] > (conns[0] + 10):
    verdict_ok = False
    notes.append(f"connections didn't return to baseline+10 (final={conns[-1]}, start={conns[0]})")
if tm and tslope is not None and tslope > 10:
    verdict_ok = False
    notes.append(f"tracemalloc.current grew {tslope:+.1f}% in the second half (>10% drift)")

print(f"- VERDICT:  {'✓ leak-free plateau' if verdict_ok else '⚠ see notes'}")
for n in notes:
    print(f"  - {n}")
EOF

echo
echo "=== complete ==="
cat "$OUT_DIR/summary.md"
echo
echo "Artefacts: $OUT_DIR"
