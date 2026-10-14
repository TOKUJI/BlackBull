#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CLOUD="$ROOT/bench/cloud"
META="$ROOT/bench/httparena/meta.json"

profiles_json="$(jq -ce \
    'if (.tests | type == "array" and length > 0 and all(.[]; type == "string" and length > 0)) then .tests else error("invalid tests") end' \
    "$META")"

case "${1:-}" in
    --plan)
        jq -nc \
            --argjson profiles "$profiles_json" \
            '{workflow:"httparena-bench",requires_approval:true,profiles:$profiles}'
        exit 0
        ;;
    --preflight)
        bash -n "$CLOUD/session.sh" "$CLOUD/remote.sh" "$CLOUD/jobs/httparena.sh" \
            "$CLOUD/up.sh" "$CLOUD/install.sh" "$CLOUD/down.sh"
        jq -nc '{workflow:"httparena-bench",ready:true}'
        exit 0
        ;;
    '') ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
esac

if [ "${APPROVE_CLOUD:-}" != "1" ]; then
    echo "run-httparena-bench: explicit approval required; set APPROVE_CLOUD=1" >&2
    exit 1
fi
PROFILES="$(jq -r '.tests | join(" ")' "$META")" exec bash "$CLOUD/session.sh" httparena
