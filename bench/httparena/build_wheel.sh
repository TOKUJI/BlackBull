#!/usr/bin/env bash
# bench/httparena/build_wheel.sh — build a BlackBull wheel from a git ref for
# the HttpArena local-validation / EC2 workflow.
#
# Same discipline as the EC2 path (bench/aws/httparena_compare.sh): the wheel
# is built from `git archive <ref>` — a clean tree of exactly that commit —
# never from the working copy.  The wheel's sha256 is written alongside it so
# the identity discipline holds end-to-end: WSL2 validates wheel W, EC2 uploads
# the SAME file W (BB_WHEEL_PATH), and the hash proves they are one artifact.
#
# Usage:
#   bash bench/httparena/build_wheel.sh [REF] [OUTDIR]
#     REF     git ref to build (default: HEAD)
#     OUTDIR  destination for dist/ + <wheel>.sha256 (default:
#             /tmp/bb-wheels/<short-ref>)
#
# Env:
#   PYTHON   python interpreter with the `build` package (default: python3)
#
# Prints the wheel path on stdout; exits non-zero on failure.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
REF="${1:-HEAD}"
PYTHON="${PYTHON:-python3}"

cd "$REPO_ROOT"
SHORT_REF="$(git rev-parse --short "${REF}")"
OUTDIR="${2:-/tmp/bb-wheels/${SHORT_REF}}"

# git archive only overwrites, never deletes — stale files from a previous
# build would contaminate the wheel.  rm -rf is the whole point.
rm -rf "$OUTDIR" && mkdir -p "$OUTDIR/src"

echo ">>> building wheel from ref ${REF} (${SHORT_REF}) ..." >&2
git archive "${REF}" | tar -C "$OUTDIR/src" -xf -

if ! "$PYTHON" -c 'import build' 2>/dev/null; then
    echo "ERROR: the 'build' package is missing for $PYTHON — install it:" >&2
    echo "  pip install build" >&2
    exit 1
fi

( cd "$OUTDIR/src" && "$PYTHON" -m build --wheel --outdir dist/ >/dev/null )

WHEEL="$(ls "$OUTDIR/src"/dist/blackbull-*.whl | head -1)"
[ -n "$WHEEL" ] || { echo "ERROR: no wheel produced" >&2; exit 1; }

# Move the wheel next to its sha256 in a flat, predictable location.
mv "$WHEEL" "$OUTDIR/"
WHEEL="$OUTDIR/$(basename "$WHEEL")"
(cd "$OUTDIR" && sha256sum "$(basename "$WHEEL")" > "$(basename "$WHEEL").sha256")

echo "    wheel : $WHEEL" >&2
echo "    sha256: $(awk '{print $1}' "$WHEEL.sha256")" >&2
echo "$WHEEL"
