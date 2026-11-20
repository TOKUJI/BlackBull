#!/usr/bin/env bash
# bench/load/install_gcannon.sh — gcannon, HttpArena's io_uring load generator.
#
# Needs kernel 6.1+ io_uring, OpenSSL headers and liburing >= 2.9, which is
# built from source because Ubuntu 24.04 ships 2.5.  Idempotent.
set -euo pipefail

sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq build-essential pkg-config libssl-dev git >/dev/null
mkdir -p ~/build && cd ~/build
if ! pkg-config --atleast-version=2.9 liburing 2>/dev/null; then
    rm -rf liburing
    git clone --quiet --depth 1 --branch liburing-2.9 https://github.com/axboe/liburing.git
    (cd liburing && ./configure --prefix=/usr >/dev/null && make -s -j"$(nproc)" -C src \
        && sudo make -s install -C src >/dev/null && sudo ldconfig)
fi
if ! command -v gcannon >/dev/null; then
    rm -rf gcannon
    git clone --quiet --depth 1 https://github.com/MDA2AV/gcannon.git
    (cd gcannon && make -s && sudo cp gcannon /usr/local/bin/)
fi
command -v gcannon >/dev/null
