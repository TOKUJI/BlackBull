#!/usr/bin/env bash
# Install k6 and h2load on Debian/Ubuntu (including WSL2)
#
# Run once:
#   bash bench/install.sh

set -e

echo "=== Installing h2load (nghttp2-client) ==="
sudo apt-get update -qq
sudo apt-get install -y nghttp2-client

echo ""
echo "=== Installing k6 ==="
# Official Grafana k6 APT repository
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
echo "=== Versions ==="
k6 version
h2load --version | head -1

echo ""
echo "=== Setup complete ==="
echo "Start the bench server:  python bench/app.py"
echo "Then run:                k6 run bench/k6/http_rampup.js"
echo "                         bash bench/h2load_run.sh"
