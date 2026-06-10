#!/usr/bin/env bash
# Sprint 33 — replicate HttpArena's static bench with default FD ulimit.
#
# Reproduces the bench's exact shape: 32 workers, cleartext :8080, t=64
# c=1024, 5 s × 3 runs, 20-path HttpArena rotation.  The only thing that
# differs from our hardened probe is that wrk runs with **ulimit -n 1024**
# (the default both inside Docker containers and on stock Ubuntu).
#
# If we reproduce HttpArena's ~5,185 r/s here, the wrk-FD-limit
# hypothesis is confirmed end-to-end — the bench number does not
# measure framework capacity.
#
# Cost: c7i.8xlarge ~ $1.36/hr × ~15 min ≈ $0.35.

set -euo pipefail
: "${INSTANCE_TYPE:=c7i.8xlarge}"
export INSTANCE_TYPE

# shellcheck source=config.sh
source "$(dirname "$0")/config.sh"
_bench_aws_check_env
export TOPO=single

WORKERS="${WORKERS:-32}"
THREADS="${THREADS:-64}"
CONNS="${CONNS:-1024}"
DURATION_S="${DURATION_S:-5}"
RUNS="${RUNS:-3}"
ULIMIT_N="${ULIMIT_N:-1024}"
KEEP_INSTANCE="${KEEP_INSTANCE:-0}"

TS="$(date -u +%Y%m%d-%H%M%SZ)"
LOCAL_DEST="$REPO_ROOT/bench/results/aws/sprint33-httparena-fdlimit-${TS}"
mkdir -p "$LOCAL_DEST"

echo "=== sprint33_httparena_fdlimit.sh ==="
echo "  destination: $LOCAL_DEST"
echo "  workers:     $WORKERS"
echo "  ulimit -n:   $ULIMIT_N  (HttpArena-default-shaped)"
echo "  wrk:         t=$THREADS c=$CONNS d=${DURATION_S}s × $RUNS runs"

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

rsync -e "ssh ${SSH_OPTS[*]}" -az "$WHEEL_PATH" "$REMOTE:~/"

ssh "${SSH_OPTS[@]}" "$REMOTE" "
    set -euo pipefail
    python3 -m venv ~/venv
    source ~/venv/bin/activate
    pip install --upgrade pip wheel >/dev/null
    pip install --quiet ~/$WHEEL_NAME'[compression]' brotli
"

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
for name, kb in [
    ("regular.woff2", 18), ("bold.woff2", 22),
    ("hero.webp", 45), ("thumb1.webp", 8), ("thumb2.webp", 6),
]:
    write(name, os.urandom(1024) * kb, compress=False)
PY
'

ssh "${SSH_OPTS[@]}" "$REMOTE" 'cat > ~/probe_app.py' <<'PYEOF'
import os, sys
from blackbull import BlackBull
from blackbull.middleware.compression import Compression
app = BlackBull()
app.use(Compression())
app.static('/static', '/tmp/static')
if __name__ == '__main__':
    app.run(port=int(sys.argv[1]), workers=int(sys.argv[2]))
PYEOF

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
  return wrk.format("GET", paths[((counter - 1) % 20) + 1],
                   {["Accept-Encoding"] = ae_header})
end
LUAEOF

# Server is launched with raised ulimit (it needs many sockets for the
# accept queue at c=1024).  Only the wrk-side is FD-limited per the
# replication.
echo ">>> starting server (workers=$WORKERS, server-side ulimit -n 65536) ..."
ssh "${SSH_OPTS[@]}" "$REMOTE" "
    sudo fuser -k 8080/tcp 2>/dev/null || true
    sleep 2
    bash -c '
        ulimit -n 65536
        cd ~
        source ~/venv/bin/activate
        BB_ACCESS_LOG=0 python3 probe_app.py 8080 $WORKERS >server.log 2>&1 &
        echo \$!
        sleep 1
    ' > /tmp/spid
    cat /tmp/spid
    SPID=\$(cat /tmp/spid | tail -1)
    for i in \$(seq 1 60); do
        if curl -s -o /dev/null --max-time 1 http://localhost:8080/static/reset.css; then
            echo '  server up after '\$i's'
            break
        fi
        sleep 1
    done
    curl -fsS -o /dev/null --max-time 3 http://localhost:8080/static/reset.css \
        || { echo 'FAIL: server never up'; tail -50 server.log; exit 1; }
"

# Run the FD-limited wrk against the running server.
for RUN in $(seq 1 $RUNS); do
    echo ">>> wrk run $RUN/$RUNS  (ulimit -n $ULIMIT_N) ..."
    ssh "${SSH_OPTS[@]}" "$REMOTE" "
        bash -c '
            ulimit -n $ULIMIT_N
            wrk -t$THREADS -c$CONNS -d${DURATION_S}s -s static-rotate.lua \
                http://localhost:8080
        ' 2>&1 | tee wrk-run${RUN}.log
    " || echo "  (wrk returned non-zero; log captured)"
done

# Stop server, pull logs.
ssh "${SSH_OPTS[@]}" "$REMOTE" "sudo fuser -k 8080/tcp 2>/dev/null || true; sleep 1"
echo ">>> pulling artefacts ..."
for pat in 'wrk-run*.log' 'server.log'; do
    rsync -az -e "ssh ${SSH_OPTS[*]}" "$REMOTE:$pat" "$LOCAL_DEST/" 2>/dev/null \
        || echo "  (no $pat matched)"
done

cat > "$LOCAL_DEST/provenance.md" <<EOF
# HttpArena replication with default FD ulimit — Sprint 33

- Timestamp: $TS
- Instance:  $INSTANCE_TYPE
- Public IP: $SERVER_PUBLIC_IP
- BlackBull: $WHEEL_NAME (from $(cd "$REPO_ROOT" && git rev-parse --short HEAD) on $(cd "$REPO_ROOT" && git rev-parse --abbrev-ref HEAD))
- Workload: cleartext HTTP/1.1, HttpArena 20-path static rotation
- Workers:  $WORKERS
- wrk:      t=$THREADS c=$CONNS d=${DURATION_S}s × $RUNS runs (best-of-N)
- wrk ulimit -n: $ULIMIT_N  (HttpArena-default shape)
- Server ulimit -n: 65536  (so server-side accept-queue isn't capped)

If r/s ≈ 5,185 (HttpArena's reported BB static at this shape), the
wrk-FD-limit hypothesis is confirmed: HttpArena's number is bottlenecked
at the load generator, not the framework.
EOF

echo
echo "=== complete ==="
ls -lh "$LOCAL_DEST"
