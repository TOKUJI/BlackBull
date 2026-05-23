#!/usr/bin/env bash
# bench/aws/profile.sh — Sprint 15: capture two py-spy flame graphs on EC2,
# one at low concurrency (B1-like, K6_VUS=200) and one at high concurrency
# (C2, K6_VUS=500).  The goal is to find what shifts in the profile under
# 500 VU — the suspected cause of the BlackBull→uvicorn gap widening from
# 1.8× (B1 c=256) to 2.4× (C2 500 VU) observed in Sprint 13/14.
#
# Expects bench/aws/.state from up.sh + a remote box prepared by install.sh
# (which now installs the .[profiling] extra → py-spy).  Re-runnable.
#
# Output lands locally in:
#   bench/results/aws/<TS>Z/profile/
#       profile_stress_<ts>_vu200.svg
#       profile_stress_<ts>_vu500.svg
#       k6_stress_profile_<ts>_vu200.json
#       k6_stress_profile_<ts>_vu500.json
#
# Override the VU pair via env:
#   K6_VUS_LIST="200 500"  bash bench/aws/profile.sh   # default
#   K6_VUS_LIST="500 1000" bash bench/aws/profile.sh

set -euo pipefail

# shellcheck source=config.sh
source "$(dirname "$0")/config.sh"
_bench_aws_check_env
_bench_aws_load_state

REMOTE="$SSH_USER@$PUBLIC_IP"
REMOTE_REPO="/home/$SSH_USER/BlackBull"

: "${K6_VUS_LIST:=200 500}"

TS="$(date -u +%Y%m%d-%H%M%SZ)"
LOCAL_DEST="$REPO_ROOT/bench/results/aws/$TS/profile"
mkdir -p "$LOCAL_DEST"

echo "Profiling on EC2.  Output will land in $LOCAL_DEST."
echo "  VU passes: $K6_VUS_LIST"
echo

# Each profile pass is ~2 min wall (15s warmup + 60s stress + a few seconds
# of py-spy SVG write).  Cool down 10s between passes so py-spy and the
# previous server fully release the port.
for vu in $K6_VUS_LIST; do
    echo "=== Pass: K6_VUS=$vu ==="
    ssh "${SSH_OPTS[@]}" \
        -o ServerAliveInterval=60 \
        "$REMOTE" \
        "cd $REMOTE_REPO && source .venv/bin/activate && \
         K6_VUS=$vu bash bench/profile_under_load.sh" \
        2>&1 | tee "$LOCAL_DEST/run_vu${vu}.log"
    echo
    echo "Cool-down 10s before next pass ..."
    sleep 10
done

echo
echo "Remote profile passes finished.  Fetching artefacts ..."

# Sync the profile_stress_*.svg and k6_stress_profile_*.json that
# bench/profile_under_load.sh wrote into the remote bench/results/.
rsync -az -e "ssh ${SSH_OPTS[*]}" \
    --include='profile_stress_*.svg' \
    --include='k6_stress_profile_*.json' \
    --exclude='*' \
    "$REMOTE:$REMOTE_REPO/bench/results/" \
    "$LOCAL_DEST/"

echo
echo "Profile artefacts:"
ls -lh "$LOCAL_DEST"/*.svg "$LOCAL_DEST"/*.json 2>/dev/null || \
    echo "  WARNING: no SVG / JSON files copied back." >&2

echo
echo "Done.  Open the SVGs in a browser to read the flame graphs."
echo "Next: write findings into bench/CHARACTERIZATION.md, then bash bench/aws/down.sh."
