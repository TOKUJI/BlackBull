#!/usr/bin/env bash
# bench/aws/latency_probe_v0290a1.sh — EC2 latency cross-check for
# v0.29.0a1 (Sprint 30 final-release gate).
#
# Methodology per the latency-focused approach:
#   - 30 s warm-up + 60 s measurement window (single 90 s wrk run,
#     window-filtered by timestamp from the access log).
#   - Captures both client-side latency (wrk --latency percentile
#     histogram) AND server-side per-request duration_ms (access log).
#   - Static-file workload — bench/httparena/_local/data/static/ files
#     served via blackbull.static().  Same payload mix the local
#     bench/static_repro probe uses, so local and EC2 numbers are
#     directly comparable.
#
# Two configurations:
#   off — BB_USE_CUSTOM_PROTOCOL=0 (Tier 1 default)
#   on  — BB_USE_CUSTOM_PROTOCOL=1 (Tier 1 + Tier 1.5)
#
# Run them in parallel (different KEY_NAME / SG_NAME to avoid collision):
#   KEY_NAME=blackbull-latency-off SG_NAME=blackbull-latency-sg-off \
#       bash bench/aws/latency_probe_v0290a1.sh off &
#   KEY_NAME=blackbull-latency-on  SG_NAME=blackbull-latency-sg-on  \
#       bash bench/aws/latency_probe_v0290a1.sh on  &
#   wait
#
# Cost: 2 × c7i.2xlarge × ~$0.18 ea = ~$0.36 total.
set -euo pipefail

MODE="${1:-}"
case "$MODE" in
    off) TOGGLE='BB_USE_CUSTOM_PROTOCOL=0' ;;
    on)  TOGGLE='BB_USE_CUSTOM_PROTOCOL=1' ;;
    *)
        echo "Usage: $0 {off|on}" >&2
        echo "  off — Tier 1 default (BB_USE_CUSTOM_PROTOCOL=0)" >&2
        echo "  on  — Tier 1 + 1.5 (BB_USE_CUSTOM_PROTOCOL=1)" >&2
        exit 1
        ;;
esac

BLACKBULL_VERSION="${BLACKBULL_VERSION:-0.29.0a1}"
CONCURRENCY="${CONCURRENCY:-4096}"
WARMUP="${WARMUP:-30}"
MEASURE="${MEASURE:-60}"
TOTAL=$(( WARMUP + MEASURE ))

# Roomy instance.  Set BEFORE sourcing config.sh so its default no-ops.
: "${INSTANCE_TYPE:=c7i.2xlarge}"
export INSTANCE_TYPE

# Unique resource names per mode so two runs can launch in parallel
# without colliding on key-pair / security-group / state files.
: "${KEY_NAME:=blackbull-latency-${MODE}}"
: "${SG_NAME:=blackbull-latency-sg-${MODE}}"
: "${STATE_FILE:=$(pwd)/bench/aws/.state-${MODE}}"
: "${KNOWN_HOSTS_FILE:=$(pwd)/bench/aws/.known_hosts-${MODE}}"
export KEY_NAME SG_NAME STATE_FILE KNOWN_HOSTS_FILE

# shellcheck source=config.sh
source "$(dirname "$0")/config.sh"
_bench_aws_check_env

export TOPO=single

TS="$(date -u +%Y%m%d-%H%M%SZ)"
LOCAL_DEST="$REPO_ROOT/bench/results/latency-tier1.5/${MODE}-${TS}"
mkdir -p "$LOCAL_DEST"

echo "=== bench/aws/latency_probe_v0290a1.sh ($MODE) ==="
echo "  $TOGGLE  c=$CONCURRENCY  warmup=${WARMUP}s  measure=${MEASURE}s"
echo "  KEY_NAME=$KEY_NAME  SG_NAME=$SG_NAME"
echo "  destination:   $LOCAL_DEST"
echo "  blackbull:     $BLACKBULL_VERSION from PyPI"
echo

# Step 1 — provision EC2 + teardown trap.
echo ">>> bench/aws/up.sh ..."
bash "$(dirname "$0")/up.sh"

_teardown() {
    local rc=$?
    echo ">>> bench/aws/down.sh (trap EXIT) ..."
    bash "$(dirname "$0")/down.sh" || true
    return $rc
}
trap _teardown EXIT

_bench_aws_load_state

SERVER_REMOTE="$SSH_USER@$SERVER_PUBLIC_IP"
echo "    instance: $SERVER_PUBLIC_IP"

# Step 2 — install python + wrk on the host (no Docker).
echo ">>> installing python3-venv + wrk ..."
ssh "${SSH_OPTS[@]}" "$SERVER_REMOTE" '
    set -euo pipefail
    sudo DEBIAN_FRONTEND=noninteractive apt-get update -qq
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -yq \
        python3-pip python3-venv wrk >/dev/null
    python3 -m venv ~/venv
    ~/venv/bin/pip install -q --upgrade pip
'

echo ">>> installing blackbull[compression]==${BLACKBULL_VERSION} ..."
ssh "${SSH_OPTS[@]}" "$SERVER_REMOTE" "
    ~/venv/bin/pip install -q --pre 'blackbull[compression]==${BLACKBULL_VERSION}'
    ~/venv/bin/python -c 'import blackbull; print(\"blackbull\", blackbull.__version__)'
"

# Step 3 — copy static dataset + scripts.
echo ">>> rsyncing static dataset ..."
rsync -e "ssh ${SSH_OPTS[*]}" -azq \
    "$REPO_ROOT/bench/httparena/_local/data/static/" \
    "$SERVER_REMOTE:~/static-data/"

echo ">>> dropping launcher.py + rotate.lua ..."
ssh "${SSH_OPTS[@]}" "$SERVER_REMOTE" "cat > ~/launcher.py" <<'PYEOF'
"""Static-file ASGI server with sub-ms access logging.

Mirrors bench/static_repro/access_log_server.py — files are read from
~/static-data, access log goes to ~/access.log with the
``YYYY-MM-DDTHH:MM:SS.mmm ... NNN.mmmms`` format the parser expects.
"""
import logging
import os

# Sub-ms duration formatting in the access log.
from blackbull.server import access_log as _al


def _hires_format(self):
    if self.close_code is not None:
        return (f'{self.client_ip} "{self.method} {self.path} WS/{self.http_version}" '
                f'101 close={self.close_code} {self.duration_ms():.3f}ms')
    return (f'{self.client_ip} "{self.method} {self.path} HTTP/{self.http_version}" '
            f'{self.status} {self.response_bytes} {self.duration_ms():.3f}ms')


_al.AccessLogRecord.format = _hires_format

handler = logging.FileHandler('/home/ubuntu/access.log', mode='w', encoding='utf-8')
handler.setLevel(logging.INFO)
handler.setFormatter(logging.Formatter(
    '%(asctime)s.%(msecs)03d %(message)s',
    datefmt='%Y-%m-%dT%H:%M:%S'))
al = logging.getLogger('blackbull.access')
al.setLevel(logging.INFO)
al.addHandler(handler)
al.propagate = False

os.environ.setdefault('BB_ACCESS_LOG', '1')

from blackbull import BlackBull  # noqa: E402

app = BlackBull()
app.static('/static', '/home/ubuntu/static-data')

if __name__ == '__main__':
    app.run(port=8080)
PYEOF

ssh "${SSH_OPTS[@]}" "$SERVER_REMOTE" "cat > ~/rotate.lua" <<'LUAEOF'
local paths = {
    "/static/app.js", "/static/components.css", "/static/helpers.js",
    "/static/layout.css", "/static/logo.svg",
}
local idx = 0
request = function()
    idx = idx + 1
    return wrk.format("GET", paths[((idx - 1) % #paths) + 1],
        {["Accept-Encoding"] = "br;q=1, gzip;q=0.8"})
end
LUAEOF

# Step 4 — boot blackbull server (mode-specific env), wait for readiness.
echo ">>> booting blackbull with $TOGGLE ..."
# Write a self-contained start.sh on the remote to avoid SSH-side
# multi-line quoting / line-continuation ambiguity that previously
# caused the python launcher never to start.  ``setsid`` detaches the
# process from the SSH session's process group so it survives the SSH
# connection closing.
ssh "${SSH_OPTS[@]}" "$SERVER_REMOTE" "cat > ~/start.sh" <<EOF
#!/usr/bin/env bash
set -x
pkill -f launcher.py 2>/dev/null || true
sleep 1
export $TOGGLE
export BB_ACCESS_LOG=1
export BB_MAX_CONNECTIONS=0
exec /home/ubuntu/venv/bin/python /home/ubuntu/launcher.py
EOF
ssh "${SSH_OPTS[@]}" "$SERVER_REMOTE" 'chmod +x ~/start.sh'
ssh "${SSH_OPTS[@]}" "$SERVER_REMOTE" 'setsid bash ~/start.sh >~/server.log 2>&1 </dev/null & disown 2>/dev/null; sleep 2; exit 0' \
    || echo "    (boot ssh returned non-zero — continuing to readiness probe)"

# Probe readiness from this side so we can fall through to a
# diagnostic dump if it never comes up.
echo ">>> waiting for /static/app.js to respond ..."
SERVER_UP=0
for i in $(seq 1 30); do
    if ssh "${SSH_OPTS[@]}" "$SERVER_REMOTE" 'curl -sf -m 2 http://localhost:8080/static/app.js >/dev/null 2>&1'; then
        SERVER_UP=1
        echo "    server ready (attempt $i)"
        break
    fi
    sleep 1
done

if [ "$SERVER_UP" -ne 1 ]; then
    echo "!!! server failed to come up — dumping diagnostics" >&2
    ssh "${SSH_OPTS[@]}" "$SERVER_REMOTE" '
        echo "--- ps ---"
        ps -ef | grep -E "python|launcher" | grep -v grep
        echo "--- ss -tlnp ---"
        ss -tlnp 2>/dev/null | head -10
        echo "--- ~/server.log ---"
        cat ~/server.log
        echo "--- ~/launcher.py ---"
        head -30 ~/launcher.py
    ' >"$LOCAL_DEST/server-diag.txt" 2>&1 || true
    cat "$LOCAL_DEST/server-diag.txt"
    exit 1   # trap will tear down
fi

# Step 5 — record window markers + run wrk for 90 s.
WARMUP_END=$(ssh "${SSH_OPTS[@]}" "$SERVER_REMOTE" "date -u -d '+${WARMUP} seconds' +'%Y-%m-%dT%H:%M:%S'")
WINDOW_END=$(ssh "${SSH_OPTS[@]}" "$SERVER_REMOTE" "date -u -d '+${TOTAL} seconds' +'%Y-%m-%dT%H:%M:%S'")
echo ">>> warmup_end=$WARMUP_END  window_end=$WINDOW_END"

echo ">>> wrk -t8 -c${CONCURRENCY} -d${TOTAL}s --latency  (detached; polling)"
# Detach wrk from the SSH session — under c=4096 load the server has no
# cycles for SSHd keepalives so an inline SSH session reliably dies with
# exit 255 before wrk completes.  Run wrk in the background, write a
# sentinel ``~/wrk.done`` on completion, poll for that from this side.
ssh "${SSH_OPTS[@]}" "$SERVER_REMOTE" "cat > ~/run_wrk.sh" <<EOF
#!/usr/bin/env bash
rm -f ~/wrk.done
wrk -t8 -c${CONCURRENCY} -d${TOTAL}s -s ~/rotate.lua --latency \\
    -H 'Accept-Encoding: br;q=1, gzip;q=0.8' \\
    http://localhost:8080 > ~/wrk.txt 2>&1
echo \$? > ~/wrk.done
EOF
ssh "${SSH_OPTS[@]}" "$SERVER_REMOTE" 'chmod +x ~/run_wrk.sh'
ssh "${SSH_OPTS[@]}" "$SERVER_REMOTE" 'setsid bash ~/run_wrk.sh >/dev/null 2>&1 </dev/null & disown 2>/dev/null; sleep 1; exit 0' \
    || echo "    (wrk-detach ssh returned non-zero — continuing to poll)"

POLL_DEADLINE=$(( TOTAL + 60 ))   # measurement window + slack
for i in $(seq 1 "$POLL_DEADLINE"); do
    if ssh "${SSH_OPTS[@]}" "$SERVER_REMOTE" 'test -f ~/wrk.done' 2>/dev/null; then
        WRK_RC=$(ssh "${SSH_OPTS[@]}" "$SERVER_REMOTE" 'cat ~/wrk.done' 2>/dev/null || echo "?")
        echo "    wrk finished after ${i}s (rc=$WRK_RC)"
        break
    fi
    sleep 1
done

# Step 6 — stop server, capture remaining log entries.
# Best-effort; SSH may still be flaky for a few seconds after the wrk
# load finishes.  Tolerate failure so we always reach the rsync step.
ssh "${SSH_OPTS[@]}" "$SERVER_REMOTE" \
    'pkill -INT -f launcher.py 2>/dev/null; sleep 2; pkill -9 -f launcher.py 2>/dev/null; true' \
    || echo "    (pkill ssh failed — proceeding)"

# Step 7 — pull results.  Retry up to 3 times because SSH/rsync may
# still fail right after the c=4096 load.  ``--ignore-missing-args``
# lets us at least pull whatever files exist if one is absent.
echo ">>> pulling results to $LOCAL_DEST ..."
RSYNC_OK=0
for i in 1 2 3; do
    if rsync -e "ssh ${SSH_OPTS[*]}" -azq --ignore-missing-args \
        "$SERVER_REMOTE:~/access.log" \
        "$SERVER_REMOTE:~/wrk.txt" \
        "$SERVER_REMOTE:~/server.log" \
        "$LOCAL_DEST/"; then
        echo "    rsync attempt $i: OK"
        RSYNC_OK=1
        break
    fi
    echo "    rsync attempt $i failed, retrying in 5s..."
    sleep 5
done
[ "$RSYNC_OK" -eq 1 ] || echo "!!! rsync failed all 3 attempts — partial or no data" >&2

cat > "$LOCAL_DEST/window.txt" <<EOF
warmup_end=$WARMUP_END
window_end=$WINDOW_END
mode=$MODE
toggle=$TOGGLE
concurrency=$CONCURRENCY
blackbull_version=$BLACKBULL_VERSION
instance_type=$INSTANCE_TYPE
instance_ip=$SERVER_PUBLIC_IP
EOF

echo ">>> done — results in $LOCAL_DEST"
echo
echo "wrk client-side latency summary:"
grep -E "Latency|Requests/sec|50%|75%|90%|99%" "$LOCAL_DEST/wrk.txt" || true
echo
echo "access-log percentiles (measurement window):"
python3 "$REPO_ROOT/bench/static_repro/parse_access_log.py" \
    "$LOCAL_DEST/access.log" "$WARMUP_END" "$WINDOW_END" || true
