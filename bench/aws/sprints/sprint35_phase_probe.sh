#!/usr/bin/env bash
# bench/aws/sprint35_phase_probe.sh — Phase-trace probe for Sprint 35.
#
# Spins up one c7i.8xlarge, builds bb-before and bb-after images from
# locally-built wheels, runs each under BB_PHASE_TRACE=1 BB_ACCESS_LOG=1
# at c=512 for 30s on the static profile, captures the docker logs
# (one [ACCESS] line per request with per-phase wall+CPU microseconds
# appended), and summarises the per-phase split locally.
#
# This is the diagnostic complement to the Cell G A/B — Cell G tells
# us whether the candidate moves r/s; the phase trace tells us where
# the per-request CPU is actually being spent.
#
# Cost: c7i.8xlarge ~$1.45/hr × ~15 min ≈ $0.40.
#
# Required env:
#   BB_WHEEL_BEFORE  absolute path to master-HEAD wheel
#   BB_WHEEL_AFTER   absolute path to candidate wheel
#
# Optional env:
#   WORKERS    BB worker count (default 16)
#   CONNS      wrk conn count (default 512)
#   DURATION_S wrk measurement duration (default 30)

set -euo pipefail

: "${BB_WHEEL_BEFORE:?BB_WHEEL_BEFORE=<path> required}"
: "${BB_WHEEL_AFTER:?BB_WHEEL_AFTER=<path> required}"
[ -f "$BB_WHEEL_BEFORE" ] || { echo "not found: $BB_WHEEL_BEFORE" >&2; exit 1; }
[ -f "$BB_WHEEL_AFTER"  ] || { echo "not found: $BB_WHEEL_AFTER"  >&2; exit 1; }

: "${INSTANCE_TYPE:=c7i.8xlarge}"
export INSTANCE_TYPE

# shellcheck source=config.sh
source "$(dirname "$0")/config.sh"
_bench_aws_check_env
export TOPO=single

WORKERS="${WORKERS:-16}"
CONNS="${CONNS:-512}"
DURATION_S="${DURATION_S:-30}"
KEEP_INSTANCE="${KEEP_INSTANCE:-0}"

WHEEL_BEFORE_NAME="$(basename "$BB_WHEEL_BEFORE")"
WHEEL_AFTER_NAME="$(basename "$BB_WHEEL_AFTER")"

TS="$(date -u +%Y%m%d-%H%M%SZ)"
LOCAL_DEST="$REPO_ROOT/bench/results/aws/sprint35-phase-probe-${TS}"
mkdir -p "$LOCAL_DEST"

echo "=== sprint35_phase_probe.sh ==="
echo "  destination:   $LOCAL_DEST"
echo "  instance type: $INSTANCE_TYPE"
echo "  workers:       $WORKERS  conns: $CONNS  duration: ${DURATION_S}s"
echo

bash "$(dirname "$0")/up.sh"

_teardown() {
    local rc=$?
    [ "$KEEP_INSTANCE" = "1" ] && return "$rc"
    bash "$(dirname "$0")/down.sh" || true
    return "$rc"
}
trap _teardown EXIT

_bench_aws_load_state
REMOTE="$SSH_USER@$SERVER_PUBLIC_IP"
echo "    instance: $SERVER_PUBLIC_IP"

for i in $(seq 1 30); do
    if ssh "${SSH_OPTS[@]}" -o ConnectTimeout=5 "$REMOTE" 'echo ready' >/dev/null 2>&1; then
        break
    fi
    sleep 5
done

echo ">>> installing toolchain ..."
ssh "${SSH_OPTS[@]}" "$REMOTE" '
    set -euo pipefail
    sudo apt-get update -qq
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
        wrk curl docker.io git rsync
    sudo systemctl enable --now docker
'

echo ">>> staging HttpArena dataset ..."
ssh "${SSH_OPTS[@]}" "$REMOTE" '
    set -euo pipefail
    rm -rf ~/HttpArena
    git clone --depth 1 https://github.com/MDA2AV/HttpArena.git ~/HttpArena
    cp ~/HttpArena/data/dataset.json /tmp/dataset.json
    cp ~/HttpArena/requests/static-rotate.lua /tmp/static-rotate.lua
    sudo rm -rf /tmp/static
    cp -r ~/HttpArena/data/static /tmp/static
'

echo ">>> uploading wheels + bench files ..."
ssh "${SSH_OPTS[@]}" "$REMOTE" 'mkdir -p ~/bb-before ~/bb-after'
rsync -e "ssh ${SSH_OPTS[*]}" -az "$BB_WHEEL_BEFORE" "$REMOTE:~/bb-before/${WHEEL_BEFORE_NAME}"
rsync -e "ssh ${SSH_OPTS[*]}" -az "$BB_WHEEL_AFTER"  "$REMOTE:~/bb-after/${WHEEL_AFTER_NAME}"
rsync -e "ssh ${SSH_OPTS[*]}" -az \
    "$REPO_ROOT/bench/httparena/app.py" \
    "$REPO_ROOT/bench/httparena/launcher.py" \
    "$REPO_ROOT/bench/httparena/logging_access.ini" \
    "$REMOTE:~/bb-before/"
rsync -e "ssh ${SSH_OPTS[*]}" -az \
    "$REPO_ROOT/bench/httparena/app.py" \
    "$REPO_ROOT/bench/httparena/launcher.py" \
    "$REPO_ROOT/bench/httparena/logging_access.ini" \
    "$REMOTE:~/bb-after/"
rsync -e "ssh ${SSH_OPTS[*]}" -az \
    "$REPO_ROOT/bench/httparena/sprint35_phase_remote.sh" \
    "$REMOTE:~/"
ssh "${SSH_OPTS[@]}" "$REMOTE" 'chmod +x ~/sprint35_phase_remote.sh'

echo ">>> building bb-before / bb-after images ..."
ssh "${SSH_OPTS[@]}" "$REMOTE" "cat > ~/bb-before/Dockerfile" <<EOF
FROM python:3.13-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \\
    PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
COPY ${WHEEL_BEFORE_NAME} /tmp/${WHEEL_BEFORE_NAME}
RUN pip install --no-cache-dir "/tmp/${WHEEL_BEFORE_NAME}[compression]"
COPY app.py launcher.py logging_access.ini /app/
EXPOSE 8080
CMD ["python", "launcher.py"]
EOF

ssh "${SSH_OPTS[@]}" "$REMOTE" "cat > ~/bb-after/Dockerfile" <<EOF
FROM python:3.13-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \\
    PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
COPY ${WHEEL_AFTER_NAME} /tmp/${WHEEL_AFTER_NAME}
RUN pip install --no-cache-dir "/tmp/${WHEEL_AFTER_NAME}[compression]"
COPY app.py launcher.py logging_access.ini /app/
EXPOSE 8080
CMD ["python", "launcher.py"]
EOF

ssh "${SSH_OPTS[@]}" "$REMOTE" '
    set -euo pipefail
    cd ~/bb-before && sudo docker build -t httparena-bb-before . 2>&1 | tail -3
    cd ~/bb-after  && sudo docker build -t httparena-bb-after  . 2>&1 | tail -3
'

echo ">>> running phase-trace probe ..."
ssh "${SSH_OPTS[@]}" "$REMOTE" \
    "bash ~/sprint35_phase_remote.sh $DURATION_S $CONNS $WORKERS" \
    | tee "$LOCAL_DEST/remote.log"

echo ">>> collecting results ..."
rsync -e "ssh ${SSH_OPTS[*]}" -az \
    "$REMOTE:~/sprint35_phase_results/" \
    "$LOCAL_DEST/results/"

# ---------------------------------------------------------------------------
# Local phase summary.  Parse trailing "[loop_start→parsed=Xw/Yc ...]" from
# each [ACCESS] line, compute per-phase mean / p50 / p99 wall + CPU.
# ---------------------------------------------------------------------------
python3 - "$LOCAL_DEST/results" <<'PYEOF'
import re, sys, statistics
from pathlib import Path

root = Path(sys.argv[1])

# Phase token: ``name1→name2=NNNw/MMMc``.  The arrow is U+2192.
phase_tok_re = re.compile(r'(\w+)>(\w+)=(\d+)w/(\d+)c')

def summarise(name, samples):
    if not samples:
        return f'  {name:>40s}: (no samples)'
    n = len(samples)
    mean = statistics.mean(samples)
    p50  = statistics.median(samples)
    p99  = sorted(samples)[max(0, int(n * 0.99) - 1)] if n > 1 else samples[0]
    return f'  {name:>40s}: n={n:7d}  mean={mean:8.1f}\xb5s  p50={p50:8.1f}\xb5s  p99={p99:8.1f}\xb5s'

for fw_log in sorted(root.glob('*_access.log')):
    fw = fw_log.stem.replace('_access', '')
    text = fw_log.read_text(encoding='utf-8', errors='replace')

    wall: dict[tuple[str, str], list[int]] = {}
    cpu:  dict[tuple[str, str], list[int]] = {}
    total = 0
    for line in text.splitlines():
        if '→' not in line:
            continue
        flat = line.replace('→', '>')
        any_match = False
        for m in phase_tok_re.finditer(flat):
            a, b, w, c = m.group(1), m.group(2), int(m.group(3)), int(m.group(4))
            wall.setdefault((a, b), []).append(w)
            cpu.setdefault((a, b), []).append(c)
            any_match = True
        if any_match:
            total += 1

    print(f'=== {fw} ({total} traced requests) ===')
    # Print in the order keys were first seen, which matches the
    # left-to-right order of phases in the access-log line.
    for k in wall:
        a, b = k
        print(summarise(f'wall {a}->{b}', wall[k]))
        print(summarise(f'cpu  {a}->{b}', cpu[k]))
    print()
PYEOF

echo
echo "=== complete ==="
echo "results: $LOCAL_DEST"
