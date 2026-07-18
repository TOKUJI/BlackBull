#!/usr/bin/env bash
# Install benchmark tooling on Debian/Ubuntu (including WSL2)
#
# Run once:
#   bash bench/install.sh
#
# Installs:
#   - h2load (nghttp2-client)     HTTP/2 load gen
#   - wrk                         HTTP/1.1 load gen (TechEmpower-style)
#   - wrk2 (built from source)    Constant-rate, CO-corrected HdrHistogram
#   - oha                         HTTP/1.1 load gen (granian-style)
#   - k6                          VU-based load gen + WebSocket
#   - nginx                       reference floor (static plaintext/json)
#   - uvicorn, hypercorn, granian, daphne, starlette  peer ASGI servers

set -e

echo "=== Installing h2load (nghttp2-client) + wrk + nginx ==="
sudo apt-get update -qq
sudo apt-get install -y nghttp2-client wrk nginx libssl-dev build-essential

echo ""
echo "=== Building wrk2 (Gil Tene's CO-corrected fork) ==="
# Local patch: upstream sample_rate() divides by elapsed_ms without guarding
# against 0, which on fast servers (sub-ms first sample) yields +Inf and a
# garbage uint64 that aborts the HDR histogram with
# `bucket_index < h->bucket_count`. Patch the divisor guard before build.
if [ "$(uname -m)" != "x86_64" ]; then
    # wrk2 bundles LuaJIT, whose build errors "No support for this
    # architecture" on aarch64 (Graviton).  wrk2 backs only the constant-rate
    # Lane B2r; skip it on non-x86 so the rest of the toolchain (apt wrk,
    # h2load, nginx) still installs.  Lane B2r is unavailable on this arch.
    echo "wrk2 skipped on $(uname -m) (LuaJIT unsupported; Lane B2r x86-only)"
elif [ ! -x "$HOME/.local/bin/wrk2" ]; then
    mkdir -p "$HOME/.local/src" "$HOME/.local/bin"
    cd "$HOME/.local/src"
    [ -d wrk2 ] || git clone --depth=1 https://github.com/giltene/wrk2.git
    cd wrk2
    if ! grep -q 'elapsed_ms ? (thread->requests' src/wrk.c; then
        sed -i 's|uint64_t requests = (thread->requests / (double) elapsed_ms) \* 1000;|uint64_t requests = elapsed_ms ? (thread->requests / (double) elapsed_ms) * 1000 : 0;|' src/wrk.c
        echo "Patched sample_rate() divide-by-zero in src/wrk.c"
    fi
    # Patch 2: response_complete() records expected_latency_timing into the
    # HDR histogram unguarded. When the timing is negative (early start of
    # the batch logic — the author even left a 'we are about to crash and
    # die' printf in the path), the negative int64 gets reinterpreted as a
    # huge uint64 and aborts on bucket_index. Guard the record calls.
    if ! grep -q 'expected_latency_timing >= 0' src/wrk.c; then
        python3 - <<'PYEOF'
import pathlib
p = pathlib.Path('src/wrk.c')
src = p.read_text()
needle = ("if (cfg.record_all_responses || !c->has_pending) {\n"
          "        hdr_record_value(thread->latency_histogram, expected_latency_timing);\n"
          "\n"
          "        uint64_t actual_latency_timing = now - c->actual_latency_start;\n"
          "        hdr_record_value(thread->u_latency_histogram, actual_latency_timing);\n"
          "    }")
replacement = ("if (cfg.record_all_responses || !c->has_pending) {\n"
               "        if (expected_latency_timing >= 0) {\n"
               "            hdr_record_value(thread->latency_histogram, expected_latency_timing);\n"
               "        }\n"
               "\n"
               "        int64_t actual_latency_timing = now - c->actual_latency_start;\n"
               "        if (actual_latency_timing >= 0) {\n"
               "            hdr_record_value(thread->u_latency_histogram, actual_latency_timing);\n"
               "        }\n"
               "    }")
assert needle in src, 'response_complete pattern not found — wrk2 source layout changed'
p.write_text(src.replace(needle, replacement))
PYEOF
        echo "Patched response_complete() negative-latency record in src/wrk.c"
    fi
    make clean >/dev/null 2>&1 || true
    make
    cp wrk "$HOME/.local/bin/wrk2"
    cd - >/dev/null
    echo "wrk2 installed at $HOME/.local/bin/wrk2"
else
    echo "wrk2 already installed at $HOME/.local/bin/wrk2 (run 'rm \$HOME/.local/bin/wrk2' to force a patched rebuild)"
fi

echo ""
echo "=== Installing oha (Rust HTTP load tool, granian-style) ==="
if ! command -v oha >/dev/null 2>&1; then
    # Prefer apt (Debian ≥13, some PPAs); fall back to the upstream GitHub
    # release binary (single static x86_64 ELF, works on Ubuntu 22.04/24.04);
    # last resort cargo if a Rust toolchain is around.
    if apt-cache show oha >/dev/null 2>&1; then
        sudo apt-get install -y oha
    elif [ "$(uname -s)-$(uname -m)" = "Linux-x86_64" ] && command -v curl >/dev/null 2>&1; then
        echo "  apt has no 'oha' package; downloading static binary from GitHub releases ..."
        tmp_oha=$(mktemp)
        if curl -fsSL -o "$tmp_oha" \
                "https://github.com/hatoo/oha/releases/latest/download/oha-linux-amd64"; then
            sudo install -m 0755 "$tmp_oha" /usr/local/bin/oha
            rm -f "$tmp_oha"
        else
            rm -f "$tmp_oha"
            echo "  failed to download oha from GitHub releases" >&2
            exit 1
        fi
    elif command -v cargo >/dev/null 2>&1; then
        cargo install oha
    else
        echo "  oha not packaged, no x86_64 release binary path, and cargo not installed." >&2
        echo "  Install Rust (curl https://sh.rustup.rs | sh) or fetch manually from" >&2
        echo "    https://github.com/hatoo/oha/releases" >&2
        exit 1
    fi
else
    echo "oha already installed, skipping."
fi

echo ""
echo "=== Installing k6 ==="
if ! command -v k6 >/dev/null 2>&1; then
  sudo apt-get install -y gnupg curl
  curl -fsSL https://dl.k6.io/key.gpg \
    | sudo gpg --batch --yes --dearmor \
        -o /usr/share/keyrings/k6-archive-keyring.gpg
  echo "deb [signed-by=/usr/share/keyrings/k6-archive-keyring.gpg] https://dl.k6.io/deb stable main" \
    | sudo tee /etc/apt/sources.list.d/k6.list
  sudo apt-get update -qq
  sudo apt-get install -y k6
else
  echo "k6 already installed, skipping."
fi

echo ""
echo "=== Installing peer ASGI servers ==="
pip install --upgrade \
    uvicorn[standard] \
    hypercorn \
    granian \
    daphne \
    starlette

echo ""
echo "=== Versions ==="
h2load --version | head -1
wrk --version 2>&1 | head -1 || true
oha --version 2>/dev/null || echo "oha: not installed"
k6 version
python3 -c "import uvicorn;  print('uvicorn   ', uvicorn.__version__)"
python3 -c "import hypercorn; print('hypercorn ', getattr(hypercorn, '__version__', '(no __version__)'))"
python3 -c "import granian;   print('granian   ', granian.__version__)"
python3 -c "import daphne;    print('daphne    ', daphne.__version__)"
python3 -c "import starlette; print('starlette ', starlette.__version__)"

echo ""
echo "=== Setup complete ==="
echo "Start a peer server:    bash bench/peers/run_peer.sh blackbull"
echo "Run BlackBull suite:    bash bench/benchmark.sh"
echo "Run peer comparison:    bash bench/peers/compare.sh"
