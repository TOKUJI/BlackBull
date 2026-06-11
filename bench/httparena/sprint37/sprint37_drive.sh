#!/usr/bin/env bash
# bench/httparena/sprint37/sprint37_drive.sh — local orchestrator.
#
# Per the `feedback_remote_scripts_via_upload` memory: local files
# uploaded, then exec'd with args.  No SSH heredocs.
#
# Assumes:
#   - A clean c7i.8xlarge has already been provisioned via
#     `INSTANCE_TYPE=c7i.8xlarge TOPO=single bash bench/aws/up.sh`.
#   - The HttpArena clone exists locally at ~/work/HttpArena/.
#   - The BlackBull framework is vendored at
#     ~/work/HttpArena/frameworks/blackbull/.
#   - ~/work/HttpArena/frameworks/blackbull/meta.json has
#     enabled:true on the vendored copy (do NOT commit this to the
#     BlackBull repo — only flip on the vendored side for the run).
#
# Args: <run_id>
# e.g.  bash sprint37_drive.sh run1
#       bash sprint37_drive.sh run2   # second clean instance, after re-up

set -euo pipefail

RUN_ID="${1:?run_id required, eg run1 / run2}"

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

if [ ! -d "$HARENA_LOCAL" ]; then
    echo "FAIL: $HARENA_LOCAL not found — clone with 'git clone https://github.com/MDA2AV/HttpArena.git ~/work/HttpArena' first" >&2
    exit 1
fi

if [ ! -d "$HARENA_LOCAL/frameworks/blackbull" ]; then
    echo "FAIL: BlackBull not vendored at $HARENA_LOCAL/frameworks/blackbull" >&2
    exit 1
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
# shellcheck disable=SC2029
ssh "${SSH_OPTS[@]}" "$SERVER" "bash ~/sprint37_remote.sh $RUN_ID"

# ── 3. Pull the result tarball back ─────────────────────────────────────────
LOCAL_OUT="${BB_ROOT}/bench/results/httparena/sprint37-${RUN_ID}-$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$LOCAL_OUT"
echo "=== rsync results to $LOCAL_OUT ==="
rsync -az -e "$SSH_E" \
    "${SERVER}:~/sprint37-results-${RUN_ID}/" \
    "${LOCAL_OUT}/"

echo "=== done: results at $LOCAL_OUT ==="
