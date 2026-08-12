#!/usr/bin/env bash
# bench/peers/scenario_capture.sh — per-scenario CPU + seam snapshot capture.
#
# Called by bench/wrk/run.sh / bench/wrk2/run.sh around each scenario:
#   "$CAPTURE_CMD" before <scenario_label>
#   "$CAPTURE_CMD" after  <scenario_label>
#
# before: capture cpu0 + a SIGUSR1 seam snapshot (baseline).
# after:  capture cpu1 + a SIGUSR1 seam snapshot, then write:
#   cpu_<CAPTURE_PREFIX><label>.txt      total CPU ticks for the scenario
#   resp_<CAPTURE_PREFIX><label>.txt     resp seam delta (calls/sum_ms/max_us)
#   handler_<CAPTURE_PREFIX><label>.txt  handler seam delta (calls/sum_ms)
#
# This gives the F2 fork per-scenario total/resp/handler (CPU-µs/req against
# the scenario's wrk request count) instead of the body-size-mixed whole-lane
# aggregate that F1 used — fixing the scope finding (B4/B5/B7 = 98% of bytes)
# and the warmup denominator mismatch (cpu0 + seam snapshot are both taken
# AFTER warmup, at the scenario boundary) in one change.
#
# Env:
#   CAPTURE_PID       server pid (the /proc stat + SIGUSR1 target)
#   CAPTURE_SCRATCH   scratch dir for the output files
#   CAPTURE_PREFIX    filename prefix (stack label, e.g. "blackbull_")
#   CAPTURE_SNAP      the server's BB_TIMING_SNAP file path
#   BENCH_REMOTE_LIFECYCLE  1 → server is remote (split topology); no-op.
#                            F2 runs TOPO=single.
#
# The snapshot file format (from response_timing._summary):
#   seq=N seam=.. calls=N sum_ms=X mean_us=.. max_us=Y handler_calls=M
#   handler_sum_ms=Z
# Fields are ordered resp-first, so `head -1` on a `calls=`/`sum_ms=`/`max_us=`
# match disambiguates resp from handler_*.

set -uo pipefail

MODE="${1:-}"
LABEL="${2:-}"
[ -n "$MODE" ] && [ -n "$LABEL" ] || { echo "usage: scenario_capture.sh before|after <label>" >&2; exit 2; }
[ "${BENCH_REMOTE_LIFECYCLE:-0}" = "1" ] && exit 0
[ -n "${CAPTURE_PID:-}" ] || exit 0
[ -n "${CAPTURE_SCRATCH:-}" ] || exit 0
[ -n "${CAPTURE_SNAP:-}" ] || exit 0

CLK_TCK="$(getconf CLK_TCK 2>/dev/null || echo 100)"
STATE="$CAPTURE_SCRATCH/.cap_${CAPTURE_PREFIX}${LABEL}"

field() {  # $1 = snapshot text, $2 = field name pattern (grep -o)
    echo "$1" | grep -o "$2=[0-9.]*" | head -1 | sed "s/^$2=//"
}

snap_seq() {
    sed -n 's/^seq=\([0-9]*\).*/\1/p' "$CAPTURE_SNAP" 2>/dev/null | head -1
}

# Send SIGUSR1 and wait until the snapshot file's seq advances past $1
# (the signal handler writes the file synchronously; ~2 s budget).
wait_snap() {
    local prev="$1" newseq="" i
    kill -USR1 "$CAPTURE_PID" 2>/dev/null || true
    for i in $(seq 1 40); do
        newseq="$(snap_seq)"
        [ -n "$newseq" ] && [ "$newseq" -gt "$prev" ] && break
        sleep 0.05
    done
    echo "${newseq:-$prev}"
}

if [ "$MODE" = "before" ]; then
    cpu0="$(awk '{print $14 + $15}' "/proc/$CAPTURE_PID/stat" 2>/dev/null || echo 0)"
    echo "$cpu0" > "$STATE.cpu0"
    prev="$(cat "$STATE.snap_seq" 2>/dev/null || echo 0)"
    seq_now="$(wait_snap "$prev")"
    echo "$seq_now" > "$STATE.snap_seq"
    cp "$CAPTURE_SNAP" "$STATE.snap" 2>/dev/null || true
    exit 0
fi

if [ "$MODE" = "after" ]; then
    cpu1="$(awk '{print $14 + $15}' "/proc/$CAPTURE_PID/stat" 2>/dev/null || echo 0)"
    cpu0="$(cat "$STATE.cpu0" 2>/dev/null || echo 0)"
    prev="$(cat "$STATE.snap_seq" 2>/dev/null || echo 0)"
    wait_snap "$prev" >/dev/null
    end="$(cat "$CAPTURE_SNAP" 2>/dev/null || true)"
    start="$(cat "$STATE.snap" 2>/dev/null || true)"

    rc0="$(field "$start" calls)";   rc1="$(field "$end" calls)"
    rs0="$(field "$start" sum_ms)";  rs1="$(field "$end" sum_ms)"
    rm0="$(field "$start" max_us)";  rm1="$(field "$end" max_us)"
    hc0="$(field "$start" handler_calls)"; hc1="$(field "$end" handler_calls)"
    hs0="$(field "$start" handler_sum_ms)"; hs1="$(field "$end" handler_sum_ms)"
    pc0="$(field "$start" parse_calls)";  pc1="$(field "$end" parse_calls)"
    ps0="$(field "$start" parse_sum_ms)"; ps1="$(field "$end" parse_sum_ms)"
    pm0="$(field "$start" parse_max_us)"; pm1="$(field "$end" parse_max_us)"
    psc0="$(field "$start" parse_scan_calls)";  psc1="$(field "$end" parse_scan_calls)"
    pss0="$(field "$start" parse_scan_sum_ms)"; pss1="$(field "$end" parse_scan_sum_ms)"
    nc0="$(field "$start" null_calls)";  nc1="$(field "$end" null_calls)"
    ns0="$(field "$start" null_sum_ms)"; ns1="$(field "$end" null_sum_ms)"
    dc0="$(field "$start" " dispatch_calls")";  dc1="$(field "$end" " dispatch_calls")"
    ds0="$(field "$start" " dispatch_sum_ms")"; ds1="$(field "$end" " dispatch_sum_ms")"
    dm0="$(field "$start" " dispatch_max_us")"; dm1="$(field "$end" " dispatch_max_us")"
    nac0="$(field "$start" null_a_calls)";  nac1="$(field "$end" null_a_calls)"
    nas0="$(field "$start" null_a_sum_ms)"; nas1="$(field "$end" null_a_sum_ms)"
    gbc0="$(field "$start" get_buffer_calls)";  gbc1="$(field "$end" get_buffer_calls)"
    gbs0="$(field "$start" get_buffer_sum_ms)"; gbs1="$(field "$end" get_buffer_sum_ms)"
    buc0="$(field "$start" buffer_updated_calls)";  buc1="$(field "$end" buffer_updated_calls)"
    bus0="$(field "$start" buffer_updated_sum_ms)"; bus1="$(field "$end" buffer_updated_sum_ms)"
    drc0="$(field "$start" data_received_calls)";  drc1="$(field "$end" data_received_calls)"
    drs0="$(field "$start" data_received_sum_ms)"; drs1="$(field "$end" data_received_sum_ms)"
    sec0="$(field "$start" scan_empty_calls)";  sec1="$(field "$end" scan_empty_calls)"
    ses0="$(field "$start" scan_empty_sum_ms)"; ses1="$(field "$end" scan_empty_sum_ms)"
    rc0="${rc0:-0}"; rc1="${rc1:-0}"; rs0="${rs0:-0}"; rs1="${rs1:-0}"
    rm0="${rm0:-0}"; rm1="${rm1:-0}"; hc0="${hc0:-0}"; hc1="${hc1:-0}"
    hs0="${hs0:-0}"; hs1="${hs1:-0}"
    pc0="${pc0:-0}"; pc1="${pc1:-0}"; ps0="${ps0:-0}"; ps1="${ps1:-0}"
    pm0="${pm0:-0}"; pm1="${pm1:-0}"
    psc0="${psc0:-0}"; psc1="${psc1:-0}"; pss0="${pss0:-0}"; pss1="${pss1:-0}"
    nc0="${nc0:-0}"; nc1="${nc1:-0}"; ns0="${ns0:-0}"; ns1="${ns1:-0}"
    dc0="${dc0:-0}"; dc1="${dc1:-0}"; ds0="${ds0:-0}"; ds1="${ds1:-0}"
    dm0="${dm0:-0}"; dm1="${dm1:-0}"
    nac0="${nac0:-0}"; nac1="${nac1:-0}"; nas0="${nas0:-0}"; nas1="${nas1:-0}"
    gbc0="${gbc0:-0}"; gbc1="${gbc1:-0}"; gbs0="${gbs0:-0}"; gbs1="${gbs1:-0}"
    buc0="${buc0:-0}"; buc1="${buc1:-0}"; bus0="${bus0:-0}"; bus1="${bus1:-0}"
    drc0="${drc0:-0}"; drc1="${drc1:-0}"; drs0="${drs0:-0}"; drs1="${drs1:-0}"
    sec0="${sec0:-0}"; sec1="${sec1:-0}"; ses0="${ses0:-0}"; ses1="${ses1:-0}"

    printf 'pid=%s ticks=%s clk_tck=%s\n' "$CAPTURE_PID" "$((cpu1 - cpu0))" "$CLK_TCK" \
        > "$CAPTURE_SCRATCH/cpu_${CAPTURE_PREFIX}${LABEL}.txt"
    printf 'resp_calls=%s resp_sum_ms=%s resp_max_us=%s\n' \
        "$((rc1 - rc0))" \
        "$(awk -v a="$rs0" -v b="$rs1" 'BEGIN{printf "%.3f", b - a}')" \
        "$(awk -v a="$rm0" -v b="$rm1" 'BEGIN{printf "%.3f", b - a}')" \
        > "$CAPTURE_SCRATCH/resp_${CAPTURE_PREFIX}${LABEL}.txt"
    printf 'handler_calls=%s handler_sum_ms=%s\n' \
        "$((hc1 - hc0))" \
        "$(awk -v a="$hs0" -v b="$hs1" 'BEGIN{printf "%.3f", b - a}')" \
        > "$CAPTURE_SCRATCH/handler_${CAPTURE_PREFIX}${LABEL}.txt"
    printf 'parse_calls=%s parse_sum_ms=%s parse_max_us=%s parse_scan_calls=%s parse_scan_sum_ms=%s null_calls=%s null_sum_ms=%s dispatch_calls=%s dispatch_sum_ms=%s dispatch_max_us=%s null_a_calls=%s null_a_sum_ms=%s get_buffer_calls=%s get_buffer_sum_ms=%s buffer_updated_calls=%s buffer_updated_sum_ms=%s data_received_calls=%s data_received_sum_ms=%s scan_empty_calls=%s scan_empty_sum_ms=%s\n' \
        "$((pc1 - pc0))" \
        "$(awk -v a="$ps0" -v b="$ps1" 'BEGIN{printf "%.3f", b - a}')" \
        "$(awk -v a="$pm0" -v b="$pm1" 'BEGIN{printf "%.3f", b - a}')" \
        "$((psc1 - psc0))" \
        "$(awk -v a="$pss0" -v b="$pss1" 'BEGIN{printf "%.3f", b - a}')" \
        "$((nc1 - nc0))" \
        "$(awk -v a="$ns0" -v b="$ns1" 'BEGIN{printf "%.3f", b - a}')" \
        "$((dc1 - dc0))" \
        "$(awk -v a="$ds0" -v b="$ds1" 'BEGIN{printf "%.3f", b - a}')" \
        "$(awk -v a="$dm0" -v b="$dm1" 'BEGIN{printf "%.3f", b - a}')" \
        "$((nac1 - nac0))" \
        "$(awk -v a="$nas0" -v b="$nas1" 'BEGIN{printf "%.3f", b - a}')" \
        "$((gbc1 - gbc0))" \
        "$(awk -v a="$gbs0" -v b="$gbs1" 'BEGIN{printf "%.3f", b - a}')" \
        "$((buc1 - buc0))" \
        "$(awk -v a="$bus0" -v b="$bus1" 'BEGIN{printf "%.3f", b - a}')" \
        "$((drc1 - drc0))" \
        "$(awk -v a="$drs0" -v b="$drs1" 'BEGIN{printf "%.3f", b - a}')" \
        "$((sec1 - sec0))" \
        "$(awk -v a="$ses0" -v b="$ses1" 'BEGIN{printf "%.3f", b - a}')" \
        > "$CAPTURE_SCRATCH/parse_${CAPTURE_PREFIX}${LABEL}.txt"

    rm -f "$STATE.cpu0" "$STATE.snap" "$STATE.snap_seq"
    exit 0
fi

echo "scenario_capture.sh: unknown mode '$MODE'" >&2
exit 2
