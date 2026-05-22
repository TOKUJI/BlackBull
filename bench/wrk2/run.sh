#!/usr/bin/env bash
# bench/wrk2/run.sh — B2r scenario only (constant-rate, CO-corrected).
#
# Why this exists alongside bench/wrk/run.sh:
#   wrk's HdrHistogram overflows under B2's c=1024 pipeline=16, producing
#   p50/p99 = 0.00us. wrk2 (giltene fork) fixes the histogram AND adds
#   Coordinated-Omission-corrected latency at a target throughput rate.
#   But wrk2 *requires* a rate (-R) — it's not a drop-in throughput tool.
#
# So we add ONE scenario that the wrk lane can't measure correctly:
#   B2r_plaintext_rate5000 = c=256, -R 5000 req/s, no pipelining.
# All six stacks can sustain 5000 req/s on the loopback, so every stack
# is measured at the same numeric load. The output is the actual tail
# latency the server delivers when steadily asked for 5000 req/s.
#
# Append-only — emits one markdown row to stdout; assumes the caller
# (compare_servers.sh -> run_lane_b_wrk) already emitted the table header
# from bench/wrk/run.sh.

BASE="${BASE:-https://localhost:8443}"
OUTDIR="${OUTDIR:-bench/results}"
LABEL_PREFIX="${LABEL_PREFIX:-}"
D="${DURATION:-30}"
RATE="${WRK2_RATE:-5000}"

mkdir -p "$OUTDIR"

# Prefer ~/.local/bin/wrk2 (where bench/install.sh puts it), else PATH.
WRK2="${WRK2_BIN:-$HOME/.local/bin/wrk2}"
[ -x "$WRK2" ] || WRK2="$(command -v wrk2 2>/dev/null)"
[ -x "$WRK2" ] || {
    echo "| B2r_plaintext_rate${RATE} | err | — | — | — | — |  (wrk2 not installed) |"
    exit 0
}

logfile="$OUTDIR/wrk2_${LABEL_PREFIX}B2r_plaintext_rate${RATE}.txt"
url="$BASE/plaintext"

{
    echo "# scenario: B2r_plaintext_rate${RATE}"
    echo "# command: $WRK2 --latency -t4 -c256 -d${D}s -R${RATE} $url"
    "$WRK2" --latency -t4 -c256 -d"${D}s" -R"${RATE}" "$url"
} > "$logfile" 2>&1 || true

if grep -q "unable to connect" "$logfile" 2>/dev/null; then
    echo "| B2r_plaintext_rate${RATE} | err | — | — | — | — |  (unable to connect) |"
    exit 0
fi

# wrk2 --latency output reuses the wrk "Latency Distribution" block, so
# the same parser works.  Mean comes from the Latency line.
rps=$(grep 'Requests/sec' "$logfile" | awk '{print $2}')
avg=$(grep -m1 '^    Latency' "$logfile" | awk '{print $2}')
# wrk2 emits percentiles as "50.000%" / "99.000%" (HdrHistogram block);
# match either the wrk-style "50%" or wrk2-style "50.000%".
p50=$(grep -E '^[[:space:]]+50(\.0+)?%[[:space:]]' "$logfile" | head -1 | awk '{print $2}')
p99=$(grep -E '^[[:space:]]+99(\.0+)?%[[:space:]]' "$logfile" | head -1 | awk '{print $2}')
max=$(grep -m1 '^    Latency' "$logfile" | awk '{print $4}')
errs=$(grep -m1 'Socket errors:' "$logfile" | sed 's/^[[:space:]]*Socket errors:[[:space:]]*//')

# wrk2 reports a flat "0.00us" when no requests fell in that bucket,
# unlike wrk's histogram-overflow case; treat the same.
[ "$p50" = "0.00us" ] && p50="—"
[ "$p99" = "0.00us" ] && p99="—"
echo "| B2r_plaintext_rate${RATE} | $rps | $avg | $p50 | $p99 | $max | ${errs:- } |"
