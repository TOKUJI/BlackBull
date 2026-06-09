#!/usr/bin/env bash
# Sprint 33 — replicate HttpArena's "static" profile shape on the same
# probe rig so the gap is apples-to-apples.
#
# Differences from sprint33_phase_trace_scaling.sh:
#   - 16 workers on c7i.8xlarge (matches the upper rung of the prior
#     clean-scaling sweep, half the HttpArena 32-worker count).
#   - Cleartext HTTP/1.1 on :8080 (HttpArena's static profile is
#     cleartext, not TLS).
#   - 20-path rotation, sizes matched to HttpArena's generate_static.py
#     output (~17 KB average response after the br-sibling negotiation
#     kicks in for the text/HTML/JSON entries).
#   - wrk t=64 c=1024 (HttpArena's lowest-c rung).
#
# If per-worker rate here is much lower than 5,800 r/s, the response-
# size / wrk-thread shape is the lid, not the framework.
#
# Cost: c7i.8xlarge ~ $1.36/hr × ~10 min ≈ $0.25.

set -euo pipefail
: "${INSTANCE_TYPE:=c7i.8xlarge}"
export INSTANCE_TYPE

# shellcheck source=config.sh
source "$(dirname "$0")/config.sh"
_bench_aws_check_env
export TOPO=single

DURATION_S="${DURATION_S:-15}"
THREADS="${THREADS:-64}"
CONNS="${CONNS:-1024}"
WORKERS="${WORKERS:-16}"
KEEP_INSTANCE="${KEEP_INSTANCE:-0}"

TS="$(date -u +%Y%m%d-%H%M%SZ)"
LOCAL_DEST="$REPO_ROOT/bench/results/aws/sprint33-httparena-shape-${TS}"
mkdir -p "$LOCAL_DEST"

echo "=== sprint33_phase_trace_httparena_shape.sh ==="
echo "  destination:   $LOCAL_DEST"
echo "  instance type: $INSTANCE_TYPE"
echo "  workers:       $WORKERS"
echo "  wrk shape:     t=$THREADS c=$CONNS d=${DURATION_S}s"

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
    "$WHEEL_PATH" \
    "$REMOTE:~/"

ssh "${SSH_OPTS[@]}" "$REMOTE" "
    set -euo pipefail
    python3 -m venv ~/venv
    source ~/venv/bin/activate
    pip install --upgrade pip wheel >/dev/null
    pip install --quiet ~/$WHEEL_NAME'[compression]' brotli uvloop
"

# Generate the 20-path dataset that matches HttpArena's static-rotate.
ssh "${SSH_OPTS[@]}" "$REMOTE" '
    set -euo pipefail
    source ~/venv/bin/activate
    mkdir -p /tmp/static
    python3 - <<PY
import os, gzip, brotli
out = "/tmp/static"
def write(name, content, compress=True):
    open(f"{out}/{name}", "wb").write(content)
    if compress:
        open(f"{out}/{name}.br", "wb").write(brotli.compress(content))
        open(f"{out}/{name}.gz", "wb").write(gzip.compress(content))

# CSS/JS/HTML/SVG/JSON — sizes match HttpArena generate_static.py
for name, kb in [
    ("reset.css", 8), ("layout.css", 25), ("theme.css", 18),
    ("components.css", 200), ("utilities.css", 60),
    ("analytics.js", 12), ("helpers.js", 22), ("app.js", 200),
    ("vendor.js", 300), ("router.js", 35),
    ("header.html", 120), ("footer.html", 55),
    ("icon-sprite.svg", 70), ("logo.svg", 15),
    ("manifest.json", 2),
]:
    write(name, (b"abcdefghijklmnopqrstuvwxyz0123456789" * 30)[:1024] * kb)
# Binary entries — no precompressed siblings
for name, kb in [
    ("regular.woff2", 18), ("bold.woff2", 22),
    ("hero.webp", 45), ("thumb1.webp", 8), ("thumb2.webp", 6),
]:
    write(name, os.urandom(1024) * kb, compress=False)
PY
    ls /tmp/static | head -5
    echo "(total files: $(ls /tmp/static | wc -l))"
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
    workers = int(sys.argv[2])
    app.run(port=port, workers=workers)
PYEOF

# HttpArena's static-rotate.lua, verbatim.
ssh "${SSH_OPTS[@]}" "$REMOTE" 'cat > ~/static-rotate.lua' <<'LUAEOF'
local paths = {
  "/static/reset.css", "/static/layout.css", "/static/theme.css",
  "/static/components.css", "/static/utilities.css",
  "/static/analytics.js", "/static/helpers.js", "/static/app.js",
  "/static/vendor.js", "/static/router.js",
  "/static/header.html", "/static/footer.html",
  "/static/regular.woff2", "/static/bold.woff2",
  "/static/logo.svg", "/static/icon-sprite.svg",
  "/static/hero.webp", "/static/thumb1.webp", "/static/thumb2.webp",
  "/static/manifest.json",
}
local ae_header = "br;q=1, gzip;q=0.8"
local counter = 0
request = function()
  counter = counter + 1
  local path = paths[((counter - 1) % 20) + 1]
  return wrk.format("GET", path, {["Accept-Encoding"] = ae_header})
end
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

TAG="w${WORKERS}-c${CONNS}-t${THREADS}-cleartext"
echo ">>> probing $TAG ..."
ssh "${SSH_OPTS[@]}" "$REMOTE" "
    cd ~
    # wrk -t64 -c1024 needs at least 1088 FDs (1 per conn + 1 per
    # thread + lua/socket overhead); default ulimit is 1024 and made
    # the first run crash with 'Too many open files', cutting wrk off
    # mid-test.  Raise the limit before anything else opens fds.
    ulimit -n 65536
    source ~/venv/bin/activate
    sudo fuser -k 8080/tcp 2>/dev/null || true
    sleep 2
    rm -f access.log
    BB_ACCESS_LOG=1 BB_PHASE_TRACE=1 \
    python3 -c \"
import logging.config
logging.config.fileConfig('/home/ubuntu/logging.conf', disable_existing_loggers=False)
import runpy
runpy.run_path('/home/ubuntu/probe_app.py', run_name='__main__')
\" 8080 $WORKERS >server-${TAG}.log 2>&1 &
    SPID=\$!
    echo '  spawned server, pid='\$SPID
    READY=0
    for i in \$(seq 1 60); do
        if curl -s -o /dev/null --max-time 1 http://localhost:8080/static/reset.css 2>/dev/null; then
            READY=1
            echo '  server up after '\$i's'
            break
        fi
        sleep 1
    done
    if [ \"\$READY\" != \"1\" ]; then
        echo '  FAIL: server never up — tail server log:'
        tail -40 server-${TAG}.log
        sudo fuser -k 8080/tcp 2>/dev/null || true
        exit 0
    fi
    echo '  warmup ...'
    wrk -t1 -c8 -d2s -s static-rotate.lua http://localhost:8080 >/dev/null 2>&1 || true
    echo '  stress ...'
    wrk -t$THREADS -c$CONNS -d${DURATION_S}s -s static-rotate.lua \
        http://localhost:8080 >wrk-${TAG}.log 2>&1 || echo '  wrk returned non-zero'
    sleep 1
    sudo fuser -k 8080/tcp 2>/dev/null || true
    sleep 3
    cp access.log access-${TAG}.log 2>/dev/null || touch access-${TAG}.log
    echo '  access log lines:' \$(wc -l < access-${TAG}.log)
    echo '  done'
" || echo "  (probe ssh returned non-zero; pulling whatever exists)"

echo ">>> pulling artefacts ..."
for pat in "wrk-${TAG}.log" "access-${TAG}.log" "server-${TAG}.log"; do
    rsync -az -e "ssh ${SSH_OPTS[*]}" "$REMOTE:$pat" "$LOCAL_DEST/" 2>/dev/null \
        || echo "  (no $pat)"
done

cat > "$LOCAL_DEST/provenance.md" <<EOF
# EC2 HttpArena-shape probe — Sprint 33

- Timestamp: $TS
- Instance:  $INSTANCE_TYPE
- Public IP: $SERVER_PUBLIC_IP
- BlackBull: $WHEEL_NAME (from $(cd "$REPO_ROOT" && git rev-parse --short HEAD) on $(cd "$REPO_ROOT" && git rev-parse --abbrev-ref HEAD))
- Workload: cleartext HTTP/1.1, 20-path HttpArena static rotation
- Workers:  $WORKERS
- wrk:      t=$THREADS c=$CONNS d=${DURATION_S}s
EOF

echo
echo "=== complete ==="
ls -lh "$LOCAL_DEST"
