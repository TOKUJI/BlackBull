#!/usr/bin/env bash
# Sprint 33 — worker scaling sweep on a c7i.8xlarge.
#
# Same TLS HTTP/1.1 + .br precompressed probe as
# sprint33_phase_trace_2w.sh, run on a bigger box so even the 16-worker
# cell has 2 vCPUs per worker.  Sweep order: 16, 8, 4 — biggest first
# so a budget cut-off still leaves the most informative cell.
#
# Cost: c7i.8xlarge ~ $1.36/hr × ~20 min ≈ $0.45.

set -euo pipefail
: "${INSTANCE_TYPE:=c7i.8xlarge}"
export INSTANCE_TYPE

# shellcheck source=config.sh
source "$(dirname "$0")/config.sh"
_bench_aws_check_env
export TOPO=single

DURATION_S="${DURATION_S:-15}"
THREADS="${THREADS:-16}"
KEEP_INSTANCE="${KEEP_INSTANCE:-0}"
WORKER_LIST="${WORKER_LIST:-16 8 4}"

TS="$(date -u +%Y%m%d-%H%M%SZ)"
LOCAL_DEST="$REPO_ROOT/bench/results/aws/sprint33-phase-trace-scaling-${TS}"
mkdir -p "$LOCAL_DEST"

echo "=== sprint33_phase_trace_scaling.sh ==="
echo "  destination:   $LOCAL_DEST"
echo "  instance type: $INSTANCE_TYPE"
echo "  workers sweep: $WORKER_LIST"

echo ">>> building wheel ..."
cd "$REPO_ROOT"
rm -f dist/blackbull-*.whl 2>/dev/null || true
python -m build --wheel >/dev/null
WHEEL_PATH="$(ls -1 "$REPO_ROOT"/dist/blackbull-*-py3-none-any.whl | head -1)"
WHEEL_NAME="$(basename "$WHEEL_PATH")"

echo ">>> bench/aws/up.sh ..."
bash "$(dirname "$0")/up.sh"

_teardown() {
    local rc=$?
    if [ "$KEEP_INSTANCE" = "1" ]; then return $rc; fi
    echo ">>> bench/aws/down.sh (trap EXIT) ..."
    bash "$(dirname "$0")/down.sh" || true
    return $rc
}
trap _teardown EXIT

_bench_aws_load_state
REMOTE="$SSH_USER@$SERVER_PUBLIC_IP"
echo "    instance: $SERVER_PUBLIC_IP"

ssh "${SSH_OPTS[@]}" "$REMOTE" '
    set -euo pipefail
    sudo apt-get update -qq
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
        python3 python3-pip python3-venv python3-brotli wrk openssl >/dev/null
'

rsync -e "ssh ${SSH_OPTS[*]}" -az \
    "$WHEEL_PATH" "$REPO_ROOT/tests/cert.pem" "$REPO_ROOT/tests/key.pem" \
    "$REMOTE:~/"

ssh "${SSH_OPTS[@]}" "$REMOTE" "
    set -euo pipefail
    python3 -m venv ~/venv
    source ~/venv/bin/activate
    pip install --upgrade pip wheel >/dev/null
    pip install --quiet ~/$WHEEL_NAME'[compression]' brotli uvloop
"

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
'

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
    workers = int(sys.argv[4])
    app.run(port=port, certfile=cert, keyfile=key, workers=workers)
PYEOF

ssh "${SSH_OPTS[@]}" "$REMOTE" 'cat > ~/probe.lua' <<'LUAEOF'
wrk.headers["Accept-Encoding"] = "br;q=1.0, gzip;q=0.8"
LUAEOF

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

for WORKERS in $WORKER_LIST; do
    for CONNS in 64 1024; do
        TAG="w${WORKERS}-c${CONNS}"
        echo ">>> probing $TAG ..."
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
\" 8443 cert.pem key.pem $WORKERS >server-${TAG}.log 2>&1 &
            SPID=\$!
            for i in \$(seq 1 30); do
                curl -k -s -o /dev/null --max-time 1 https://localhost:8443/static/app.js && break
                sleep 1
            done
            wrk -t1 -c8 -d2s -s probe.lua https://localhost:8443/static/app.js >/dev/null 2>&1 || true
            wrk -t$THREADS -c$CONNS -d${DURATION_S}s -s probe.lua \
                https://localhost:8443/static/app.js >wrk-${TAG}.log 2>&1
            sleep 1
            sudo fuser -k 8443/tcp 2>/dev/null || true
            sleep 3
            cp access.log access-${TAG}.log
            wc -l access-${TAG}.log
        "
    done
done

echo ">>> pulling artefacts ..."
for pat in 'wrk-w*-c*.log' 'access-w*-c*.log' 'server-w*-c*.log'; do
    rsync -az -e "ssh ${SSH_OPTS[*]}" "$REMOTE:$pat" "$LOCAL_DEST/" 2>/dev/null \
        || echo "  (no $pat matched)"
done

cat > "$LOCAL_DEST/provenance.md" <<EOF
# EC2 worker scaling sweep — Sprint 33

- Timestamp: $TS
- Instance:  $INSTANCE_TYPE (32 vCPUs)
- Public IP: $SERVER_PUBLIC_IP
- BlackBull: $WHEEL_NAME (from $(cd "$REPO_ROOT" && git rev-parse --short HEAD) on $(cd "$REPO_ROOT" && git rev-parse --abbrev-ref HEAD))
- Workload: TLS HTTP/1.1, Brotli precompressed sibling
- wrk:      t=$THREADS d=${DURATION_S}s
- Sweep:    WORKERS={$WORKER_LIST} × CONNS={64, 1024}
EOF

echo
echo "=== complete ==="
ls -lh "$LOCAL_DEST"
