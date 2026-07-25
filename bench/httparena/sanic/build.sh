#!/usr/bin/env bash
# build.sh — Sanic framework build script for HttpArena.
# Called by HttpArena's validate.sh to build the container image.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
FRAMEWORK="$(basename "$SCRIPT_DIR")"
IMAGE_NAME="httparena-${FRAMEWORK}"
docker build -t "$IMAGE_NAME" "$SCRIPT_DIR"
