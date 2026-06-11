#!/usr/bin/env bash
# bench/httparena/sprint37/sprint37_install_docker.sh — runs on EC2.
#
# `bench/aws/install.sh` sets up BlackBull's bench harness (native
# Python).  HttpArena's framework images need Docker, which Ubuntu
# 24.04 doesn't ship by default.  This script installs Docker
# Engine + adds the `ubuntu` user to the docker group.
#
# Idempotent — safe to re-run; checks `docker version` before
# installing, and `usermod -aG` is a no-op if already a member.
#
# IMPORTANT: group-membership changes take effect on the NEXT SSH
# session.  The orchestrator (sprint37_drive.sh) runs this in one
# session, then opens a fresh session for sprint37_remote.sh so
# the bench user is in the docker group when validate.sh /
# benchmark.sh invoke `docker` without sudo.

set -euo pipefail

if command -v docker >/dev/null 2>&1 && docker version >/dev/null 2>&1; then
    echo "docker already installed and reachable"
    exit 0
fi

if ! command -v docker >/dev/null 2>&1; then
    echo "=== installing Docker Engine via official convenience script ==="
    curl -fsSL https://get.docker.com -o /tmp/get-docker.sh
    sudo sh /tmp/get-docker.sh
    rm -f /tmp/get-docker.sh
fi

echo "=== enabling + starting docker.service ==="
sudo systemctl enable --now docker

echo "=== adding 'ubuntu' to docker group ==="
sudo usermod -aG docker ubuntu

echo "=== docker installed; group membership will be effective in next SSH session ==="
docker --version || sudo docker --version
