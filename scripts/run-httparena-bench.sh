#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
META="$ROOT/bench/httparena/meta.json"
DRIVER="$ROOT/bench/aws/httparena_compare.sh"

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
        bash -n "$ROOT/bench/aws/up.sh" "$ROOT/bench/aws/install.sh" \
            "$DRIVER" "$ROOT/bench/aws/down.sh"
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
if [ "${KEEP_INSTANCE:-0}" != "0" ] || [ "${SKIP_PROVISION:-0}" != "0" ]; then
    echo "run-httparena-bench: KEEP_INSTANCE and SKIP_PROVISION must remain 0" >&2
    exit 1
fi

cd "$ROOT"
state_file="${STATE_FILE:-$ROOT/bench/aws/.state}"
teardown_proof="${state_file}.clean"
[ ! -e "$state_file" ] || {
    echo "run-httparena-bench: an existing AWS harness state must be handled first" >&2
    exit 1
}
rm -f "$teardown_proof"
owns_run=1
cleanup() {
    local rc=$?
    trap - EXIT HUP INT TERM
    if [ "$owns_run" -eq 1 ] && [ -f "$state_file" ]; then
        if ! STATE_FILE="$state_file" bash bench/aws/down.sh; then
            rc=1
        fi
    fi
    exit "$rc"
}
trap cleanup EXIT
trap 'exit 130' HUP INT TERM

profiles="$(jq -r '.tests | join(" ")' "$META")"
STATE_FILE="$state_file" KEEP_INSTANCE=0 SKIP_PROVISION=0 PROFILES="$profiles" \
    bash "$DRIVER"
[ ! -e "$state_file" ] && [ -f "$teardown_proof" ] || {
    echo "run-httparena-bench: verified teardown proof is missing" >&2
    exit 1
}
owns_run=0

mkdir -p bench/results/httparena
marker="bench/results/httparena/httparena-$(date -u +%Y%m%dT%H%M%SZ)-$$.complete.json"
marker_tmp="$(mktemp "bench/results/httparena/.complete.XXXXXX")"
jq -nc \
    --argjson profiles "$profiles_json" \
    --arg revision "$(git rev-parse HEAD)" \
    '{workflow:"httparena-bench",complete:true,teardown_verified:true,profiles:$profiles,revision:$revision}' \
    > "$marker_tmp"
mv "$marker_tmp" "$marker"
trap - EXIT HUP INT TERM
echo "$marker"
