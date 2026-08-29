#!/usr/bin/env bash
# hotpath_ab.sh — BlackBull(+uvloop) vs FastAPI(uvicorn+uvloop+httptools).
#
# Question: HttpArena's `baseline` profile puts FastAPI ~6 % ahead of
# BlackBull+uvloop.  Is that a per-request hot-path cost, or does it come
# from something the leaderboard entry mounts on top of the framework, or
# from the scale it is run at?  Three phases, one box, one session:
#
#   1  cost per request   1 worker pinned to ONE core, 64 conns.  The core
#                         saturates, so throughput is the direct inverse of
#                         CPU-seconds per request.  Bare app and the
#                         HttpArena middleware stack (PEER_MW=httparena)
#                         are both measured, for both frameworks.
#   2  attribution        py-spy over the same single-worker load.
#   3  scale              3 workers on 3 physical cores, 512 conns — the
#                         shape the leaderboard actually runs.
#
# Arms are interleaved rather than run as one block each, so a monotonic
# drift (thermal, page cache) cannot alias onto the arm.  SMT siblings are
# (2n, 2n+1) here, so a generator on core 3 would share silicon with a
# server on core 2.
#
# Companions in this directory, run against the output dir this prints:
#   hotpath_summary.py <dir>   per-phase tables + inside/outside-noise verdicts
#   folded.py <dir>/raw/X.folded   self/inclusive rollup of a py-spy profile
#   parser_micro.py            `_parse` vs httptools on byte-identical requests
#
# Findings: BLA-A-2 [private]
set -u

REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
cd "$REPO" || exit 1

PORT="${PORT:-8501}"
GEN_CPUS="${GEN_CPUS:-6,8,10}"
WARMUP="${WARMUP:-5}"
DURATION="${DURATION:-15}"
CYCLES="${CYCLES:-3}"
SAMPLE_HZ="${SAMPLE_HZ:-200}"
PROFILE_SECS="${PROFILE_SECS:-60}"
BASELINE_Q='/baseline11?a=1&b=2&c=3'
PHASES="${PHASES:-1 3 2 4 5}"

has_phase() { case " $PHASES " in *" $1 "*) return 0 ;; *) return 1 ;; esac; }

TS=$(date -u +%Y%m%d-%H%M%SZ)
OUT="${OUT:-$REPO/bench/results/hotpath-ab/$TS}"
mkdir -p "$OUT/raw"
cp "$0" "$OUT/hotpath_ab.sh"

ulimit -n 65536 2>/dev/null || true
TICKS=$(getconf CLK_TCK)
SRV_PGID=""
WRAP_PID=""
MY_PGID=$(ps -o pgid= -p $$ | tr -d ' ')

log() { echo "[$(date -u +%H:%M:%S)] $*" | tee -a "$OUT/run.log"; }

# --- process bookkeeping ---------------------------------------------------

pgid_pids() { pgrep -g "$1" 2>/dev/null; }
port_pid()  { fuser "$PORT/tcp" 2>/dev/null | awk '{print $1}'; }

# utime+stime in clock ticks, summed over the arm's whole process group.
# 10 ms resolution over a 15 s window is 0.07 % — `ps -o cputimes` reports
# whole seconds only and could not support a verdict on a few percent.
pgid_cpu_ticks() {
    local pid total=0 one
    for pid in $(pgid_pids "$1"); do
        # comm can contain spaces and parens, so split on the LAST ') '.
        one=$(awk '{n=split($0,a,") "); m=split(a[n],b," "); print b[12]+b[13]}' \
              "/proc/$pid/stat" 2>/dev/null) || continue
        total=$(( total + ${one:-0} ))
    done
    echo "$total"
}

stop_arm() {
    # Signal the group only when the arm really has its own group.  The
    # profiled launch skips setsid, so the server sits in THIS script's
    # group — a group-wide TERM there would kill the run itself.
    if [ -n "$SRV_PGID" ] && [ "$SRV_PGID" != "$MY_PGID" ]; then
        kill -TERM -"$SRV_PGID" 2>/dev/null
    fi
    [ -n "$WRAP_PID" ] && kill -TERM "$WRAP_PID" 2>/dev/null
    SRV_PGID=""; WRAP_PID=""
    fuser -k "$PORT/tcp" 2>/dev/null
    for _ in $(seq 1 60); do
        fuser -s "$PORT/tcp" 2>/dev/null || return 0
        sleep 0.25
    done
    return 0
}
trap 'stop_arm' EXIT

wait_ready() {
    local i
    for i in $(seq 1 200); do
        # -f matters: without it curl exits 0 on an error page, so a dead
        # server reads as ready and gets benchmarked against the error.
        curl -sf --max-time 1 "http://127.0.0.1:$PORT/ping" >/dev/null 2>&1 && return 0
        sleep 0.25
    done
    echo "FATAL: $1 never answered on :$PORT" | tee -a "$OUT/run.log"
    tail -25 "$OUT/raw/$1.server.log" 2>/dev/null
    return 1
}

# The silent-fallback failure mode: a server that quietly fell back to the
# stock selector loop, or to the pure-Python parser, still produces a full
# and plausible set of numbers.  Prove the accelerated libraries are mapped.
assert_libs() {
    local arm="$1"; shift
    local pid want hit found=""
    for want in "$@"; do
        hit=""
        for pid in $(pgid_pids "$SRV_PGID"); do
            grep -qi "$want" "/proc/$pid/maps" 2>/dev/null && { hit=$pid; break; }
        done
        [ -z "$hit" ] && { echo "FATAL: $arm — $want is not mapped into any server process" \
            | tee -a "$OUT/run.log"; return 1; }
        found="$found $want"
    done
    log "  $arm: $(pgid_pids "$SRV_PGID" | wc -l) procs, libs$found"
}

# start_arm <arm> <workers> <srv_cpus> [wrapper argv...]
start_arm() {
    local arm="$1" workers="$2" cpus="$3"; shift 3
    local mw="" a="$arm"
    case "$arm" in *-mw) mw=httparena; a="${arm%-mw}" ;; esac
    local -a cmd
    case "$a" in
        blackbull)
            cmd=(env BB_UVLOOP=1 "BB_WORKERS=$workers" BB_ACCESS_LOG=0 "PEER_MW=$mw"
                 taskset -c "$cpus"
                 "$REPO/.venv/bin/blackbull" bench.peers.native_app:app
                 --bind "127.0.0.1:$PORT")
            ;;
        fastapi)
            # --loop / --http named rather than `auto`: provenance must not
            # depend on what happens to be installed.
            cmd=(env "PEER_MW=$mw"
                 taskset -c "$cpus"
                 "$REPO/.venv/bin/uvicorn" bench.peers.fastapi_app:app
                 --host 127.0.0.1 --port "$PORT"
                 --loop uvloop --http httptools --workers "$workers"
                 --log-level warning --no-access-log)
            ;;
        *) echo "unknown arm $arm"; return 1 ;;
    esac
    stop_arm
    if [ "$#" -gt 0 ]; then
        # Profiled launch.  Deliberately NOT setsid: job control is on in
        # this shell, so setsid forks and $! would name a process that exits
        # at once — phase 2 must be able to wait for the profiler itself.
        "$@" "${cmd[@]}" >"$OUT/raw/$arm.server.log" 2>&1 &
        WRAP_PID=$!
    else
        setsid nohup "${cmd[@]}" >"$OUT/raw/$arm.server.log" 2>&1 &
        WRAP_PID=""
    fi
    wait_ready "$arm" || return 1
    # Derive the group from whoever owns the port, not from $! — the
    # wrapper may or may not be the group leader.
    SRV_PGID=$(ps -o pgid= -p "$(port_pid)" 2>/dev/null | tr -d ' ')
    [ -n "$SRV_PGID" ] || { echo "FATAL: $arm — cannot find the server pgid"; return 1; }
    case "$a" in
        blackbull) assert_libs "$arm" uvloop || return 1 ;;
        fastapi)   assert_libs "$arm" uvloop httptools || return 1 ;;
    esac
}

# measure <arm> <phase> <cycle> <path> <conns> <threads> [wrk -H args...]
measure() {
    local arm="$1" phase="$2" cyc="$3" p="$4" conns="$5" thr="$6" f t0 t1 secs reqs rps
    shift 6
    f="$OUT/raw/wrk-$phase-$arm-${p//[^a-zA-Z0-9]/_}-c$cyc.txt"
    taskset -c "$GEN_CPUS" wrk -t"$thr" -c"$conns" -d"${WARMUP}s" "$@" \
        "http://127.0.0.1:$PORT$p" >/dev/null 2>&1
    t0=$(pgid_cpu_ticks "$SRV_PGID")
    taskset -c "$GEN_CPUS" wrk -t"$thr" -c"$conns" -d"${DURATION}s" --latency "$@" \
        "http://127.0.0.1:$PORT$p" >"$f" 2>&1
    t1=$(pgid_cpu_ticks "$SRV_PGID")
    secs=$(awk -v a="$t0" -v b="$t1" -v k="$TICKS" 'BEGIN{printf "%.3f",(b-a)/k}')
    reqs=$(awk '/requests in/{print $1}' "$f")
    rps=$(awk '/Requests\/sec/{print $2}' "$f")
    printf 'cpu_seconds=%s requests=%s arm=%s phase=%s path=%s conns=%s\n' \
        "$secs" "$reqs" "$arm" "$phase" "$p" "$conns" >>"$f"
    log "  $arm $p c$cyc: $rps req/s | ${secs}s CPU / ${DURATION}s wall | $reqs reqs"
}

# --- phase 1: cost per request --------------------------------------------

if has_phase 1; then
log "phase 1 — one worker on one core (cpu 2), 64 conns"
for cyc in $(seq 1 "$CYCLES"); do
    for arm in blackbull fastapi blackbull-mw fastapi-mw; do
        start_arm "$arm" 1 2 || exit 1
        for p in /ping "$BASELINE_Q"; do
            measure "$arm" p1 "$cyc" "$p" 64 3
        done
        stop_arm
    done
done
fi

# --- phase 5: what the router costs as the table grows --------------------
# /ping is the first route in both apps, /pingz the last and byte-identical.
# BlackBull probes a dict; starlette runs a regex per candidate in order.

if has_phase 5; then
log "phase 5 — route position: /ping (first) vs /pingz (last)"
for cyc in $(seq 1 "$CYCLES"); do
    for arm in blackbull fastapi; do
        start_arm "$arm" 1 2 || exit 1
        for p in /ping /pingz "$BASELINE_Q"; do
            measure "$arm" p5 "$cyc" "$p" 64 3
        done
        stop_arm
    done
done
fi

# --- phase 4: what a header costs -----------------------------------------
# The one structural difference between the stacks is the parser: BlackBull
# validates every field line in Python (an architectural rule), uvicorn
# hands the bytes to httptools in C.  wrk sends one header by default, which
# is the friendliest possible case for the Python parser; real clients send
# more.  Same endpoint, same body — only the request head grows.

if has_phase 4; then
log "phase 4 — request header count sensitivity on /ping"
H3=(-H 'User-Agent: Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36'
    -H 'Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8'
    -H 'Accept-Language: en-US,en;q=0.9')
H7=("${H3[@]}"
    -H 'Cookie: session=8f14e45fceea167a5a36dedd4bea2543'
    -H 'Referer: http://127.0.0.1:8501/index.html'
    -H 'X-Request-Id: 3f2504e0-4f89-11d3-9a0c-0305e82c3301'
    -H 'X-Forwarded-For: 203.0.113.7')
for cyc in $(seq 1 "$CYCLES"); do
    for arm in blackbull fastapi; do
        start_arm "$arm" 1 2 || exit 1
        measure "$arm" p4h1 "$cyc" /ping 64 3
        measure "$arm" p4h4 "$cyc" /ping 64 3 "${H3[@]}"
        measure "$arm" p4h8 "$cyc" /ping 64 3 "${H7[@]}"
        stop_arm
    done
done
fi

# --- phase 3: the shape the leaderboard runs ------------------------------
# Run before the profiles so a profiler crash cannot cost the scale data.

if has_phase 3; then
log "phase 3 — 3 workers on cpus 0,2,4, 512 conns"
for cyc in $(seq 1 "$CYCLES"); do
    for arm in blackbull fastapi; do
        start_arm "$arm" 3 0,2,4 || exit 1
        measure "$arm" p3 "$cyc" "$BASELINE_Q" 512 3
        stop_arm
    done
done
fi

# --- phase 2: where the cycles go -----------------------------------------
#
# Sampled in its own run: py-spy pauses the process on every sample, so the
# cost-per-request numbers above must come from unsampled runs.  Spawn form
# is mandatory — ptrace_scope=1 refuses `-p` attach to a sibling process,
# but any process may ptrace its own descendants.  Blocking sampling, not
# --nonblocking: on a saturated core nonblocking dropped ~45 % of samples,
# and a dropped sample is not random — reads tear precisely when frames
# churn fastest.  py-spy is left unpinned so it does not eat the server's
# core.

if has_phase 2; then
log "phase 2 — py-spy attribution (${SAMPLE_HZ} Hz, ${PROFILE_SECS}s, $BASELINE_Q)"
for arm in blackbull fastapi; do
    # --nonblocking is mandatory here, not a tuning preference.  Blocking
    # mode ptrace-stops the process for every sample; on this box that took
    # the server from ~28 000 req/s down to 14 req/s, and a profile taken at
    # 14 req/s describes connection setup, not the hot path.  Nonblocking
    # discards samples it cannot read cleanly (counted as Errors) but leaves
    # the server running.  The wrk result is recorded next to the profile
    # precisely so the profile can be thrown out if the rate collapsed.
    start_arm "$arm" 1 2 \
        "$REPO/.venv/bin/py-spy" record -f raw -r "$SAMPLE_HZ" --nonblocking \
            -d "$PROFILE_SECS" -o "$OUT/raw/$arm.folded" -- \
        || exit 1
    taskset -c "$GEN_CPUS" wrk -t3 -c64 -d"$((PROFILE_SECS - 5))s" \
        "http://127.0.0.1:$PORT$BASELINE_Q" >"$OUT/raw/pyspy-load-$arm.txt" 2>&1
    wait "$WRAP_PID"
    log "  $arm: $(rg -N -o 'Samples: [0-9]+ Errors: [0-9]+' "$OUT/raw/$arm.server.log" | tail -1) | under load: $(awk '/Requests\/sec/{print $2}' "$OUT/raw/pyspy-load-$arm.txt") req/s"
    stop_arm
done
fi

# --- provenance ------------------------------------------------------------

{
    echo "# hotpath A/B provenance"
    echo
    echo "- when: $TS"
    echo "- host: $(uname -srm)"
    echo "- cpu: $(rg -m1 'model name' /proc/cpuinfo | cut -d: -f2- | sed 's/^ //') ($(nproc) threads, SMT siblings 2n/2n+1)"
    echo "- generator cores: $GEN_CPUS (wrk)"
    echo "- phase 1/2: 1 worker on cpu 2, wrk -t3 -c64"
    echo "- phase 3: 3 workers on cpus 0,2,4, wrk -t3 -c512"
    echo "- window: ${WARMUP}s warmup + ${DURATION}s measure, $CYCLES interleaved cycles"
    echo "- endpoints: /ping (4-byte pre-encoded body) and $BASELINE_Q (HttpArena's baseline handler)"
    echo "- '-mw' arms: PEER_MW=httparena — BlackBull adds Compression()+static(); FastAPI adds GZipMiddleware(1000)+/static mount"
    echo "- blackbull: $("$REPO/.venv/bin/python" -c 'import blackbull;print(blackbull.__version__)') @ $(git -C "$REPO" rev-parse --short HEAD)"
    echo "- peers: $("$REPO/.venv/bin/python" -c 'import uvicorn,starlette,fastapi,httptools,uvloop;print(f"uvicorn {uvicorn.__version__}, starlette {starlette.__version__}, fastapi {fastapi.__version__}, httptools {httptools.__version__}, uvloop {uvloop.__version__}")')"
    echo "- python: $("$REPO/.venv/bin/python" -V)"
    echo "- wrk: $(wrk --version 2>&1 | head -1)"
} > "$OUT/provenance.md"

log "done -> $OUT"
echo "$OUT"
