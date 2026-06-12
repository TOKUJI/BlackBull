#!/usr/bin/env bash
# bench/httparena/sprint37/sprint37_drive.sh — local orchestrator.
#
# Per the `feedback_remote_scripts_via_upload` memory: local files
# uploaded, then exec'd with args.  No SSH heredocs.
#
# Assumes:
#   - A clean c7i.8xlarge has already been provisioned via
#     `INSTANCE_TYPE=c7i.8xlarge TOPO=single bash bench/aws/up.sh`.
#
# Vendoring is automatic: this script wipes ~/work/HttpArena, clones
# upstream HttpArena fresh, copies bench/httparena/* into the vendored
# frameworks/blackbull/, strips sprint-specific dev files, and flips
# the vendored meta.json's enabled flag to true.  Set SKIP_VENDOR=1 to
# skip that step (use only if you've already hand-prepared
# ~/work/HttpArena and don't want it overwritten).
#
# Args: <run_id> [profiles_csv]
# e.g.  bash sprint37_drive.sh run1
#       bash sprint37_drive.sh run2                       # second clean instance
#       bash sprint37_drive.sh smoke baseline,static      # cheap-tier subset
#       SKIP_VENDOR=1 bash sprint37_drive.sh resume       # reuse current vendor tree

set -euo pipefail

RUN_ID="${1:?run_id required, eg run1 / run2}"
# Optional 2nd arg: comma-separated profile subset to run instead of
# the full list from meta.json["tests"].  Useful for cheap-tier
# validation runs on c7i.xlarge before committing to the full
# c7i.8xlarge sweep.  Example: bash sprint37_drive.sh test baseline,static
PROFILES_OVERRIDE="${2:-}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BB_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
HARENA_LOCAL="${HOME}/work/HttpArena"

# Source the bench/aws/ config.sh — sets SSH_USER, SSH_OPTS array,
# LOCAL_KEY, STATE_FILE.  Then load .state for SERVER_PUBLIC_IP.
# shellcheck source=../../aws/config.sh
source "${BB_ROOT}/bench/aws/config.sh"
_bench_aws_load_state

SERVER_PUBLIC_IP="${SERVER_PUBLIC_IP:?SERVER_PUBLIC_IP not in $STATE_FILE — has up.sh been run?}"
SERVER="${SSH_USER}@${SERVER_PUBLIC_IP}"

# rsync's -e needs the SSH command as a single string; flatten SSH_OPTS.
SSH_E="ssh ${SSH_OPTS[*]}"

# ── 0. Vendor HttpArena + BlackBull into ~/work/HttpArena ───────────────────
# Why automatic: the manual "rm -rf + git clone + cp -r + clean + flip"
# dance was the single most common failure mode between runs (forgotten
# step → script bails after up.sh has already booted the c7i.8xlarge).
# Doing it here means provisioning + vendoring + measurement are one
# unbroken command sequence.
#
# Also wipes-and-reclones every time so we always pick up upstream
# patches (methodology fixes, the wrk avg=p99 fix from issue #858 if
# it merges) without any thinking on the caller's side.
SKIP_VENDOR="${SKIP_VENDOR:-0}"
VENDOR_DST="$HARENA_LOCAL/frameworks/blackbull"

if [ "$SKIP_VENDOR" = "1" ]; then
    echo "=== SKIP_VENDOR=1: re-using existing vendor tree at $VENDOR_DST ==="
    if [ ! -d "$VENDOR_DST" ]; then
        echo "FAIL: SKIP_VENDOR=1 but $VENDOR_DST not found — drop the flag to auto-vendor" >&2
        exit 1
    fi
else
    echo "=== vendor: wipe + re-clone HttpArena, copy BlackBull on top ==="
    rm -rf "$HARENA_LOCAL"
    git clone --depth=1 https://github.com/MDA2AV/HttpArena.git "$HARENA_LOCAL"
    mkdir -p "$VENDOR_DST"
    cp -r "${BB_ROOT}/bench/httparena/." "$VENDOR_DST/"
    # Strip sprint-specific dev files — HttpArena's validate.sh /
    # benchmark.sh don't need them, and they don't ship upstream.
    ( cd "$VENDOR_DST" && \
      rm -rf __pycache__ _local *.whl USAGE.md Dockerfile.dev monitor.sh \
             postprocess.sh runner.sh patch_httparena.py sprint* logging_access.ini )
    # Flip the VENDORED meta.json's enabled flag to true.  The repo-side
    # meta.json stays enabled:false until the upstream PR merges.
    if [ -f "$VENDOR_DST/meta.json" ]; then
        sed -i 's/"enabled": false/"enabled": true/' "$VENDOR_DST/meta.json"
    fi
    echo "=== vendor done: $(grep blackbull "$VENDOR_DST/requirements.txt" 2>/dev/null || echo 'requirements.txt missing') ==="
fi

# ── 1. rsync HttpArena (incl. vendored BlackBull) to the instance ───────────
echo "=== rsync HttpArena to ${SERVER}:~/HttpArena/ ==="
# Strip .git to keep the upload tight; the remote side doesn't need
# history.  Pull the framework's local .dockerignore so the build
# context matches the local one.
rsync -az --delete \
    --exclude='.git/' \
    --exclude='results/' \
    -e "$SSH_E" \
    "${HARENA_LOCAL}/" \
    "${SERVER}:~/HttpArena/"

# ── 2a. Install Docker (separate SSH session so docker-group membership
#       takes effect for the run session below) ────────────────────────────
echo "=== upload + run sprint37_install_docker.sh ==="
scp "${SSH_OPTS[@]}" \
    "${SCRIPT_DIR}/sprint37_install_docker.sh" \
    "${SERVER}:~/sprint37_install_docker.sh"
# shellcheck disable=SC2029
ssh "${SSH_OPTS[@]}" "$SERVER" "bash ~/sprint37_install_docker.sh"

# ── 2b. Upload + exec the remote benchmark script in a FRESH SSH session
#       so the user's group membership now includes docker ────────────────
echo "=== upload sprint37_remote.sh ==="
scp "${SSH_OPTS[@]}" \
    "${SCRIPT_DIR}/sprint37_remote.sh" \
    "${SERVER}:~/sprint37_remote.sh"

echo "=== exec remote script (run id: $RUN_ID) ==="

# Pre-allocate the local output dir and register the rsync-back as an
# EXIT trap so the results come back even when the remote script exits
# non-zero (e.g. sanity check tripped, network blip, OOM).  Losing the
# logs to a `set -e` bail after spending c7i.8xlarge minutes is too
# expensive a failure mode.
LOCAL_OUT="${BB_ROOT}/bench/results/httparena/sprint37-${RUN_ID}-$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$LOCAL_OUT"

pull_results() {
    local rc=$?
    echo "=== rsync results to $LOCAL_OUT (remote exit code: $rc) ==="
    # `|| true` because at this point we want the orchestrator's overall
    # exit code to reflect the remote outcome — the rsync is best-effort
    # cleanup, not a gate.
    rsync -az -e "$SSH_E" \
        "${SERVER}:~/sprint37-results-${RUN_ID}/" \
        "${LOCAL_OUT}/" || echo "WARN: rsync-back failed (network? remote dir missing?)"
    if [ "$rc" -ne 0 ]; then
        echo "WARN: remote script exited $rc — check ${LOCAL_OUT}/remote.log"
    else
        echo "=== done: results at $LOCAL_OUT ==="
    fi
}
trap pull_results EXIT

# shellcheck disable=SC2029
ssh "${SSH_OPTS[@]}" "$SERVER" "bash ~/sprint37_remote.sh '$RUN_ID' '$PROFILES_OVERRIDE'"
