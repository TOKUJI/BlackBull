#!/usr/bin/env bash
# bench/aws/sprint31_sendfile_probe.sh — EC2 A/B for Sprint 31's
# ``http.response.pathsend`` / ``loop.sendfile`` static-file path.
#
# Single c7i.2xlarge instance, two back-to-back runs:
#   A. chunked: install ``blackbull==0.29.0`` from PyPI (Sprint 30
#      close — no pathsend in scope, static.py takes the chunked
#      ``asyncio.to_thread`` path).
#   B. sendfile: install from ``git+master`` HEAD (PR #44 merge —
#      pathsend advertised, static.py routes the body through
#      ``loop.sendfile``).
#
# Workload: 16 MiB synthetic file at concurrency=64, 30 s warmup +
# 60 s measurement.  c=64 is enough to keep the server busy without
# making the WRK load generator the bottleneck for a 16 MiB body.
#
# Output per run goes to bench/results/sprint31-sendfile/<mode>-<ts>/:
#   - access.log   (per-request server-side duration_ms)
#   - wrk.txt      (client-side latency histogram, RPS, error counts)
#   - server.log   (launcher stderr)
#   - usage.txt    (peak RSS + user/sys CPU from /usr/bin/time -v)
#   - window.txt   (warmup_end, window_end, mode, blackbull source)
#
# Cost: 1 × c7i.2xlarge × ~6 min × ~$0.36/h ≈ $0.04.
set -euo pipefail

WARMUP="${WARMUP:-30}"
MEASURE="${MEASURE:-60}"
TOTAL=$(( WARMUP + MEASURE ))
CONCURRENCY="${CONCURRENCY:-64}"
TARGET_PATH='/static/data-16M.bin'

: "${INSTANCE_TYPE:=c7i.2xlarge}"
export INSTANCE_TYPE

: "${KEY_NAME:=blackbull-sprint31}"
: "${SG_NAME:=blackbull-sprint31-sg}"
: "${STATE_FILE:=$(pwd)/bench/aws/.state-sprint31}"
: "${KNOWN_HOSTS_FILE:=$(pwd)/bench/aws/.known_hosts-sprint31}"
export KEY_NAME SG_NAME STATE_FILE KNOWN_HOSTS_FILE

# shellcheck source=config.sh
source "$(dirname "$0")/config.sh"
_bench_aws_check_env

export TOPO=single

TS="$(date -u +%Y%m%d-%H%M%SZ)"
RESULTS_BASE="$REPO_ROOT/bench/results/sprint31-sendfile"
mkdir -p "$RESULTS_BASE"

echo "=== sprint31_sendfile_probe.sh ($TS) ==="
echo "  c=$CONCURRENCY  warmup=${WARMUP}s  measure=${MEASURE}s  target=$TARGET_PATH"
echo "  KEY_NAME=$KEY_NAME  SG_NAME=$SG_NAME"
echo

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

# Step 1 — install python + wrk + venv on the host.
echo ">>> installing python3-venv + wrk + git ..."
ssh "${SSH_OPTS[@]}" "$SERVER_REMOTE" '
    set -euo pipefail
    sudo DEBIAN_FRONTEND=noninteractive apt-get update -qq
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -yq \
        python3-pip python3-venv wrk git time >/dev/null
    python3 -m venv ~/venv
    ~/venv/bin/pip install -q --upgrade pip
'

# Step 2 — create the 16 MiB synthetic file + the launcher + wrk script.
echo ">>> staging static data + launcher ..."
ssh "${SSH_OPTS[@]}" "$SERVER_REMOTE" '
    mkdir -p ~/static
    dd if=/dev/urandom of=~/static/data-16M.bin bs=1M count=16 status=none
'

ssh "${SSH_OPTS[@]}" "$SERVER_REMOTE" "cat > ~/launcher.py" <<'PYEOF'
"""Sprint 31 EC2 probe launcher — serves /home/ubuntu/static/ with
sub-ms access logging.  Same shape as bench/static_repro/access_log_server.py.
"""
import logging
import os

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
app.static('/static', '/home/ubuntu/static')

if __name__ == '__main__':
    app.run(port=8080)
PYEOF

# Run-one — generic per-mode driver.  Installs the right blackbull,
# boots launcher, runs wrk, captures everything.
_run_one() {
    local label="$1"
    local install_spec="$2"
    local dest="$RESULTS_BASE/${label}-${TS}"
    mkdir -p "$dest"

    echo
    echo "##### mode=$label  install=$install_spec  dest=$dest #####"

    echo ">>> installing $install_spec ..."
    ssh "${SSH_OPTS[@]}" "$SERVER_REMOTE" "
        ~/venv/bin/pip uninstall -y blackbull >/dev/null 2>&1 || true
        ~/venv/bin/pip install -q '${install_spec}'
        ~/venv/bin/python -c 'import blackbull; print(\"blackbull\", blackbull.__version__)'
    "

    echo ">>> writing start.sh ..."
    ssh "${SSH_OPTS[@]}" "$SERVER_REMOTE" "cat > ~/start.sh" <<EOF
#!/usr/bin/env bash
set -x
pkill -f launcher.py 2>/dev/null || true
sleep 1
export BB_ACCESS_LOG=1
exec /usr/bin/time -v -o /home/ubuntu/usage.txt \\
    /home/ubuntu/venv/bin/python /home/ubuntu/launcher.py
EOF
    ssh "${SSH_OPTS[@]}" "$SERVER_REMOTE" 'chmod +x ~/start.sh'

    echo ">>> booting launcher ..."
    ssh "${SSH_OPTS[@]}" "$SERVER_REMOTE" 'setsid bash ~/start.sh >~/server.log 2>&1 </dev/null & disown 2>/dev/null; sleep 2; exit 0' \
        || echo "    (boot ssh returned non-zero — continuing to probe readiness)"

    echo ">>> waiting for $TARGET_PATH ..."
    local up=0
    for i in $(seq 1 30); do
        if ssh "${SSH_OPTS[@]}" "$SERVER_REMOTE" "curl -sf -m 2 http://localhost:8080$TARGET_PATH >/dev/null 2>&1"; then
            up=1
            echo "    ready (attempt $i)"
            break
        fi
        sleep 1
    done
    if [ "$up" -ne 1 ]; then
        echo "!!! server failed to come up in mode $label — dumping diagnostics" >&2
        ssh "${SSH_OPTS[@]}" "$SERVER_REMOTE" '
            ps -ef | grep -E "python|launcher" | grep -v grep
            ss -tlnp 2>/dev/null | head
            echo --- server.log ---
            cat ~/server.log
        ' >"$dest/server-diag.txt" 2>&1 || true
        cat "$dest/server-diag.txt"
        return 1
    fi

    local warmup_end window_end
    warmup_end=$(ssh "${SSH_OPTS[@]}" "$SERVER_REMOTE" "date -u -d '+${WARMUP} seconds' +'%Y-%m-%dT%H:%M:%S'")
    window_end=$(ssh "${SSH_OPTS[@]}" "$SERVER_REMOTE" "date -u -d '+${TOTAL} seconds' +'%Y-%m-%dT%H:%M:%S'")
    echo ">>> warmup_end=$warmup_end window_end=$window_end"

    # Build wrk script that hits the 16M file via lua so connections
    # keep pulling instead of pipelining smaller files.
    ssh "${SSH_OPTS[@]}" "$SERVER_REMOTE" "cat > ~/rotate.lua" <<LUAEOF
request = function()
    return wrk.format("GET", "$TARGET_PATH")
end
LUAEOF

    echo ">>> wrk -t8 -c${CONCURRENCY} -d${TOTAL}s --latency (detached)"
    ssh "${SSH_OPTS[@]}" "$SERVER_REMOTE" "cat > ~/run_wrk.sh" <<EOF
#!/usr/bin/env bash
rm -f ~/wrk.done
wrk -t8 -c${CONCURRENCY} -d${TOTAL}s -s ~/rotate.lua --latency \\
    http://localhost:8080 > ~/wrk.txt 2>&1
echo \$? > ~/wrk.done
EOF
    ssh "${SSH_OPTS[@]}" "$SERVER_REMOTE" 'chmod +x ~/run_wrk.sh'
    ssh "${SSH_OPTS[@]}" "$SERVER_REMOTE" 'setsid bash ~/run_wrk.sh >/dev/null 2>&1 </dev/null & disown 2>/dev/null; sleep 1; exit 0' \
        || echo "    (wrk-detach ssh returned non-zero — continuing to poll)"

    local poll_deadline=$(( TOTAL + 60 ))
    for i in $(seq 1 "$poll_deadline"); do
        if ssh "${SSH_OPTS[@]}" "$SERVER_REMOTE" 'test -f ~/wrk.done' 2>/dev/null; then
            echo "    wrk done after ${i}s rc=$(ssh ${SSH_OPTS[*]} $SERVER_REMOTE 'cat ~/wrk.done' 2>/dev/null || echo ?)"
            break
        fi
        sleep 1
    done

    # Stop launcher to flush usage.txt (time -v writes on process exit).
    ssh "${SSH_OPTS[@]}" "$SERVER_REMOTE" \
        'pkill -INT -f launcher.py 2>/dev/null; sleep 2; pkill -9 -f launcher.py 2>/dev/null; true' \
        || echo "    (pkill ssh failed — proceeding)"

    echo ">>> pulling results to $dest ..."
    local got=0
    for i in 1 2 3; do
        if rsync -e "ssh ${SSH_OPTS[*]}" -azq --ignore-missing-args \
            "$SERVER_REMOTE:~/access.log" \
            "$SERVER_REMOTE:~/wrk.txt" \
            "$SERVER_REMOTE:~/server.log" \
            "$SERVER_REMOTE:~/usage.txt" \
            "$dest/"; then
            got=1
            break
        fi
        sleep 5
    done
    [ "$got" -eq 1 ] || echo "!!! rsync failed all 3 attempts" >&2

    cat > "$dest/window.txt" <<EOF
mode=$label
install_spec=$install_spec
warmup_end=$warmup_end
window_end=$window_end
concurrency=$CONCURRENCY
target_path=$TARGET_PATH
EOF

    echo
    echo "--- wrk summary ($label) ---"
    grep -E "Latency|Requests/sec|50%|75%|90%|99%" "$dest/wrk.txt" 2>/dev/null || echo "(no wrk.txt)"
    echo
    echo "--- access-log percentiles ($label) ---"
    python3 "$REPO_ROOT/bench/static_repro/parse_access_log.py" \
        "$dest/access.log" "$warmup_end" "$window_end" 2>/dev/null || echo "(parse failed)"
    echo
    echo "--- usage.txt ($label) ---"
    grep -E "Maximum resident|User time|System time|Percent of CPU" "$dest/usage.txt" 2>/dev/null || echo "(no usage.txt)"
}

# A — chunked path (v0.29.0 from PyPI)
_run_one "chunked" "blackbull[compression]==0.29.0"

# B — sendfile path (post-PR-44 from git master) — PEP 508 direct-URL form.
_run_one "sendfile" "blackbull[compression] @ git+https://github.com/TOKUJI/BlackBull.git@master"

echo
echo ">>> done — instance will be torn down by trap"
