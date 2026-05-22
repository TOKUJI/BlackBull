#!/usr/bin/env bash
# bench/aws/run.sh — execute the full five-peer comparison on the remote
# instance, then scp the result directory back to bench/results/aws/<ts>/.
#
# This is the longest-running step (~45–60 min for the full matrix).
# It runs synchronously: we keep the SSH session open and stream the
# remote stdout/stderr back to the local terminal so progress is visible.
#
# Override RUNS / LANES / STACKS / DURATION via env before invoking:
#   LANES="A B-wrk" bash bench/aws/run.sh
#
# Knobs consumed by bench/peers/compare_servers.sh:
#   RUNS      h2load median-of-N (default 3 — we pass 5 per Sprint 13 plan)
#   LANES     subset of {A, B-wrk, B-oha, C, D}
#   STACKS    subset of {blackbull, uvicorn, hypercorn, granian, daphne, nginx}
#   DURATION  per-scenario seconds for wrk/oha (default 30)

set -euo pipefail

# shellcheck source=config.sh
source "$(dirname "$0")/config.sh"
_bench_aws_check_env
_bench_aws_load_state

REMOTE="$SSH_USER@$PUBLIC_IP"
REMOTE_REPO="/home/$SSH_USER/BlackBull"

# Allow callers to override the matrix.  Defaults match CHARACTERIZATION.md.
: "${RUNS:=5}"
: "${LANES:=A B-wrk B-oha C D}"
: "${STACKS:=blackbull uvicorn hypercorn granian daphne}"
: "${DURATION:=30}"

TS="$(date -u +%Y%m%d-%H%M%SZ)"
LOCAL_DEST="$REPO_ROOT/bench/results/aws/$TS"
mkdir -p "$LOCAL_DEST"

echo "Starting remote benchmark.  Output will be tee'd to $LOCAL_DEST/run.log."
echo "  RUNS=$RUNS  DURATION=${DURATION}s"
echo "  LANES=$LANES"
echo "  STACKS=$STACKS"
echo

ssh "${SSH_OPTS[@]}" \
    -o ServerAliveInterval=60 \
    "$REMOTE" \
    "cd $REMOTE_REPO && source .venv/bin/activate && \
     RUNS='$RUNS' DURATION='$DURATION' LANES='$LANES' STACKS='$STACKS' \
     bash bench/peers/compare_servers.sh" \
    2>&1 | tee "$LOCAL_DEST/run.log"

echo
echo "Remote benchmark finished.  Fetching results ..."

# scp the entire bench/results/ tree (small — markdown + json + txt).  We
# keep them under aws/<ts>/results/ so they're never confused with WSL runs.
rsync -az -e "ssh ${SSH_OPTS[*]}" \
    "$REMOTE:$REMOTE_REPO/bench/results/" \
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
