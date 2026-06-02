#!/usr/bin/env bash
# bench/peers/compare_servers.sh — five-server head-to-head on the shared
# ASGI app (bench/peers/asgi_app.py).
#
# Drives all 4 lanes from CHARACTERIZATION.md against each of:
#   blackbull, uvicorn, hypercorn, granian, daphne
#
# Each stack is brought up cleanly via bench/peers/run_peer.sh, exercised,
# and torn down. Output is a single dated markdown report.
#
# Usage:
#   bash bench/peers/compare_servers.sh                  # all stacks, all lanes
#   STACKS="blackbull hypercorn" bash bench/peers/compare_servers.sh
#   LANES="A B-wrk" bash bench/peers/compare_servers.sh
#
# Env:
#   STACKS    space-separated subset (default: all five)
#   LANES     space-separated subset of {A, B-wrk, B-oha, C, D} (default: all)
#   PORT      bind port (default 8443)
#   DURATION  per-scenario seconds for wrk/oha (default 60 from Sprint 24;
#             was 30 in Sprints 13–23 — set DURATION=30 to reproduce older
#             numbers)
#   WARMUP    server warmup seconds before any measured run (default 15);
#             one wrk pass at /plaintext c=64, output discarded.  Set to 0
#             to skip — older Sprint 13–23 numbers were captured without
#             this pass.
#   RUNS      h2load runs per scenario (median picked; default 3)

set -e

BASE_PORT="${PORT:-8443}"
# Sprint 20: $BENCH_TARGET_HOST replaces the hard-coded `localhost` in every
# $BASE URL the load tools see.  Default keeps loopback semantics for the
# single-instance harness; split-topology sets BENCH_TARGET_HOST to a name
# that resolves to the server instance's VPC private IP (with cert SAN +
# /etc/hosts entry in place so TLS verification keeps working).
BENCH_TARGET_HOST="${BENCH_TARGET_HOST:-localhost}"
# BASE is now per-stack — set inside bench_stack() via compute_base() so
# the Sprint 14 *-cleartext stacks target http:// and *-nginx / *-h11 stacks
# stay on https://.  Initial value is the standalone-TLS default so the
# pre-loop health check / report header still work.
BASE="https://${BENCH_TARGET_HOST}:${BASE_PORT}"

# Sprint 20: when BENCH_REMOTE_LIFECYCLE=1 the launcher / kill / readiness
# checks dispatch over SSH to a second instance instead of running locally.
# BENCH_REMOTE_SSH is the ssh prefix (e.g. `ssh -i ~/.ssh/server.pem ... ubuntu@10.0.0.5`).
# BENCH_REMOTE_REPO is the absolute path of the BlackBull checkout on the
# server instance (must already contain the regenerated cert with the right SANs).
# BENCH_BIND_HOST is the BIND_HOST that the remote launcher passes to run_peer.sh.
BENCH_REMOTE_LIFECYCLE="${BENCH_REMOTE_LIFECYCLE:-0}"
BENCH_REMOTE_SSH="${BENCH_REMOTE_SSH:-}"
BENCH_REMOTE_REPO="${BENCH_REMOTE_REPO:-/home/ubuntu/BlackBull}"
BENCH_BIND_HOST="${BENCH_BIND_HOST:-127.0.0.1}"

if [ "$BENCH_REMOTE_LIFECYCLE" = "1" ] && [ -z "$BENCH_REMOTE_SSH" ]; then
    echo "compare_servers.sh: BENCH_REMOTE_LIFECYCLE=1 requires BENCH_REMOTE_SSH to be set" >&2
    exit 1
fi
CERT="tests/cert.pem"
KEY="tests/key.pem"
RUNS="${RUNS:-3}"
# Sprint 24: default duration raised from 30 s to 60 s for the
# wrk/oha lanes.  30 s left allocator-state,
# kernel-pacing, and TLS-session-cache transients in the measured
# window.  Older numbers in CHARACTERIZATION.md were captured at 30 s;
# Sprint 24+ rows are 60 s.  Override via env to reproduce.
DURATION="${DURATION:-60}"
# Sprint 24: explicit server warmup pass before any lane runs.  One
# 15 s wrk burst against /plaintext c=64 — discards output, just nudges
# Python allocator + kernel TCP autotune + TLS session-cache to
# steady-state before the measured runs start.
WARMUP="${WARMUP:-15}"

STACKS_ALL="blackbull uvicorn hypercorn granian daphne nginx"
# Sprint 24 added Lane E (connection churn).  Defaulting it off in the
# all-lanes set so existing AWS runs (and the cost envelope they assume)
# don't grow without intent — opt in with LANES="A B-wrk B-oha C D E-wrk".
LANES_ALL="A B-wrk B-oha C D"
STACKS="${STACKS:-$STACKS_ALL}"
LANES="${LANES:-$LANES_ALL}"

# Servers that support HTTP/2 (lane A applies).  Sprint 14 variants
# (*-cleartext, *-nginx, *-h11) are intentionally NOT listed — Lane A
# would either not negotiate (cleartext) or measure nginx-frontend H2
# (which isn't apples-to-apples with the standalone H2 numbers).
# Sprint 16 *-w<N> variants are checked against the base name via
# strip_worker_suffix() below, so multi-worker stacks inherit capabilities.
SUPPORTS_H2="blackbull hypercorn granian nginx"
# Servers that DO NOT support /echo POST or /ws — those scenarios are skipped
# automatically by the orchestrator's health check (server returns 405 / no WS).
NO_POST_NO_WS="nginx"

# Sprint 16: strip the -w<N> worker-count suffix so blackbull-w2 etc. match
# the SUPPORTS_H2 / NO_POST_NO_WS lists exactly like plain "blackbull".
# Other suffixes (-cleartext, -nginx, -h11) are intentionally NOT stripped —
# those variants have different capability semantics by design (Sprint 14).
strip_worker_suffix() {
    case "$1" in
        *-w[0-9]|*-w[0-9][0-9]) echo "${1%-w*}" ;;
        *)                       echo "$1" ;;
    esac
}

# Per-stack BASE URL.  Sprint 14 introduces three suffix conventions;
# Sprint 16 adds a fourth (multi-worker):
#   *-cleartext     → http  on $BASE_PORT (no TLS on the server)
#   *-nginx         → https on $BASE_PORT (TLS terminated by nginx, HTTP upstream)
#   *-h11           → https on $BASE_PORT (uvicorn with --http h11)
#   blackbull-w<N>  → https on $BASE_PORT (BlackBull with N workers, e.g. -w4)
#   (no suffix)     → https on $BASE_PORT (standalone TLS — current default)
compute_base() {
    case "$1" in
        *-cleartext) echo "http://${BENCH_TARGET_HOST}:${BASE_PORT}" ;;
        *)           echo "https://${BENCH_TARGET_HOST}:${BASE_PORT}" ;;
    esac
}

RESULT_DIR="bench/results"
mkdir -p "$RESULT_DIR"
TS="$(date +%Y%m%d-%H%M%S)"
OUT="$RESULT_DIR/compare_servers_${TS}.md"
SCRATCH="$RESULT_DIR/scratch_${TS}"
mkdir -p "$SCRATCH"

# Locate mkcert CA so curl/h2load/k6 trust the self-signed cert.
MKCERT_CA=""
for path in \
    /mnt/c/Users/*/AppData/Local/mkcert/rootCA.pem \
    "$HOME/.local/share/mkcert/rootCA.pem" \
    /usr/local/share/ca-certificates/mkcert-rootCA.crt; do
    found=$(ls $path 2>/dev/null | head -1)
    [ -n "$found" ] && { MKCERT_CA="$found"; break; }
done
[ -n "$MKCERT_CA" ] && export SSL_CERT_FILE="$MKCERT_CA"

# ----------------------------------------------------------------------------
# Lifecycle helpers
# ----------------------------------------------------------------------------

kill_existing_local() {
    # Kill by listening port — robust against forked workers whose cmdlines
    # no longer contain the launcher's name (hypercorn multiprocessing
    # workers lose "hypercorn" from argv after fork, which is how today's
    # hypercorn orphans answered to "granian" and "daphne" benchmarks).
    if command -v fuser >/dev/null 2>&1; then
        fuser -k -9 -n tcp "$BASE_PORT"  2>/dev/null || true
        fuser -k -9 -n tcp "$((BASE_PORT+1))" 2>/dev/null || true  # daphne fallback
    fi
    # Belt-and-suspenders by name in case the kill above missed (e.g.
    # process is not yet listening because it crashed at bind time but
    # left a child around).
    pkill -9 -f "bench/app.py"         2>/dev/null || true
    pkill -9 -f "bench.peers.asgi_app" 2>/dev/null || true
    pkill -9 -f "hypercorn"            2>/dev/null || true
    pkill -9 -f "uvicorn"              2>/dev/null || true
    pkill -9 -f "granian"              2>/dev/null || true
    pkill -9 -f "daphne"               2>/dev/null || true
    pkill -9 -f "nginx.*bench/peers"   2>/dev/null || true
    # Wait for the port to actually be free before returning.
    for _ in $(seq 1 20); do
        if ! ss -tln 2>/dev/null | grep -q ":$BASE_PORT "; then
            return 0
        fi
        sleep 0.5
    done
    echo "WARNING: port $BASE_PORT still bound after kill_existing" >&2
    ss -tlnp 2>/dev/null | grep ":$BASE_PORT " >&2 || true
    return 0   # never abort the orchestrator on a stuck port — wait_ready handles validation
}

kill_existing() {
    if [ "$BENCH_REMOTE_LIFECYCLE" = "1" ]; then
        # Server lives on a remote instance — dispatch via SSH.  The remote
        # helper mirrors kill_existing_local's by-port + by-name logic.
        $BENCH_REMOTE_SSH "cd $BENCH_REMOTE_REPO && BASE_PORT=$BASE_PORT bash bench/peers/server_lifecycle_remote.sh kill_existing" 2>/dev/null || true
        return 0
    fi
    kill_existing_local
}

# Argument: expected server PID (the one we just backgrounded).
# wait_ready confirms (1) the port comes up, (2) it's held by the expected
# PID or one of its descendants — so the response can't be an orphan from
# the previous section.
#
# In remote-lifecycle mode the local orchestrator cannot see the server's
# process tree, so we drop the PID-descendant check and trust that the
# preceding kill_existing left the port clean.  The HTTP probe is the only
# remaining readiness signal — same shape as Sprint 16's between-lane
# health_check, just used at startup instead of between lanes.
wait_ready() {
    local expected_pid="$1"
    for _ in $(seq 1 30); do
        if curl -sk --max-time 2 "$BASE/plaintext" 2>/dev/null | grep -q "Hello"; then
            if [ "$BENCH_REMOTE_LIFECYCLE" = "1" ]; then
                return 0
            fi
            # Confirm the listener belongs to our spawned tree.
            local listener_pid
            listener_pid=$(ss -tlnp 2>/dev/null \
                | awk -v p=":$BASE_PORT" '$0 ~ p {
                    if (match($0, /pid=([0-9]+)/, a)) print a[1]
                  }' | head -1)
            if [ -z "$listener_pid" ]; then
                # ss may be unavailable in the namespace; trust the HTTP response.
                return 0
            fi
            if [ "$listener_pid" = "$expected_pid" ] \
               || is_descendant_of "$listener_pid" "$expected_pid"; then
                return 0
            fi
            echo "ERROR: port $BASE_PORT held by PID $listener_pid, not our spawn $expected_pid (orphan from previous stack?)" >&2
            return 1
        fi
        sleep 1
    done
    echo "ERROR: server not ready at $BASE" >&2
    return 1
}

is_descendant_of() {
    # Walk parent pointers until we hit the expected pid or PID 1.
    local pid="$1" want="$2"
    for _ in $(seq 1 8); do
        [ "$pid" = "$want" ] && return 0
        [ "$pid" = "1" ] || [ -z "$pid" ] && return 1
        pid=$(ps -o ppid= -p "$pid" 2>/dev/null | tr -d ' ')
    done
    return 1
}

contains_word() {
    local needle="$1"; shift
    for w in "$@"; do [ "$w" = "$needle" ] && return 0; done
    return 1
}

# ----------------------------------------------------------------------------
# Lane runners
# ----------------------------------------------------------------------------

run_lane_a_h2load() {
    local label="$1"
    {
        echo ""
        echo "### $label — Lane A (HTTP/2, h2load)"
        echo ""
        # h2load 'time for request:' line is: min  max  mean  sd  +/- sd
        echo "| Scenario | req/s | mean | sd | min | max | succeeded |"
        echo "|---|---|---|---|---|---|---|"
        for entry in \
            "A1_plaintext_mux1     50000 50 1   /plaintext" \
            "A2_plaintext_mux10    90000 50 10  /plaintext" \
            "A3_plaintext_mux50    90000 50 50  /plaintext" \
            "A4_json_mux10         50000 50 10  /json" \
            "A5_16kb_mux10         50000 50 10  /16kb" \
            "A6_64kb_mux10         30000 50 10  /64kb" \
            "A7_1mb_mux3            3000 20 3   /1mb"; do
            read -r sname n c m path <<< "$entry"
            local rawfile="$SCRATCH/h2load_${label}_${sname}.txt"
            local out
            out=$(h2load -n "$n" -c "$c" -m "$m" "$BASE$path" 2>&1 | tee "$rawfile")
            local rps min max mean sd succ
            rps=$(echo "$out"  | grep "finished in" | awk -F',' '{print $2}' | awk '{print $1}')
            min=$(echo  "$out" | grep "time for request:" | awk '{print $4}')
            max=$(echo  "$out" | grep "time for request:" | awk '{print $5}')
            mean=$(echo "$out" | grep "time for request:" | awk '{print $6}')
            sd=$(echo   "$out" | grep "time for request:" | awk '{print $7}')
            succ=$(echo "$out" | grep "requests:" | awk '{for(i=1;i<=NF;i++) if($i=="succeeded,") print $(i-1)}')
            echo "| $sname | $rps | $mean | $sd | $min | $max | $succ |"
        done
    } >> "$OUT"
}

run_lane_b_wrk() {
    local label="$1"
    local skip_post=0
    contains_word "$(strip_worker_suffix "$label")" $NO_POST_NO_WS && skip_post=1
    {
        echo ""
        echo "### $label — Lane B-wrk (HTTP/1.1, wrk + wrk2)"
        [ "$skip_post" = "1" ] && echo "" && \
            echo "_$label is a static reference — B6/B7 (POST /echo) skipped._"
        echo ""
        BASE="$BASE" OUTDIR="$SCRATCH" DURATION="$DURATION" \
            LABEL_PREFIX="${label}_" SKIP_POST="$skip_post" \
            bash bench/wrk/run.sh
        # Append the wrk2 row (CO-corrected p99 at fixed rate). Same
        # markdown columns; emits one extra row "B2r_plaintext_rate5000".
        BASE="$BASE" OUTDIR="$SCRATCH" DURATION="$DURATION" \
            LABEL_PREFIX="${label}_" \
            bash bench/wrk2/run.sh
    } >> "$OUT"
}

run_lane_e_wrk() {
    local label="$1"
    {
        echo ""
        echo "### $label — Lane E (connection churn, wrk no-keepalive)"
        echo ""
        BASE="$BASE" OUTDIR="$SCRATCH" DURATION="$DURATION" \
            LABEL_PREFIX="${label}_" \
            bash bench/wrk/lane_e.sh
    } >> "$OUT"
}

run_lane_b_oha() {
    local label="$1"
    local skip_post=0
    contains_word "$(strip_worker_suffix "$label")" $NO_POST_NO_WS && skip_post=1
    {
        echo ""
        echo "### $label — Lane B-oha (HTTP/1.1, oha)"
        [ "$skip_post" = "1" ] && echo "" && \
            echo "_$label is a static reference — B6/B7 (POST /echo) skipped._"
        echo ""
        BASE="$BASE" OUTDIR="$SCRATCH" DURATION="$DURATION" \
            LABEL_PREFIX="${label}_" SKIP_POST="$skip_post" \
            bash bench/oha/run.sh
    } >> "$OUT"
}

run_lane_c_k6() {
    local label="$1"
    local json_c1="$SCRATCH/k6_${label}_c1.json"
    local json_c2="$SCRATCH/k6_${label}_c2.json"

    # C1 — 200 VU.  BASE is exported so the k6 script picks up the
    # actual benchmark target (Sprint 20 split topology points BASE at
    # bench-server.internal rather than localhost).
    K6_VUS=200 K6_DURATION=60s BASE="$BASE" \
        k6 run --quiet --summary-export="$json_c1" \
            --summary-trend-stats="p(50),p(95),p(99),max" \
            bench/k6/http_stress.js >/dev/null 2>&1 || true
    # C2 — 500 VU (default)
    BASE="$BASE" \
        k6 run --quiet --summary-export="$json_c2" \
            --summary-trend-stats="p(50),p(95),p(99),max" \
            bench/k6/http_stress.js >/dev/null 2>&1 || true

    {
        echo ""
        echo "### $label — Lane C (k6 stress)"
        echo ""
        echo "_The \`proto\` column reads HTTP/2 when ≥99% of responses"
        echo "were served over h2 (k6 negotiates via ALPN), HTTP/1.1 when"
        echo "≤1%, otherwise mixed.  Stacks without h2 support fall back"
        echo "to HTTP/1.1; stacks without TLS (cleartext) can't negotiate"
        echo "ALPN and stay on HTTP/1.1._"
        echo ""
        echo "| Scenario | VUs | proto | req/s | p50 | p95 | p99 | max | err% |"
        echo "|---|---|---|---|---|---|---|---|---|"
        for entry in "C1 200 $json_c1" "C2 500 $json_c2"; do
            read -r sname vu file <<< "$entry"
            python3 - "$file" "$sname" "$vu" <<'PYEOF'
import json, sys
try:
    d = json.loads(open(sys.argv[1]).read())
    m = d['metrics']
    dur = m['http_req_duration']
    reqs = m['http_reqs']['rate']
    errs = m.get('http_req_failed', {}).get('value', 0) * 100
    # http_stress.js logs h2Rate = 1 when res.proto == 'HTTP/2.0'.
    # Read the fraction and label the row.
    h2 = m.get('http2_ok', {}).get('value', None)
    if h2 is None:
        proto = '?'
    elif h2 >= 0.99:
        proto = 'HTTP/2'
    elif h2 <= 0.01:
        proto = 'HTTP/1.1'
    else:
        proto = f'mixed ({h2*100:.0f}%h2)'
    print(f"| {sys.argv[2]} | {sys.argv[3]} | {proto} | {reqs:.0f} | "
          f"{dur['p(50)']:.2f} | {dur['p(95)']:.2f} | "
          f"{dur['p(99)']:.2f} | {dur['max']:.2f} | {errs:.2f}% |")
except Exception as e:
    print(f"| {sys.argv[2]} | {sys.argv[3]} | err | err | err | err | err | err | err | ({e})")
PYEOF
        done
    } >> "$OUT"
}

run_lane_d_ws() {
    local label="$1"
    local json_d="$SCRATCH/k6_ws_${label}.json"
    # Include avg in summary stats — k6's Trend stores sub-ms accuracy in
    # avg even though percentile buckets are ms-quantized.
    BASE="$BASE" k6 run --quiet --summary-export="$json_d" \
        --summary-trend-stats="avg,p(50),p(95),p(99),max" \
        bench/k6/websocket.js >/dev/null 2>&1 || true
    {
        echo ""
        echo "### $label — Lane D (WebSocket RTT, k6)"
        echo ""
        echo "_RTT measured in ms; k6 WS context has no high-resolution timer,_"
        echo "_so sub-ms samples quantize to 0 ms. **avg** keeps sub-ms accuracy._"
        echo ""
        python3 - "$json_d" <<'PYEOF'
import json, sys
try:
    d = json.loads(open(sys.argv[1]).read())
    m = d['metrics']
    rtt = m.get('ws_rtt_ms') or m.get('rtt') or m.get('ws_rtt') or {}
    rate = m.get('ws_msgs_received', {}).get('rate', 0)
    if rtt:
        print(f"| msg/s | rtt avg | rtt p50 | rtt p95 | rtt p99 | rtt max |")
        print(f"|---|---|---|---|---|---|")
        # avg is sub-ms (k6 stores float); the others are integer ms because
        # k6's WS context has no sub-ms timer. Use :.0f for the integer columns.
        print(f"| {rate:.0f} | {rtt.get('avg', 0):.3f} ms | "
              f"{rtt.get('p(50)', 0):.0f} | {rtt.get('p(95)', 0):.0f} | "
              f"{rtt.get('p(99)', 0):.0f} | {rtt.get('max', 0):.0f} |")
    else:
        print(f"(no rtt metric; ws_msgs_received rate = {rate:.0f}/s)")
except Exception as e:
    print(f"(parse error: {e})")
PYEOF
    } >> "$OUT"
}

# ----------------------------------------------------------------------------
# Per-stack driver
# ----------------------------------------------------------------------------

bench_stack() {
    local stack="$1"

    # Sprint 14: per-stack BASE.  *-cleartext stacks target http://, the
    # rest stay on https://.  All downstream helpers (health_check, the
    # lane runners) read $BASE from the outer scope.
    BASE="$(compute_base "$stack")"

    echo ""
    echo "=========================================="
    echo "Benchmarking: $stack  (target: $BASE)"
    echo "=========================================="
    {
        echo ""
        echo "## $stack"
        echo ""
        echo "_Target URL: ${BASE}_"
    } >> "$OUT"

    kill_existing

    echo "Starting $stack ..."
    local server_pid=0
    if [ "$BENCH_REMOTE_LIFECYCLE" = "1" ]; then
        # Remote mode: server_lifecycle_remote.sh handles backgrounding +
        # log redirection on the server instance.  The pseudo-PID 0 tells
        # wait_ready to skip the process-tree check.  We still snapshot the
        # remote log on failure so the orchestrator output is useful.
        local granian_log_env_remote=""
        [ "$stack" = "granian" ] && granian_log_env_remote="GRANIAN_LOG_TARGET=$BENCH_REMOTE_REPO/$SCRATCH/server_granian.log"
        # Sprint 21 Phase B: propagate BB_BENCH_TASKSET so per-stack runs
        # can pin workers to specific CPUs.  Empty (the default) means no
        # pinning on the server side.
        local taskset_env_remote=""
        [ -n "${BB_BENCH_TASKSET:-}" ] && taskset_env_remote="BB_BENCH_TASKSET=$BB_BENCH_TASKSET"
        $BENCH_REMOTE_SSH "cd $BENCH_REMOTE_REPO && mkdir -p $SCRATCH && \
            $granian_log_env_remote $taskset_env_remote BIND_HOST=$BENCH_BIND_HOST BASE_PORT=$BASE_PORT \
            bash bench/peers/server_lifecycle_remote.sh start \
            '$stack' '$BASE_PORT' '$CERT' '$KEY' '$SCRATCH/server_${stack}.log'" \
            || {
                echo "  remote launcher failed for $stack" >&2
                {
                    echo ""
                    echo "**$stack failed to start (remote launcher non-zero exit).**"
                    echo ""
                } >> "$OUT"
                return 0
            }
    else
        # Granian gets a direct FileHandler log (avoids the shell-pipe
        # buffering question altogether); other stacks use the shell pipe.
        local granian_log_env=""
        [ "$stack" = "granian" ] && granian_log_env="GRANIAN_LOG_TARGET=$(pwd)/$SCRATCH/server_granian.log"
        # Sprint 21 Phase B: optional CPU pinning, same env var as remote mode.
        local taskset_prefix=()
        [ -n "${BB_BENCH_TASKSET:-}" ] && taskset_prefix=(taskset -c "$BB_BENCH_TASKSET")
        env $granian_log_env \
            "${taskset_prefix[@]}" \
            bash bench/peers/run_peer.sh "$stack" "$BASE_PORT" "$CERT" "$KEY" \
            > "$SCRATCH/server_${stack}.log" 2>&1 &
        server_pid=$!
        disown
    fi

    if ! wait_ready "$server_pid"; then
        echo "  failed to start (or orphan answering on port); last 20 log lines:" >&2
        if [ "$BENCH_REMOTE_LIFECYCLE" = "1" ]; then
            $BENCH_REMOTE_SSH "tail -20 $BENCH_REMOTE_REPO/$SCRATCH/server_${stack}.log" >&2 2>/dev/null || true
        else
            tail -20 "$SCRATCH/server_${stack}.log" >&2
            kill "$server_pid" 2>/dev/null || true
        fi
        {
            echo ""
            echo "**$stack failed to start** — see \`scratch_${TS}/server_${stack}.log\`."
            echo ""
        } >> "$OUT"
        return 0
    fi
    echo "$stack ready (spawn pid=$server_pid)."

    # Sprint 24: short warmup burst to settle Python allocator, kernel
    # TCP autotune, and TLS session-cache before any measured run.
    # Output is discarded — this exists to remove transients, not to
    # produce a number.  WARMUP=0 disables (back-compat with the
    # Sprint 13–23 protocol).
    if [ "${WARMUP:-15}" -gt 0 ]; then
        echo "  warmup ${WARMUP}s ..."
        wrk -t2 -c64 -d"${WARMUP}s" "$BASE/plaintext" >/dev/null 2>&1 || true
    fi

    # Between-lane health check: if the server has died mid-run (hypercorn
    # has been observed to crash silently on large multiplexed responses),
    # mark the rest of the lanes skipped and move on to the next stack.
    health_check() {
        curl -sk --max-time 2 "$BASE/plaintext" 2>/dev/null | grep -q "Hello" \
            || { echo "  server died mid-run; skipping remaining lanes for $stack." >&2
                 echo "" >> "$OUT"
                 echo "**Server crashed mid-run; remaining lanes skipped.**" >> "$OUT"
                 return 1; }
        return 0
    }

    # Capability checks against SUPPORTS_H2 / NO_POST_NO_WS use the
    # worker-suffix-stripped base name so blackbull-w<N> inherits from
    # blackbull.  Sprint 14 suffixes (-cleartext / -nginx / -h11) are NOT
    # stripped — their capability semantics differ from the base by design.
    local stack_base
    stack_base="$(strip_worker_suffix "$stack")"

    if contains_word "A" $LANES && contains_word "$stack_base" $SUPPORTS_H2; then
        echo "  Lane A (h2load HTTP/2) ..."
        run_lane_a_h2load "$stack"
        health_check || { kill_existing; return 0; }
    fi
    if contains_word "B-wrk" $LANES; then
        echo "  Lane B-wrk ..."
        run_lane_b_wrk "$stack"
        health_check || { kill_existing; return 0; }
    fi
    if contains_word "B-oha" $LANES; then
        echo "  Lane B-oha ..."
        run_lane_b_oha "$stack"
        health_check || { kill_existing; return 0; }
    fi
    if contains_word "E-wrk" $LANES; then
        echo "  Lane E-wrk (connection churn) ..."
        run_lane_e_wrk "$stack"
        health_check || { kill_existing; return 0; }
    fi
    if contains_word "C" $LANES; then
        echo "  Lane C (k6 stress) ..."
        run_lane_c_k6 "$stack"
        health_check || { kill_existing; return 0; }
    fi
    if contains_word "D" $LANES && ! contains_word "$stack_base" $NO_POST_NO_WS; then
        echo "  Lane D (WebSocket) ..."
        run_lane_d_ws "$stack"
    elif contains_word "D" $LANES; then
        echo "  Lane D skipped (no WebSocket on $stack)."
        echo "" >> "$OUT"
        echo "### $stack — Lane D (WebSocket RTT, k6)" >> "$OUT"
        echo "" >> "$OUT"
        echo "_Skipped — $stack is a static-only reference (no WebSocket terminator)._" >> "$OUT"
    fi

    kill_existing
}

# ----------------------------------------------------------------------------
# Report preamble
# ----------------------------------------------------------------------------

{
    echo "# Server comparison — $TS"
    echo ""
    echo "Methodology: bench/CHARACTERIZATION.md"
    echo "App:         bench/peers/asgi_app.py (shared minimal ASGI)"
    echo "             — BlackBull uses bench/app.py for parity at the wire level"
    echo "Target:      $BASE  (default; Sprint 14 *-cleartext stacks use http:// instead)"
    echo ""
    echo "Hardware:    $(uname -a | cut -d' ' -f1-3)"
    echo "CPU:         $(grep -m1 'model name' /proc/cpuinfo | sed 's/.*: //')"
    echo "Python:      $(python3 --version)"
    echo "wrk:         $(wrk --version 2>&1 | head -1 || echo 'not installed')"
    echo "h2load:      $(h2load --version 2>&1 | head -1)"
    echo "oha:         $(oha --version 2>/dev/null || echo 'not installed')"
    echo "k6:          $(k6 version 2>&1 | head -1)"
    # Use importlib.metadata so packages without __version__ (hypercorn) still report.
    pkg_version() {
        python3 -c "from importlib.metadata import version, PackageNotFoundError
try: print(version('$1'))
except PackageNotFoundError: print('not installed')" 2>/dev/null || echo 'not installed'
    }
    echo "uvicorn:     $(pkg_version uvicorn)"
    echo "hypercorn:   $(pkg_version hypercorn)"
    echo "granian:     $(pkg_version granian)"
    echo "daphne:      $(pkg_version daphne)"
    echo "nginx:       $(nginx -v 2>&1 | head -1 | sed 's/^nginx version: //' || echo 'not installed')"
    echo ""
    echo "Stacks:      $STACKS"
    echo "Lanes:       $LANES"
    echo "Duration:    $DURATION s per HTTP/1.1 scenario"
    echo "Warmup:      $WARMUP s per stack (one /plaintext burst, output discarded)"
    echo ""
} > "$OUT"

# ----------------------------------------------------------------------------
# Run
# ----------------------------------------------------------------------------

for stack in $STACKS; do
    bench_stack "$stack"
done

# Prepend side-by-side summary table (idempotent).
python3 bench/peers/summarize.py "$OUT" 2>&1 || \
    echo "WARNING: summarize.py failed; report has no summary section" >&2

echo ""
echo "=========================================="
echo "Report: $OUT"
echo "Scratch: $SCRATCH/"
echo "=========================================="
