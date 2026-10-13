#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CLOUD="$ROOT/bench/cloud"

case "${1:-}" in
    --plan)
        jq -nc '{workflow:"ab-verify",requires_approval:true,steps:["up","failsafe","install","launch","wait","pull","down","marker"]}'
        exit 0
        ;;
    --preflight)
        bash -n "$CLOUD/session.sh" "$CLOUD/remote.sh" "$CLOUD/jobs/ab.sh" \
            "$CLOUD/up.sh" "$CLOUD/install.sh" "$CLOUD/down.sh"
        jq -nc '{workflow:"ab-verify",ready:true}'
        exit 0
        ;;
    '') ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
esac

if [ "${APPROVE_CLOUD:-}" != "1" ]; then
    echo "run-ab-verify: explicit approval required; set APPROVE_CLOUD=1" >&2
    exit 1
fi
exec bash "$CLOUD/session.sh" ab
