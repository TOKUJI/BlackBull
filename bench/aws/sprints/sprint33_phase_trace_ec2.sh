#!/usr/bin/env bash
# bench/aws/sprint33_phase_trace_ec2.sh
#
# Quantify per-request CPU and the between-request gap on EC2 using
# BlackBull's phase-trace access log (BB_PHASE_TRACE=1).  Mirrors the
# local WSL2 probe shape (single-worker, TLS HTTP/1.1, Brotli
# precompressed sibling) so the EC2 numbers compare directly.
#
# Output: per-phase wall + CPU distributions + the inter-request gap
# (dispatch_done(N) → loop_start(N+1)) on a keep-alive connection.
#
# Cost: c7i.2xlarge ~ $0.36/hr × ~15 min = ~$0.10.
#
# Usage:
#   bash bench/aws/sprint33_phase_trace_ec2.sh
#
# Env knobs:
#   DURATION_S       wrk duration (default 15)
#   CONNS            wrk -c (default 64; bumped to 1024 for the saturated run)
#   THREADS          wrk -t (default 8)
#   KEEP_INSTANCE    1 to leave the box up on exit

set -euo pipefail
: "${INSTANCE_TYPE:=c7i.2xlarge}"
export INSTANCE_TYPE

# shellcheck source=config.sh
source "$(dirname "$0")/config.sh"
_bench_aws_check_env
export TOPO=single

DURATION_S="${DURATION_S:-15}"
THREADS="${THREADS:-8}"
KEEP_INSTANCE="${KEEP_INSTANCE:-0}"

TS="$(date -u +%Y%m%d-%H%M%SZ)"
LOCAL_DEST="$REPO_ROOT/bench/results/aws/sprint33-phase-trace-${TS}"
mkdir -p "$LOCAL_DEST"

echo "=== sprint33_phase_trace_ec2.sh ==="
echo "  destination:   $LOCAL_DEST"
echo "  instance type: $INSTANCE_TYPE"

# Build wheel.
echo ">>> building wheel ..."
cd "$REPO_ROOT"
rm -f dist/blackbull-*.whl 2>/dev/null || true
python -m build --wheel >/dev/null
WHEEL_PATH="$(ls -1 "$REPO_ROOT"/dist/blackbull-*-py3-none-any.whl | head -1)"
WHEEL_NAME="$(basename "$WHEEL_PATH")"
echo "    wheel: $WHEEL_NAME"

echo ">>> bench/aws/up.sh ..."
bash "$(dirname "$0")/up.sh"

_teardown() {
    local rc=$?
    if [ "$KEEP_INSTANCE" = "1" ]; then
        echo "KEEP_INSTANCE=1 — leaving EC2 alive"
        return $rc
    fi
    echo ">>> bench/aws/down.sh (trap EXIT) ..."
    bash "$(dirname "$0")/down.sh" || true
    return $rc
}
trap _teardown EXIT

_bench_aws_load_state
REMOTE="$SSH_USER@$SERVER_PUBLIC_IP"
echo "    instance: $SERVER_PUBLIC_IP"

# Install Python + wrk + the wheel + a TLS cert.
echo ">>> installing tooling ..."
ssh "${SSH_OPTS[@]}" "$REMOTE" '
    set -euo pipefail
    sudo apt-get update -qq
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
        python3 python3-pip python3-venv python3-brotli wrk openssl >/dev/null
'

# Upload wheel + app + tests cert/key.
echo ">>> uploading wheel + cert ..."
rsync -e "ssh ${SSH_OPTS[*]}" -az \
    "$WHEEL_PATH" \
    "$REPO_ROOT/tests/cert.pem" \
    "$REPO_ROOT/tests/key.pem" \
    "$REMOTE:~/"

# Build venv with the wheel + uvloop (the latter so a uvloop run is
# easy to add if needed).  Also brotli for dataset generation.
ssh "${SSH_OPTS[@]}" "$REMOTE" "
    set -euo pipefail
    python3 -m venv ~/venv
    source ~/venv/bin/activate
    pip install --upgrade pip wheel >/dev/null
    pip install --quiet ~/$WHEEL_NAME'[compression]' brotli uvloop
    pip list | grep -E 'blackbull|brotli|uvloop' || true
"

# Generate one ~16 KB JS file + Brotli sibling.
ssh "${SSH_OPTS[@]}" "$REMOTE" '
    set -euo pipefail
    source ~/venv/bin/activate
    mkdir -p /tmp/static
    python3 - <<PY
import brotli
content = (b"abcdefghijklmnopqrstuvwxyz0123456789" * 30)[:1024] * 16
open("/tmp/static/app.js", "wb").write(content)
open("/tmp/static/app.js.br", "wb").write(brotli.compress(content))
PY
    ls -la /tmp/static
'

# Build the launcher script remotely.
ssh "${SSH_OPTS[@]}" "$REMOTE" 'cat > ~/probe_app.py' <<'PYEOF'
import os, sys
from http import HTTPMethod
from blackbull import BlackBull, Response
from blackbull.middleware.compression import Compression

app = BlackBull()
app.use(Compression())
app.static('/static', '/tmp/static')

@app.route(path='/ping', methods=[HTTPMethod.GET])
async def ping():
    return Response(b'ok', content_type='text/plain')

if __name__ == '__main__':
    port = int(sys.argv[1])
    cert = sys.argv[2]
    key = sys.argv[3]
    app.run(port=port, certfile=cert, keyfile=key, workers=1)
PYEOF

# wrk lua: TLS + Accept-Encoding: br
ssh "${SSH_OPTS[@]}" "$REMOTE" 'cat > ~/probe.lua' <<'LUAEOF'
wrk.headers["Accept-Encoding"] = "br;q=1.0, gzip;q=0.8"
LUAEOF

# Configure the access log to land on a file (so we can rsync it).
ssh "${SSH_OPTS[@]}" "$REMOTE" 'cat > ~/logging.conf' <<'CFG'
[loggers]
keys=root,access

[handlers]
keys=console,file

[formatters]
keys=plain

[logger_root]
level=WARNING
handlers=console

[logger_access]
level=INFO
handlers=file
qualname=blackbull.access
propagate=0

[handler_console]
class=StreamHandler
level=WARNING
formatter=plain
args=(sys.stderr,)

[handler_file]
class=FileHandler
level=INFO
formatter=plain
args=('/home/ubuntu/access.log',)

[formatter_plain]
format=%(message)s
CFG

# Driver: c=64 (per-worker headroom) and c=1024 (saturated).
for CONNS in 64 1024; do
    echo ">>> probing CONNS=$CONNS ..."
    ssh "${SSH_OPTS[@]}" "$REMOTE" "
        set -e
        cd ~
        source ~/venv/bin/activate
        sudo fuser -k 8443/tcp 2>/dev/null || true
        sleep 2
        rm -f access.log
        BB_ACCESS_LOG=1 BB_PHASE_TRACE=1 \
        python3 -c \"
import logging.config
logging.config.fileConfig('/home/ubuntu/logging.conf', disable_existing_loggers=False)
import runpy
runpy.run_path('/home/ubuntu/probe_app.py', run_name='__main__')
\" 8443 cert.pem key.pem >server.log 2>&1 &
        SPID=\$!
        for i in \$(seq 1 30); do
            curl -k -s -o /dev/null --max-time 1 https://localhost:8443/static/app.js && break
            sleep 1
        done
        # warmup
        wrk -t1 -c8 -d2s -s probe.lua https://localhost:8443/static/app.js >/dev/null 2>&1 || true
        # probe
        wrk -t$THREADS -c$CONNS -d${DURATION_S}s -s probe.lua \
            https://localhost:8443/static/app.js >wrk-c${CONNS}.log 2>&1
        sleep 1
        kill \$SPID 2>/dev/null || true
        sleep 2
        cp access.log access-c${CONNS}.log
        wc -l access-c${CONNS}.log
    "
done

# Pull artefacts back.
echo ">>> pulling artefacts ..."
for pattern in 'wrk-c*.log' 'access-c*.log' 'server.log'; do
    rsync -az -e "ssh ${SSH_OPTS[*]}" "$REMOTE:$pattern" "$LOCAL_DEST/" 2>/dev/null \
        || echo "  (no $pattern matched)"
done

cat > "$LOCAL_DEST/provenance.md" <<EOF
# EC2 phase-trace probe — Sprint 33

- Timestamp: $TS
- Instance:  $INSTANCE_TYPE
- Public IP: $SERVER_PUBLIC_IP
- BlackBull: $WHEEL_NAME (from $(cd "$REPO_ROOT" && git rev-parse --short HEAD) on $(cd "$REPO_ROOT" && git rev-parse --abbrev-ref HEAD))
- Workload: single-worker, TLS HTTP/1.1, Brotli precompressed sibling
- wrk:      t=$THREADS d=${DURATION_S}s; CONNS swept across {64, 1024}
EOF

echo
echo "=== complete ==="
ls -lh "$LOCAL_DEST"
echo
echo "Parse with: python3 /tmp/_parse_phase_log.py $LOCAL_DEST/access-cNNNN.log"
