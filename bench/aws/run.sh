#!/usr/bin/env bash
# bench/aws/run.sh — execute the full peer comparison on the remote
# instance(s), then scp the result directory back to
# bench/results/aws/<ts>/.
#
# This is the longest-running step (~45–60 min for the full matrix).
# It runs synchronously: we keep the SSH session open and stream the
# remote stdout/stderr back to the local terminal so progress is visible.
#
# Override RUNS / LANES / STACKS / DURATION via env before invoking:
#   LANES="A B-wrk" bash bench/aws/run.sh
#
# Knobs consumed by bench/peers/compare_servers.sh:
#   RUNS      h2load median-of-N (default 3 — Sprint 13 plan defaults to 5)
#   LANES     subset of {A, B-wrk, B-oha, C, D}
#   STACKS    subset of {blackbull, uvicorn, hypercorn, granian, daphne, nginx}
#   DURATION  per-scenario seconds for wrk/oha (default 30)
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

# Backward-compat shims for state files written before Sprint 20.
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

TS="$(date -u +%Y%m%d-%H%M%SZ)"
LOCAL_DEST="$REPO_ROOT/bench/results/aws/$TS"
mkdir -p "$LOCAL_DEST"

echo "Topology: $TOPO"
echo "Starting remote benchmark.  Output will be tee'd to $LOCAL_DEST/run.log."
echo "  RUNS=$RUNS  DURATION=${DURATION}s"
echo "  LANES=$LANES"
echo "  STACKS=$STACKS"
echo

case "$TOPO" in
    single)
        REMOTE="$SSH_USER@$SERVER_PUBLIC_IP"
        ssh "${SSH_OPTS[@]}" \
            -o ServerAliveInterval=60 \
            "$REMOTE" \
            "cd $REMOTE_REPO && source .venv/bin/activate && \
             RUNS='$RUNS' DURATION='$DURATION' LANES='$LANES' STACKS='$STACKS' \
             bash bench/peers/compare_servers.sh" \
            2>&1 | tee "$LOCAL_DEST/run.log"
        RESULTS_HOST="$REMOTE"
        ;;
    split)
        if [ -z "$LOADGEN_PUBLIC_IP" ] || [ -z "$SERVER_PRIVATE_IP" ]; then
            echo "bench/aws: TOPO=split requires LOADGEN_PUBLIC_IP + SERVER_PRIVATE_IP in .state" >&2
            exit 1
        fi
        REMOTE="$SSH_USER@$LOADGEN_PUBLIC_IP"
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
        ssh "${SSH_OPTS[@]}" \
            -o ServerAliveInterval=60 \
            "$REMOTE" \
            "cd $REMOTE_REPO && source .venv/bin/activate && \
             RUNS='$RUNS' DURATION='$DURATION' LANES='$LANES' STACKS='$STACKS' \
             BENCH_TARGET_HOST='$SERVER_DNS_NAME' \
             BENCH_REMOTE_LIFECYCLE=1 \
             BENCH_REMOTE_REPO='$REMOTE_REPO' \
             BENCH_BIND_HOST=0.0.0.0 \
             BENCH_REMOTE_SSH=\"$REMOTE_SSH\" \
             bash bench/peers/compare_servers.sh" \
            2>&1 | tee "$LOCAL_DEST/run.log"
        RESULTS_HOST="$REMOTE"
        ;;
esac

echo
echo "Remote benchmark finished.  Fetching results ..."

# scp the entire bench/results/ tree (small — markdown + json + txt).  We
# keep them under aws/<ts>/results/ so they're never confused with WSL runs.
rsync -az -e "ssh ${SSH_OPTS[*]}" \
    "$RESULTS_HOST:$REMOTE_REPO/bench/results/" \
    "$LOCAL_DEST/results/"

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
