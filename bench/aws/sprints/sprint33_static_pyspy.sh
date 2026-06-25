#!/usr/bin/env bash
# bench/aws/sprint33_static_pyspy.sh — py-spy under static wrk load.
#
# Goal: find where BlackBull spends per-request CPU during the
# HttpArena static profile, so future static-perf work targets the
# actual hot spot rather than guessing.  Captures one flame graph per
# server configuration:
#
#   1. blackbull (patched)        — local wheel, asyncio loop
#   2. blackbull-uvloop (patched) — local wheel, BB_UVLOOP=1
#   3. uvicorn-fastapi            — for direct comparison
#
# Single-worker mode so we can attach py-spy by PID without iterating
# 32 workers; per-request CPU proportions are unaffected.
#
# Cost: c7i.2xlarge ~ $0.36/hr × ~15 min = ~$0.10.
#
# Usage:
#   bash bench/aws/sprint33_static_pyspy.sh
#
# Env knobs:
#   DURATION_S      wrk run + py-spy capture window (default 20)
#   CONNS           wrk -c (default 256; 8 vCPU box can't drive 1024)
#   THREADS         wrk -t (default 8)
#   KEEP_INSTANCE   1 = leave the box up on exit

set -euo pipefail

: "${INSTANCE_TYPE:=c7i.2xlarge}"
export INSTANCE_TYPE

# shellcheck source=config.sh
source "$(dirname "$0")/config.sh"
_bench_aws_check_env
export TOPO=single

DURATION_S="${DURATION_S:-20}"
CONNS="${CONNS:-256}"
THREADS="${THREADS:-8}"
KEEP_INSTANCE="${KEEP_INSTANCE:-0}"

TS="$(date -u +%Y%m%d-%H%M%SZ)"
LOCAL_DEST="$REPO_ROOT/bench/results/aws/sprint33-static-pyspy-${TS}"
mkdir -p "$LOCAL_DEST"

echo "=== sprint33_static_pyspy.sh ==="
echo "  destination:   $LOCAL_DEST"
echo "  instance type: $INSTANCE_TYPE"
echo "  wrk shape:     t=$THREADS c=$CONNS d=${DURATION_S}s"

# ---------------------------------------------------------------------------
# Step 0 — build wheel.
# ---------------------------------------------------------------------------
echo ">>> building wheel ..."
cd "$REPO_ROOT"
rm -f dist/blackbull-*.whl 2>/dev/null || true
python -m build --wheel >/dev/null
WHEEL_PATH="$(ls -1 "$REPO_ROOT"/dist/blackbull-*-py3-none-any.whl | head -1)"
WHEEL_NAME="$(basename "$WHEEL_PATH")"
echo "    wheel: $WHEEL_NAME"

# ---------------------------------------------------------------------------
# Step 1 — provision EC2.
# ---------------------------------------------------------------------------
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

# ---------------------------------------------------------------------------
# Step 2 — install Python 3.13 + wrk + py-spy + the wheel on the box.
# ---------------------------------------------------------------------------
echo ">>> installing tooling ..."
ssh "${SSH_OPTS[@]}" "$REMOTE" '
    set -euo pipefail
    sudo apt-get update -qq
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
        python3 python3-pip python3-venv \
        wrk lua-socket >/dev/null
'

# Upload wheel + bench files.
echo ">>> uploading wheel + framework files ..."
rsync -e "ssh ${SSH_OPTS[*]}" -az \
    "$WHEEL_PATH" \
    "$REPO_ROOT/bench/httparena/app.py" \
    "$REPO_ROOT/bench/httparena/launcher.py" \
    "$REMOTE:~/"

# Also upload the wrk lua script we'll drive with.
# (Use HttpArena's static-rotate.lua shape; cache locally first.)
if [ ! -f /tmp/static-rotate.lua ]; then
    # Mirror the lua we found in /tmp/httparena/requests/static-rotate.lua
    cat > /tmp/static-rotate.lua <<'LUA'
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
LUA
fi
rsync -e "ssh ${SSH_OPTS[*]}" -az /tmp/static-rotate.lua /tmp/_probe_remote.sh "$REMOTE:~/"

# Generate the same static dataset HttpArena uses; reuse their generator
# if present locally, otherwise create representative files.  The probe
# only needs the 20 paths in the lua to resolve, with size > 1KB so the
# br-sibling path is exercised.
echo ">>> generating static dataset (matches HttpArena's static-rotate set) ..."
ssh "${SSH_OPTS[@]}" "$REMOTE" '
    set -euo pipefail
    rm -rf /tmp/static-data
    mkdir -p /tmp/static-data/static
    cd /tmp/static-data/static
    # Files matching the 20 wrk paths.  Sizes are approximate matches to
    # generate_static.py output (~1-72KB text, plus a few binaries).
    python3 -c "
import os, gzip, brotli
def write(name, content, compress=True):
    with open(name, \"wb\") as f: f.write(content)
    if compress:
        with open(name + \".br\", \"wb\") as f: f.write(brotli.compress(content))
        with open(name + \".gz\", \"wb\") as f: f.write(gzip.compress(content))

# text files — content is shape-representative junk
for name, kb in [
    (\"reset.css\", 8), (\"layout.css\", 25), (\"theme.css\", 18),
    (\"components.css\", 200), (\"utilities.css\", 60),
    (\"analytics.js\", 12), (\"helpers.js\", 22), (\"app.js\", 200),
    (\"vendor.js\", 300), (\"router.js\", 35),
    (\"header.html\", 120), (\"footer.html\", 55),
    (\"icon-sprite.svg\", 70), (\"logo.svg\", 15),
    (\"manifest.json\", 2),
]:
    write(name, (b\"abcdefghijklmnopqrstuvwxyz0123456789\" * 30)[:1024] * kb)
# binaries — no precompressed siblings
for name, kb in [
    (\"regular.woff2\", 18), (\"bold.woff2\", 22),
    (\"hero.webp\", 45), (\"thumb1.webp\", 8), (\"thumb2.webp\", 6),
]:
    write(name, os.urandom(1024) * kb, compress=False)
" 2>/dev/null || sudo apt-get install -y -qq python3-brotli >/dev/null && python3 -c "
import os, gzip, brotli
def write(name, content, compress=True):
    with open(name, \"wb\") as f: f.write(content)
    if compress:
        with open(name + \".br\", \"wb\") as f: f.write(brotli.compress(content))
        with open(name + \".gz\", \"wb\") as f: f.write(gzip.compress(content))
for name, kb in [
    (\"reset.css\", 8), (\"layout.css\", 25), (\"theme.css\", 18),
    (\"components.css\", 200), (\"utilities.css\", 60),
    (\"analytics.js\", 12), (\"helpers.js\", 22), (\"app.js\", 200),
    (\"vendor.js\", 300), (\"router.js\", 35),
    (\"header.html\", 120), (\"footer.html\", 55),
    (\"icon-sprite.svg\", 70), (\"logo.svg\", 15),
    (\"manifest.json\", 2),
]:
    write(name, (b\"abcdefghijklmnopqrstuvwxyz0123456789\" * 30)[:1024] * kb)
for name, kb in [
    (\"regular.woff2\", 18), (\"bold.woff2\", 22),
    (\"hero.webp\", 45), (\"thumb1.webp\", 8), (\"thumb2.webp\", 6),
]:
    write(name, os.urandom(1024) * kb, compress=False)
"
    ls /tmp/static-data/static/ | wc -l
'

# Create a Python venv with BlackBull + uvicorn + fastapi + py-spy.
ssh "${SSH_OPTS[@]}" "$REMOTE" "
    set -euo pipefail
    python3 -m venv ~/venv
    source ~/venv/bin/activate
    pip install --upgrade pip wheel >/dev/null
    pip install --quiet \
        ~/$WHEEL_NAME'[compression]' \
        uvloop fastapi 'uvicorn[standard]' \
        py-spy brotli
    echo 'installed:'
    pip list | grep -E 'blackbull|uvicorn|fastapi|uvloop|py-spy' || true
"

# ---------------------------------------------------------------------------
# Step 3 — define a runner that starts a server, attaches py-spy in
# record mode, drives wrk against it, then kills server.  Capture one
# SVG per configuration.
# ---------------------------------------------------------------------------

run_pyspy() {
    local label="$1"   # display name + filename prefix
    local launch="$2"  # command to launch single-worker server on :8080
    echo ">>> profiling: $label"
    ssh "${SSH_OPTS[@]}" "$REMOTE" \
        "bash ~/_probe_remote.sh '$label' '$launch' '$DURATION_S' '$THREADS' '$CONNS'" \
        || echo "  (profiling step for $label had a non-zero exit; keeping going)"
}

# 1. blackbull patched
run_pyspy "blackbull" "python3 app.py --port 8080 --workers 1"

# 2. blackbull patched + uvloop
run_pyspy "blackbull-uvloop" "BB_UVLOOP=1 python3 app.py --port 8080 --workers 1"

# 3. uvicorn + FastAPI (single worker)
# Need a tiny uvicorn-fastapi app that mounts /static at the same dir.
ssh "${SSH_OPTS[@]}" "$REMOTE" "cat > ~/uvicorn_app.py" <<'PYEOF'
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
import os
app = FastAPI()
app.mount('/static', StaticFiles(directory=os.environ['STATIC_DIR']), name='static')
PYEOF
run_pyspy "uvicorn" "uvicorn uvicorn_app:app --host 0.0.0.0 --port 8080 --loop uvloop --workers 1 --log-level critical"

# ---------------------------------------------------------------------------
# Step 4 — pull artefacts back.
# ---------------------------------------------------------------------------
echo ">>> pulling SVGs + wrk logs back ..."
# Use explicit per-pattern rsync — the filter-based form silently
# dropped everything in earlier attempts; remote-side glob expansion is
# the reliable path here.
for pattern in 'profile-*.svg' 'server-*.log' 'wrk-*.log' 'py-spy-*.log'; do
    rsync -az -e "ssh ${SSH_OPTS[*]}" \
        "$REMOTE:$pattern" "$LOCAL_DEST/" 2>/dev/null \
        || echo "  (no files matched $pattern)"
done

cat > "$LOCAL_DEST/provenance.md" <<EOF
# py-spy static-load probe

- Timestamp: $TS
- Instance:  $INSTANCE_TYPE
- Public IP: $SERVER_PUBLIC_IP
- BlackBull: $WHEEL_NAME (from $(cd "$REPO_ROOT" && git rev-parse --short HEAD) on $(cd "$REPO_ROOT" && git rev-parse --abbrev-ref HEAD))
- wrk:       t=$THREADS c=$CONNS d=${DURATION_S}s, static-rotate (Accept-Encoding: br;q=1, gzip;q=0.8)
- Each config single-worker (--workers 1) so PID attach is unambiguous.
EOF

echo
echo "=== complete ==="
echo "Artefacts at: $LOCAL_DEST"
ls -lh "$LOCAL_DEST"/*.svg "$LOCAL_DEST"/*.log 2>/dev/null
