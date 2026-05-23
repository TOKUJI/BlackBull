#!/usr/bin/env bash
# bench/aws/install.sh — deploy BlackBull + the benchmark toolchain to the
# running EC2 instance, then smoke-test.
#
# Expects bench/aws/.state from up.sh.  Re-runnable: rsync overwrites, apt
# is idempotent, pip --upgrade is fine to repeat.
#
# What it does:
#   1. rsync the working tree from local to ~/BlackBull on the instance
#      (skips .venv, bench/results, __pycache__, dot-state files).
#   2. apt-get install python3-pip python3-venv build-essential …
#   3. Create a venv, pip install -e .[testing,speed,compression,profiling].
#   4. Run bench/install.sh (h2load / wrk / wrk2 / oha / k6 / peer ASGI servers).
#   5. Install tests/cert.pem into the system CA store so h2load/k6 trust the
#      self-signed cert that bench/peers/run_peer.sh uses.
#   6. Smoke-test: pytest -q tests/unit/ --timeout=30.

set -euo pipefail

# shellcheck source=config.sh
source "$(dirname "$0")/config.sh"
_bench_aws_check_env
_bench_aws_load_state

REMOTE="$SSH_USER@$PUBLIC_IP"
REMOTE_REPO="/home/$SSH_USER/BlackBull"

# Capture local HEAD so we can echo it from the remote (we exclude .git/ from
# the rsync, so the remote has no way to look this up itself).
LOCAL_HEAD="$(git -C "$REPO_ROOT" rev-parse HEAD 2>/dev/null || echo unknown)"

echo "Deploying to $REMOTE:$REMOTE_REPO ..."
rsync -az --delete \
    --exclude '.venv/' \
    --exclude '__pycache__/' \
    --exclude '*.pyc' \
    --exclude '.pytest_cache/' \
    --exclude 'bench/results/' \
    --exclude 'bench/aws/.state' \
    --exclude 'bench/aws/.known_hosts' \
    --exclude 'bench/aws/*.pem' \
    --exclude '.git/' \
    -e "ssh ${SSH_OPTS[*]}" \
    "$REPO_ROOT/" "$REMOTE:$REMOTE_REPO/"

echo "Running remote install on $REMOTE ..."
# Heredoc with quoted EOF so $ expansions happen on the remote.
ssh "${SSH_OPTS[@]}" "$REMOTE" "LOCAL_HEAD=$LOCAL_HEAD bash -se" <<'REMOTE_EOF'
set -euo pipefail

cd "$HOME/BlackBull"

echo "=== apt prerequisites ==="
sudo DEBIAN_FRONTEND=noninteractive apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y \
    python3-pip python3-venv build-essential git curl ca-certificates

echo "=== Python venv ==="
if [ ! -d .venv ]; then
    python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
pip install --upgrade pip
pip install -e '.[testing,speed,compression,profiling]'

echo "=== Bench toolchain (h2load, wrk, wrk2, oha, k6, peer ASGI servers) ==="
bash bench/install.sh

echo "=== Install repo TLS cert into system CA store ==="
sudo cp tests/cert.pem /usr/local/share/ca-certificates/blackbull-bench-rootCA.crt
sudo update-ca-certificates >/dev/null

echo "=== Smoke test ==="
.venv/bin/python -m pytest -q tests/unit/ --timeout=30 -x 2>&1 | tail -5

echo "=== Versions ==="
.venv/bin/python --version
.venv/bin/python -c 'import blackbull; print("blackbull module OK")'
echo "blackbull HEAD (from local rsync source): ${LOCAL_HEAD:-unknown}"
REMOTE_EOF

echo
echo "Done.  Next: bash bench/aws/run.sh"
