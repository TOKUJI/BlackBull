#!/usr/bin/env bash
# bench/aws/install.sh — deploy BlackBull + the benchmark toolchain to the
# running EC2 instance(s), then smoke-test.
#
# Expects bench/aws/.state from up.sh.  Re-runnable: rsync overwrites, apt
# is idempotent, pip --upgrade is fine to repeat.
#
# TOPO=single (default):
#   1. rsync repo to the one host
#   2. apt + venv + pip install -e .[testing,speed,compression,profiling]
#   3. bench/install.sh (h2load / wrk / wrk2 / oha / k6 / peer ASGI servers)
#   4. install tests/cert.pem into the system CA store
#   5. smoke test: pytest -q tests/unit/
#
# TOPO=split (Sprint 20):
#   1. same as above on the SERVER, **plus** regenerate tests/cert.pem
#      with bench-server.internal + server private IP as SANs and reinstall
#      in the server's CA store.
#   2. rsync repo to the LOADGEN; install python deps + bench tooling
#      (load tools only); install the regenerated cert in the loadgen's
#      CA store; add /etc/hosts entry mapping bench-server.internal →
#      $SERVER_PRIVATE_IP; deploy a copy of the bench SSH key to
#      ~/.ssh/server.pem on the loadgen (chmod 600) so the loadgen can
#      SSH into the server for remote lifecycle.

set -euo pipefail

# shellcheck source=config.sh
source "$(dirname "$0")/config.sh"
_bench_aws_check_env
_bench_aws_load_state

# Backward-compat with pre-Sprint-20 state files.
SERVER_INSTANCE_ID="${SERVER_INSTANCE_ID:-${INSTANCE_ID:-}}"
SERVER_PUBLIC_IP="${SERVER_PUBLIC_IP:-${PUBLIC_IP:-}}"
SERVER_PRIVATE_IP="${SERVER_PRIVATE_IP:-}"
LOADGEN_INSTANCE_ID="${LOADGEN_INSTANCE_ID:-}"
LOADGEN_PUBLIC_IP="${LOADGEN_PUBLIC_IP:-}"
LOADGEN_PRIVATE_IP="${LOADGEN_PRIVATE_IP:-}"
TOPO="${TOPO:-single}"

if [ -z "$SERVER_PUBLIC_IP" ]; then
    echo "bench/aws: SERVER_PUBLIC_IP missing from .state — re-run up.sh" >&2
    exit 1
fi

SERVER_REMOTE="$SSH_USER@$SERVER_PUBLIC_IP"
REMOTE_REPO="/home/$SSH_USER/BlackBull"
LOCAL_HEAD="$(git -C "$REPO_ROOT" rev-parse HEAD 2>/dev/null || echo unknown)"

# --- Common: deploy + base install on the SERVER --------------------------
echo "Deploying to $SERVER_REMOTE:$REMOTE_REPO ..."
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
    "$REPO_ROOT/" "$SERVER_REMOTE:$REMOTE_REPO/"

# In split topology the server's cert needs SANs that include the
# bench-server.internal DNS name AND the server's VPC private IP so that
# either name lookup path (loadgen's /etc/hosts entry or a direct IP probe)
# verifies cleanly.  REGEN_CERT_SANS is the comma-separated SAN list
# passed to the remote regen step (skipped when empty).
REGEN_CERT_SANS=""
if [ "$TOPO" = "split" ]; then
    if [ -z "$SERVER_PRIVATE_IP" ]; then
        echo "bench/aws: TOPO=split requires SERVER_PRIVATE_IP in .state" >&2
        exit 1
    fi
    REGEN_CERT_SANS="DNS:$SERVER_DNS_NAME,DNS:localhost,IP:127.0.0.1,IP:$SERVER_PRIVATE_IP"
fi

echo "Running remote install on server ..."
ssh "${SSH_OPTS[@]}" "$SERVER_REMOTE" \
    "LOCAL_HEAD=$LOCAL_HEAD REGEN_CERT_SANS=$REGEN_CERT_SANS BIND_BENCH=server bash -se" <<'REMOTE_EOF'
set -euo pipefail

cd "$HOME/BlackBull"

echo "=== apt prerequisites ==="
sudo DEBIAN_FRONTEND=noninteractive apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y \
    python3-pip python3-venv build-essential git curl ca-certificates openssl \
    sysstat

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

if [ -n "${REGEN_CERT_SANS:-}" ]; then
    echo "=== Regenerating tests/cert.pem with SANs: $REGEN_CERT_SANS ==="
    # Replace the repo's checked-in cert+key with a fresh self-signed pair
    # whose subjectAltName matches the split-topology hostnames + private IP.
    # The bench harness is the only consumer of these files on this host;
    # the repo copy can safely diverge from the upstream tests/cert.pem.
    openssl req -x509 -nodes -newkey rsa:2048 -days 30 \
        -keyout tests/key.pem -out tests/cert.pem \
        -subj "/CN=$(echo "$REGEN_CERT_SANS" | sed -n 's/.*DNS:\([^,]*\).*/\1/p')" \
        -addext "subjectAltName=$REGEN_CERT_SANS" \
        -addext "extendedKeyUsage=serverAuth" \
        2>/dev/null
    chmod 644 tests/cert.pem
    chmod 600 tests/key.pem
fi

echo "=== Install repo TLS cert into system CA store ==="
sudo cp tests/cert.pem /usr/local/share/ca-certificates/blackbull-bench-rootCA.crt
sudo update-ca-certificates >/dev/null

echo "=== Loadgen TCP tuning (Lane E support) ==="
# Lane E (Connection: close per request) exhausts the ephemeral source-port
# pool within seconds at any meaningful rate.  Raise the ceiling and let
# TIME_WAIT sockets be re-used immediately so Lane E measures stack
# cost-per-connection-setup rather than the kernel's port-recycle rate.
# In TOPO=single this host plays both roles; tuning runs unconditionally.
sudo tee /etc/sysctl.d/99-blackbull-bench.conf >/dev/null <<'SYSCTL'
# BlackBull benchmark loadgen tuning — installed by bench/aws/install.sh.
# Required for Lane E (Connection: close per request) to measure stack
# cost rather than kernel ephemeral-port-pool ceiling.  See
# bench/CHARACTERIZATION.md "Lane E methodology" for rationale.
net.ipv4.tcp_tw_reuse = 1
net.ipv4.ip_local_port_range = 1024 65535
SYSCTL
sudo sysctl --quiet -p /etc/sysctl.d/99-blackbull-bench.conf

echo "=== Smoke test ==="
.venv/bin/python -m pytest -q tests/unit/ --timeout=30 -x 2>&1 | tail -5

echo "=== Server CPU topology (Sprint 21 Phase B) ==="
mkdir -p bench/results
{
    echo "# Server CPU topology — captured at bench install time."
    echo "# Used to interpret multi-worker scaling: vCPUs that share the"
    echo "# same 'CORE' column are SMT siblings of a single physical core."
    echo
    echo "## lscpu --extended"
    lscpu --extended=CPU,CORE,SOCKET,ONLINE 2>/dev/null || true
    echo
    echo "## /proc/cpuinfo (processor / physical id / core id)"
    grep -E '^processor|^physical id|^core id' /proc/cpuinfo || true
    echo
    echo "## /sys/devices/system/cpu/cpu*/topology/thread_siblings_list"
    for f in /sys/devices/system/cpu/cpu[0-9]*/topology/thread_siblings_list; do
        cpu="${f#/sys/devices/system/cpu/}"; cpu="${cpu%%/*}"
        [ -r "$f" ] && printf '%s  %s\n' "$cpu" "$(cat "$f")"
    done
} > bench/results/server-cpu-topology.txt

echo "=== Versions ==="
.venv/bin/python --version
.venv/bin/python -c 'import blackbull; print("blackbull module OK")'
echo "blackbull HEAD (from local rsync source): ${LOCAL_HEAD:-unknown}"
REMOTE_EOF

# --- TOPO=single ends here ------------------------------------------------
if [ "$TOPO" = "single" ]; then
    echo
    echo "Done.  Next: bash bench/aws/run.sh"
    exit 0
fi

# --- TOPO=split: provision the load generator ----------------------------
if [ -z "$LOADGEN_PUBLIC_IP" ]; then
    echo "bench/aws: TOPO=split but LOADGEN_PUBLIC_IP missing from .state" >&2
    exit 1
fi
LOADGEN_REMOTE="$SSH_USER@$LOADGEN_PUBLIC_IP"

# Pull the regenerated cert + key back from the server so we can hand them
# to the loadgen (whose repo copy must match for the CA store install).
LOCAL_CERT_TMP=$(mktemp -d)
trap 'rm -rf "$LOCAL_CERT_TMP"' EXIT
rsync -az -e "ssh ${SSH_OPTS[*]}" \
    "$SERVER_REMOTE:$REMOTE_REPO/tests/cert.pem" \
    "$SERVER_REMOTE:$REMOTE_REPO/tests/key.pem" \
    "$LOCAL_CERT_TMP/"

echo "Deploying to $LOADGEN_REMOTE:$REMOTE_REPO ..."
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
    "$REPO_ROOT/" "$LOADGEN_REMOTE:$REMOTE_REPO/"

# Overwrite the loadgen's checked-in cert/key with the server-regenerated
# pair before the CA-store install step runs on the loadgen.
rsync -az -e "ssh ${SSH_OPTS[*]}" \
    "$LOCAL_CERT_TMP/cert.pem" "$LOCAL_CERT_TMP/key.pem" \
    "$LOADGEN_REMOTE:$REMOTE_REPO/tests/"

# Copy the bench SSH key to the loadgen so it can SSH into the server
# during the run.  The .pem lives outside the rsynced tree on purpose
# (excluded above), so push it explicitly here.
rsync -az -e "ssh ${SSH_OPTS[*]}" \
    "$LOCAL_KEY" \
    "$LOADGEN_REMOTE:/home/$SSH_USER/.ssh/server.pem"
ssh "${SSH_OPTS[@]}" "$LOADGEN_REMOTE" "chmod 600 /home/$SSH_USER/.ssh/server.pem"

echo "Running remote install on loadgen ..."
ssh "${SSH_OPTS[@]}" "$LOADGEN_REMOTE" \
    "LOCAL_HEAD=$LOCAL_HEAD SERVER_DNS_NAME=$SERVER_DNS_NAME \
     SERVER_PRIVATE_IP=$SERVER_PRIVATE_IP bash -se" <<'REMOTE_EOF'
set -euo pipefail

cd "$HOME/BlackBull"

echo "=== apt prerequisites ==="
sudo DEBIAN_FRONTEND=noninteractive apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y \
    python3-pip python3-venv build-essential git curl ca-certificates openssl \
    sysstat

echo "=== Python venv (loadgen only needs the test/bench libs + curl) ==="
if [ ! -d .venv ]; then
    python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
pip install --upgrade pip
# The loadgen runs k6/wrk/oha/h2load (external tools) plus
# compare_servers.sh's python3 post-processing.  Install the full extras
# anyway so `pytest -q tests/unit/` smoke-tests behave like the server.
pip install -e '.[testing,speed,compression,profiling]'

echo "=== Bench toolchain ==="
bash bench/install.sh

echo "=== /etc/hosts entry for $SERVER_DNS_NAME → $SERVER_PRIVATE_IP ==="
if ! grep -qE "[[:space:]]$SERVER_DNS_NAME(\$|[[:space:]])" /etc/hosts; then
    echo "$SERVER_PRIVATE_IP $SERVER_DNS_NAME" | sudo tee -a /etc/hosts >/dev/null
fi

echo "=== Install repo TLS cert into system CA store ==="
sudo cp tests/cert.pem /usr/local/share/ca-certificates/blackbull-bench-rootCA.crt
sudo update-ca-certificates >/dev/null

echo "=== Loadgen TCP tuning (Lane E support) ==="
# See server-side block for rationale.  This host is the loadgen in
# TOPO=split, so the tuning is what actually matters for Lane E.
sudo tee /etc/sysctl.d/99-blackbull-bench.conf >/dev/null <<'SYSCTL'
# BlackBull benchmark loadgen tuning — installed by bench/aws/install.sh.
# Required for Lane E (Connection: close per request) to measure stack
# cost rather than kernel ephemeral-port-pool ceiling.  See
# bench/CHARACTERIZATION.md "Lane E methodology" for rationale.
net.ipv4.tcp_tw_reuse = 1
net.ipv4.ip_local_port_range = 1024 65535
SYSCTL
sudo sysctl --quiet -p /etc/sysctl.d/99-blackbull-bench.conf

echo "=== Smoke test ==="
.venv/bin/python -m pytest -q tests/unit/ --timeout=30 -x 2>&1 | tail -5

echo "=== Versions ==="
.venv/bin/python --version
.venv/bin/python -c 'import blackbull; print("blackbull module OK")'
echo "blackbull HEAD (from local rsync source): ${LOCAL_HEAD:-unknown}"

echo "=== SSH probe to server ==="
ssh -i "$HOME/.ssh/server.pem" \
    -o StrictHostKeyChecking=accept-new \
    -o UserKnownHostsFile="$HOME/.ssh/server_known_hosts" \
    -o ConnectTimeout=10 \
    "ubuntu@$SERVER_DNS_NAME" 'echo loadgen→server ok'
REMOTE_EOF

echo
echo "Done.  Next: bash bench/aws/run.sh"
