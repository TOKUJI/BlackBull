#!/usr/bin/env bash
# bench/aws/run.sh — execute the full peer comparison on the remote
# instance(s), then rsync the result directory back to
# bench/results/aws/<ts>/.
#
# Drop-safe lifecycle (ab.sh discipline): compare_servers.sh is launched
# DETACHED on the instance (nohup, stdin/stdout detached), so an SSH drop
# can never kill the measurement.  This driver then polls for the
# completion marker and pulls the results before the caller tears down.
#
# Modes:
#   bash bench/aws/run.sh            # launch → poll → pull (default)
#   bash bench/aws/run.sh finish     # poll + pull only (recover a run whose
#                                    #   local driver died; the remote
#                                    #   measurement keeps running detached)
#
# Exit codes:
#   0  completed and pulled
#   1  remote runner died early (no report marker) — partials pulled
#   2  poll budget exhausted while the remote is STILL measuring — do NOT
#      tear down; results stay safe on the box (recover with `run.sh finish`)
#   3  `finish` found nothing complete — instance left up
#
# Override RUNS / LANES / STACKS / DURATION via env before invoking:
#   LANES="A B-wrk" bash bench/aws/run.sh
#
# Knobs consumed by bench/peers/compare_servers.sh:
#   RUNS      h2load median-of-N (default 3 — the plan defaults to 5)
#   LANES     subset of {A, B-wrk, B-oha, C, D}
#   STACKS    subset of {blackbull, uvicorn, hypercorn, granian, daphne, nginx}
#   DURATION  per-scenario seconds for wrk/oha (default 30)
#
# Poll / destination knobs:
#   RUN_BASE_DIR  override the local results root (the compare_servers_aws.sh
#                 driver sets this so its driver.log co-locates with results)
#   RUN_POLLS / RUN_POLL_INTERVAL  poll budget (default 720 × 15 s ≈ 180 min,
#                 matching the driver's safety-shutdown window)
#
# Topology (read from bench/aws/.state):
#   TOPO=single → compare_servers.sh runs on the sole instance
#   TOPO=split  → compare_servers.sh runs on the LOADGEN instance with
#                 BENCH_REMOTE_LIFECYCLE=1 so it launches the server on
#                 the SERVER instance via SSH.

set -euo pipefail

# shellcheck source=config.sh
source "$(dirname "$0")/config.sh"
_bench_aws_check_env
_bench_aws_load_state

# Backward-compat shims for legacy state files.
SERVER_PUBLIC_IP="${SERVER_PUBLIC_IP:-${PUBLIC_IP:-}}"
SERVER_PRIVATE_IP="${SERVER_PRIVATE_IP:-}"
LOADGEN_PUBLIC_IP="${LOADGEN_PUBLIC_IP:-}"
TOPO="${TOPO:-single}"

REMOTE_REPO="/home/$SSH_USER/BlackBull"

# Allow callers to override the matrix.  Defaults match CHARACTERIZATION.md.
: "${RUNS:=5}"
: "${LANES:=A B-wrk B-oha C D}"
: "${STACKS:=blackbull uvicorn hypercorn granian daphne}"
: "${DURATION:=30}"
# Loop override for BlackBull's own row (run_peer.sh defaults BB_UVLOOP=1).
# Sprint 100's four-row discipline needs BB_UVLOOP=0 (pure-Python identity)
# explicitly — pass it through like BB_GC_STATS.
: "${BB_UVLOOP:=1}"
# Sprint 100 instrument toggles (empty = disabled on the remote).
: "${BB_GC_STATS:=}"
: "${BB_LOOP_STAMP:=}"
: "${BB_RESP_TIMING:=}"
: "${BB_TIMING_SNAP:=}"
: "${BB_HANDLER_TIMING:=}"
: "${BB_PARSE_TIMING:=}"
: "${BB_DISPATCH_TIMING:=}"
: "${BB_READ_TIMING:=}"
: "${BB_NULL_SEAM:=}"
: "${BB_NO_EVENTS:=}"
: "${CALIBRATE:=1}"
: "${CALIBRATE_RUNS:=3}"

TS="$(date -u +%Y%m%d-%H%M%SZ)"
# RUN_BASE_DIR lets the compare_servers_aws.sh driver co-locate its
# driver.log with the results; standalone default keeps the legacy path.
LOCAL_DEST="${RUN_BASE_DIR:-$REPO_ROOT/bench/results/aws/$TS}"
mkdir -p "$LOCAL_DEST"

# BB_BENCH_TASKSET is optional and threaded through to the
# server's launch path.  Empty means no pinning.
BB_BENCH_TASKSET="${BB_BENCH_TASKSET:-}"

# Modes: (default) launch → poll → pull; `finish` = poll + pull only
# (recover a run whose local driver died — the remote keeps measuring).
MODE="${1:-run}"

# Poll budget.  Default matches the driver's safety-shutdown window
# (720 × 15 s ≈ 180 min).  Override to shrink/expand.
RUN_POLLS="${RUN_POLLS:-720}"
RUN_POLL_INTERVAL="${RUN_POLL_INTERVAL:-15}"

# Names on the instance.  REMOTE_RUNNER_RE is the pgrep pattern with a
# bracketed dot so the poll's own remote bash -c cmdline (which contains
# the literal pattern) never self-matches.
REMOTE_RUNNER="bench/results/run_compare_servers.sh"
REMOTE_RUNNER_RE="bench/results/run_compare_servers[.]sh"
REMOTE_LOG="bench/results/run_compare_servers_remote.log"

# Everything below is captured into run.log as well as the live console.
exec > >(tee -a "$LOCAL_DEST/run.log") 2>&1

echo "Topology: $TOPO  mode=$MODE"
echo "  RUNS=$RUNS  DURATION=${DURATION}s  LANES=$LANES  STACKS=$STACKS"
echo "  poll budget: $((RUN_POLLS * RUN_POLL_INTERVAL))s (RUN_POLLS=$RUN_POLLS)"
echo

case "$TOPO" in
    single)
        REMOTE="$SSH_USER@$SERVER_PUBLIC_IP"
        RESULTS_HOST="$REMOTE"
        runner_env() {
            printf "export RUNS='%s' DURATION='%s' LANES='%s' STACKS='%s'\n" \
                "$RUNS" "$DURATION" "$LANES" "$STACKS"
            printf "export BB_UVLOOP='%s' BB_GC_STATS='%s' BB_LOOP_STAMP='%s' BB_RESP_TIMING='%s' BB_BENCH_TASKSET='%s'\n" \
                "$BB_UVLOOP" "$BB_GC_STATS" "$BB_LOOP_STAMP" "$BB_RESP_TIMING" "$BB_BENCH_TASKSET"
            printf "export BB_TIMING_SNAP='%s' BB_HANDLER_TIMING='%s' BB_PARSE_TIMING='%s'\n" \
                "$BB_TIMING_SNAP" "$BB_HANDLER_TIMING" "$BB_PARSE_TIMING"
            printf "export BB_DISPATCH_TIMING='%s'\n" "$BB_DISPATCH_TIMING"
            printf "export BB_READ_TIMING='%s'\n" "$BB_READ_TIMING"
            printf "export BB_GATE_STAMP='%s'\n" "$BB_GATE_STAMP"
            printf "export BB_NULL_SEAM='%s' BB_NO_EVENTS='%s'\n" \
                "$BB_NULL_SEAM" "$BB_NO_EVENTS"
            printf "export CALIBRATE='%s' CALIBRATE_RUNS='%s'\n" \
                "$CALIBRATE" "$CALIBRATE_RUNS"
        }
        ;;
    split)
        if [ -z "$LOADGEN_PUBLIC_IP" ] || [ -z "$SERVER_PRIVATE_IP" ]; then
            echo "bench/aws: TOPO=split requires LOADGEN_PUBLIC_IP + SERVER_PRIVATE_IP in .state" >&2
            exit 1
        fi
        REMOTE="$SSH_USER@$LOADGEN_PUBLIC_IP"
        RESULTS_HOST="$REMOTE"
        # The loadgen orchestrator drives the server lifecycle over SSH
        # using its copy of the bench key (deployed by install.sh as
        # ~/.ssh/server.pem).  StrictHostKeyChecking=no is acceptable here
        # because the target is a private-VPC IP that the loadgen reaches
        # only through its own intra-SG rule — no MITM risk.
        REMOTE_SSH="ssh -i /home/$SSH_USER/.ssh/server.pem \
            -o StrictHostKeyChecking=no \
            -o UserKnownHostsFile=/home/$SSH_USER/.ssh/server_known_hosts \
            -o ConnectTimeout=10 \
            -o ServerAliveInterval=30 \
            $SSH_USER@$SERVER_PRIVATE_IP"
        runner_env() {
            printf "export RUNS='%s' DURATION='%s' LANES='%s' STACKS='%s'\n" \
                "$RUNS" "$DURATION" "$LANES" "$STACKS"
            printf "export BB_UVLOOP='%s' BB_GC_STATS='%s' BB_LOOP_STAMP='%s' BB_RESP_TIMING='%s' BB_BENCH_TASKSET='%s'\n" \
                "$BB_UVLOOP" "$BB_GC_STATS" "$BB_LOOP_STAMP" "$BB_RESP_TIMING" "$BB_BENCH_TASKSET"
            printf "export BB_TIMING_SNAP='%s' BB_HANDLER_TIMING='%s' BB_PARSE_TIMING='%s'\n" \
                "$BB_TIMING_SNAP" "$BB_HANDLER_TIMING" "$BB_PARSE_TIMING"
            printf "export BB_DISPATCH_TIMING='%s'\n" "$BB_DISPATCH_TIMING"
            printf "export BB_READ_TIMING='%s'\n" "$BB_READ_TIMING"
            printf "export BB_GATE_STAMP='%s'\n" "$BB_GATE_STAMP"
            printf "export BB_NULL_SEAM='%s' BB_NO_EVENTS='%s'\n" \
                "$BB_NULL_SEAM" "$BB_NO_EVENTS"
            printf "export CALIBRATE='%s' CALIBRATE_RUNS='%s'\n" \
                "$CALIBRATE" "$CALIBRATE_RUNS"
            printf "export BENCH_TARGET_HOST='%s' BENCH_REMOTE_LIFECYCLE=1 BENCH_REMOTE_REPO='%s' BENCH_BIND_HOST=0.0.0.0\n" \
                "$SERVER_DNS_NAME" "$REMOTE_REPO"
            printf 'export BENCH_REMOTE_SSH=%q\n' "$REMOTE_SSH"
        }
        ;;
    *)
        echo "bench/aws: unknown TOPO='$TOPO' (single|split)" >&2
        exit 1
        ;;
esac

# --- launch (run mode only) -----------------------------------------------
if [ "$MODE" = "run" ]; then
    # Build the runner locally (avoids nested-quote hell through ssh),
    # scp it up, then launch it fully detached so the ssh returns.
    RUNNER="$(mktemp)"
    trap 'rm -f "$RUNNER"' EXIT
    {
        echo '#!/usr/bin/env bash'
        echo 'set -uo pipefail'
        printf 'cd %q\n' "$REMOTE_REPO"
        echo 'source .venv/bin/activate'
        runner_env
        echo 'bash bench/peers/compare_servers.sh'
    } > "$RUNNER"

    scp "${SSH_OPTS[@]}" "$RUNNER" "$REMOTE:$REMOTE_REPO/$REMOTE_RUNNER" >/dev/null 2>&1
    ssh "${SSH_OPTS[@]}" "$REMOTE" \
        "cd $REMOTE_REPO && chmod +x $REMOTE_RUNNER && \
         rm -f $REMOTE_LOG && \
         nohup bash $REMOTE_RUNNER </dev/null >$REMOTE_LOG 2>&1 & echo launched"
    echo "compare_servers.sh launched DETACHED on $REMOTE"
    echo "  remote runner: $REMOTE_REPO/$REMOTE_RUNNER"
    echo "  remote log:    $REMOTE_REPO/$REMOTE_LOG"
    echo
fi

# --- poll for the completion marker ---------------------------------------
# Complete when the runner process is gone AND the remote log carries the
# final "Report: bench/results/compare_servers_<ts>.md" line.  (compare_servers.sh
# writes its report progressively, so an existing .md alone is NOT enough;
# the trailing Report: echo only happens after summarize.py.)
complete=0
for i in $(seq 1 "$RUN_POLLS"); do
    state=$(ssh "${SSH_OPTS[@]}" "$REMOTE" \
        "cd $REMOTE_REPO && \
         n=\$(pgrep -f '$REMOTE_RUNNER_RE' | wc -l); \
         m=\$(grep -c '^Report: bench/results/compare_servers_' '$REMOTE_LOG' 2>/dev/null || true); \
         echo \"\$n \$m\"" 2>/dev/null || true)
    n="${state%% *}"; m="${state##* }"
    n="${n:-1}"; m="${m:-0}"
    if [ "$n" = "0" ] && [ "$m" -ge 1 ]; then
        complete=1
        echo "  complete after $((i * RUN_POLL_INTERVAL))s (runner gone, report marker found)."
        break
    fi
    [ $((i % 20)) -eq 0 ] && \
        echo "  poll $i/$RUN_POLLS (n=$n m=$m) $(date -u +%H:%M:%SZ)"
    sleep "$RUN_POLL_INTERVAL"
done

pull_results() {
    echo
    echo "Fetching results ..."
    # rsync the entire bench/results/ tree (small — markdown + json + txt).
    # Kept under aws/<ts>/results/ so they're never confused with WSL runs.
    if rsync -az -e "ssh ${SSH_OPTS[*]}" \
        "$RESULTS_HOST:$REMOTE_REPO/bench/results/" \
        "$LOCAL_DEST/results/"; then
        echo "  pulled to $LOCAL_DEST/results/"
        return 0
    fi
    echo "  WARNING: rsync failed; results remain on the instance." >&2
    return 1
}

if [ "$complete" != "1" ]; then
    echo
    echo "!!! Poll budget exhausted (RUN_POLLS=$RUN_POLLS, $((RUN_POLLS * RUN_POLL_INTERVAL))s)."
    if [ "$MODE" = "finish" ]; then
        echo "    finish: nothing complete — leaving instance up; retry later."
        exit 3
    fi
    still=$(ssh "${SSH_OPTS[@]}" "$REMOTE" \
        "pgrep -f '$REMOTE_RUNNER_RE' | wc -l" 2>/dev/null || true)
    pull_results || true
    if [ "${still:-0}" != "0" ]; then
        echo "    Remote measurement is STILL RUNNING — do NOT tear down."
        echo "    Partials were pulled; recover the rest with 'bash bench/aws/run.sh finish'."
        exit 2
    fi
    echo "    Remote runner is not running and no report marker — died early."
    exit 1
fi

pull_results || true

echo
echo "Results landed in $LOCAL_DEST"
if compgen -G "$LOCAL_DEST/results/compare_servers_*.md" >/dev/null; then
    summary=$(ls -1 "$LOCAL_DEST/results"/compare_servers_*.md | tail -1)
    bytes=$(stat -c %s "$summary" 2>/dev/null || stat -f %z "$summary")
    echo "  summary: $summary ($bytes bytes)"
else
    echo "  WARNING: no compare_servers_*.md found in result tree." >&2
fi
echo
echo "Done.  Next: bash bench/aws/down.sh"
