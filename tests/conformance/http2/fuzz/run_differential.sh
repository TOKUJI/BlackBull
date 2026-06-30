#!/bin/bash
# H2 differential fuzz run — BlackBull h2c vs nginx h2 TLS
# Run: bash tests/conformance/http2/fuzz/run_differential.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/../../../.." && pwd)"
VENV_PYTHON="$PROJECT_DIR/.venv/bin/python"
CORPUS_DIR="$SCRIPT_DIR/corpus"
USER_CORPUS_DIR="$SCRIPT_DIR/user-corpus"
NGINX_CONF="/tmp/nginx_h2_fuzz.conf"
NGINX_PORT=18443

echo "=== H2 Differential Fuzz Run ==="
echo "Project: $PROJECT_DIR"
echo "Corpus:  $CORPUS_DIR"
echo "User corpus (output): $USER_CORPUS_DIR"
echo ""

# ---- 1. Start nginx H2 ----
echo "[1/5] Starting nginx H2 on port $NGINX_PORT..."
cat > "$NGINX_CONF" << 'NGXEOF'
worker_processes 1;
error_log /tmp/nginx_h2_fuzz_error.log warn;
pid /tmp/nginx_h2_fuzz.pid;
daemon on;
events { worker_connections 64; }
http {
    access_log off;
    server {
        listen 18443 ssl http2 reuseport;
        ssl_certificate     CHANGEME_CERT;
        ssl_certificate_key CHANGEME_KEY;
        ssl_protocols TLSv1.2 TLSv1.3;
        ssl_ciphers HIGH:!aNULL:!MD5;
        location / { return 200 "nginx-ok"; }
    }
}
NGXEOF
sed -i "s|CHANGEME_CERT|$PROJECT_DIR/tests/cert.pem|" "$NGINX_CONF"
sed -i "s|CHANGEME_KEY|$PROJECT_DIR/tests/key.pem|" "$NGINX_CONF"

pkill -F /tmp/nginx_h2_fuzz.pid 2>/dev/null || true
sleep 0.3
nginx -c "$NGINX_CONF"
sleep 0.5

# Verify nginx is alive
if curl -sk --http2 https://localhost:$NGINX_PORT/ 2>/dev/null | grep -q nginx-ok; then
    echo "  nginx H2 OK"
else
    echo "  ERROR: nginx H2 not responding"
    exit 1
fi

# ---- 2. Regenerate seeds ----
echo "[2/5] Regenerating seed corpus..."
rm -f "$CORPUS_DIR"/*.bin
"$VENV_PYTHON" "$SCRIPT_DIR/make_seeds.py"
echo "  $(ls "$CORPUS_DIR"/*.bin 2>/dev/null | wc -l) seeds"

# ---- 3. Clear previous divergences ----
echo "[3/5] Clearing previous divergences..."
rm -f "$USER_CORPUS_DIR"/div_*.bin
rm -f "$PROJECT_DIR"/crash-*

# ---- 4. Run fuzzer ----
echo "[4/5] Running differential fuzz (30s)..."
BB_FUZZ_NGINX_H2_HOST=127.0.0.1 \
BB_FUZZ_NGINX_H2_PORT=$NGINX_PORT \
timeout 35 "$VENV_PYTHON" "$SCRIPT_DIR/fuzz_http2.py" \
    -keep_going=1 \
    -max_total_time=30 \
    "$CORPUS_DIR/" \
    2>&1 | tee /tmp/h2_fuzz_output.txt
FUZZ_EXIT=$?
echo "  fuzzer exit code: $FUZZ_EXIT"

# ---- 5. Collect results ----
echo "[5/5] Results..."
echo ""
echo "Divergences found:"
if ls "$USER_CORPUS_DIR"/div_*.bin 2>/dev/null; then
    echo "  $(ls "$USER_CORPUS_DIR"/div_*.bin 2>/dev/null | wc -l) divergence files"
    for f in "$USER_CORPUS_DIR"/div_*.bin; do
        name=$(basename "$f")
        size=$(wc -c < "$f")
        echo "  $name ($size bytes)"
    done
else
    echo "  (none)"
fi

echo ""
echo "Crashes found:"
if ls "$PROJECT_DIR"/crash-* 2>/dev/null; then
    echo "  $(ls "$PROJECT_DIR"/crash-* 2>/dev/null | wc -l) crash files"
else
    echo "  (none)"
fi

echo ""
echo "Fuzz summary (from /tmp/h2_fuzz_output.txt):"
grep -E "DIV #|DIVERGENCE|SUMMARY|SAVED|Test unit|DONE|error" /tmp/h2_fuzz_output.txt 2>/dev/null | head -10 || echo "  (no summary lines)"

# ---- Cleanup nginx ----
pkill -F /tmp/nginx_h2_fuzz.pid 2>/dev/null || true
echo ""
echo "=== Done ==="
