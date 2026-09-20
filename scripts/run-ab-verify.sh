#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

case "${1:-}" in
    --plan)
        jq -nc '{workflow:"ab-verify",requires_approval:true,steps:["up","failsafe","install","launch","finish","marker"]}'
        exit 0
        ;;
    --preflight)
        bash -n "$ROOT/bench/aws/up.sh" "$ROOT/bench/aws/install.sh" \
            "$ROOT/bench/aws/ab.sh" "$ROOT/bench/aws/down.sh"
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

failsafe_minutes="${AB_FAILSAFE_MINUTES:-180}"
[[ "$failsafe_minutes" =~ ^[1-9][0-9]*$ ]] || {
    echo "run-ab-verify: AB_FAILSAFE_MINUTES must be a positive integer" >&2
    exit 1
}

cd "$ROOT"
state_file="${STATE_FILE:-$ROOT/bench/aws/.state}"
teardown_proof="${state_file}.clean"
[ ! -e "$state_file" ] || {
    echo "run-ab-verify: an existing AWS harness state must be handled first" >&2
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

STATE_FILE="$state_file" bash bench/aws/up.sh

# shellcheck source=bench/aws/config.sh
source bench/aws/config.sh
_bench_aws_load_state
arm_failsafe() {
    local host="$1"
    ssh -n "${SSH_OPTS[@]}" "$SSH_USER@$host" \
        "sudo shutdown -h +$failsafe_minutes </dev/null >/dev/null 2>&1"
}
arm_failsafe "$SERVER_PUBLIC_IP"
if [ -n "${LOADGEN_PUBLIC_IP:-}" ]; then
    arm_failsafe "$LOADGEN_PUBLIC_IP"
fi

STATE_FILE="$state_file" DEPLOY_GIT=1 bash bench/aws/install.sh
STATE_FILE="$state_file" bash bench/aws/ab.sh launch
STATE_FILE="$state_file" bash bench/aws/ab.sh finish
[ ! -e "$state_file" ] && [ -f "$teardown_proof" ] || {
    echo "run-ab-verify: verified teardown proof is missing" >&2
    exit 1
}
owns_run=0

mkdir -p bench/results
marker="bench/results/ab-verify-$(date -u +%Y%m%dT%H%M%SZ)-$$.complete.json"
marker_tmp="$(mktemp "bench/results/.ab-verify.XXXXXX")"
jq -nc \
    --arg revision "$(git rev-parse HEAD)" \
    '{workflow:"ab-verify",complete:true,teardown_verified:true,revision:$revision}' > "$marker_tmp"
mv "$marker_tmp" "$marker"
trap - EXIT HUP INT TERM
echo "$marker"
