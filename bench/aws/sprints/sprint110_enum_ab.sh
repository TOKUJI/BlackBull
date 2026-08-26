#!/usr/bin/env bash
# Sprint 110 — EC2 A/B for the HTTP/2 enum-lookup change.
#
# ONE self-driving script: up -> fail-safe -> install -> launch -> finish.
# Only the terminal completion marker crosses a turn boundary, per
# `.claude/patterns/chaining-long-running-steps.md`.  Run it detached:
#
#   nohup setsid bash bench/aws/sprints/sprint110_enum_ab.sh \
#       > bench/results/sprint110_ab/driver.log 2>&1 &
#
# Fail-safes, in order of who is trusted least:
#   1. up.sh already sets --instance-initiated-shutdown-behavior terminate.
#   2. This script schedules `shutdown -h +MINUTES` ON the instance, so it
#      self-terminates even if this script, the SSH link, and the agent all
#      die.  The agent is never the sole teardown path.
#   3. ab.sh finish polls raw.tsv to completeness, scp's results back, and
#      only then runs down.sh — the ordering that stops a self-termination
#      from eating the results.
#   4. A local EXIT trap runs down.sh if we die before finish does.
set -uo pipefail

REPO="$(cd "$(dirname "$0")/../../.." && pwd)"
cd "$REPO"

RESULT_DIR="${RESULT_DIR:-bench/results/sprint110_ab}"
MARKER="$RESULT_DIR/COMPLETE"
FAILED="$RESULT_DIR/FAILED"
SELF_SHUTDOWN_MIN="${SELF_SHUTDOWN_MIN:-180}"

REF_BASE="${REF_BASE:-5c9288e}"     # drain seam — before the enum change
REF_TREAT="${REF_TREAT:-2384f2a}"   # the enum change, nothing else
ROUNDS="${ROUNDS:-12}"

mkdir -p "$RESULT_DIR"
rm -f "$MARKER" "$FAILED"

say() { echo "[$(date -u +%H:%M:%S)] $*"; }

teardown_if_still_up() {
    local rc=$?
    if [ ! -f "$MARKER" ]; then
        say "driver exiting rc=$rc without COMPLETE — tearing the instance down"
        bash bench/aws/down.sh >>"$RESULT_DIR/down.log" 2>&1 || true
        echo "driver rc=$rc" > "$FAILED"
    fi
}
trap teardown_if_still_up EXIT

say "=== 1/5  up (m7a.2xlarge, single topology, no SMT) ==="
INSTANCE_TYPE=m7a.2xlarge TOPO=single bash bench/aws/up.sh 2>&1 | tail -20 || {
    say 'up.sh failed'; exit 1; }

# shellcheck disable=SC1091
source bench/aws/config.sh
_bench_aws_load_state || { say 'no state file after up.sh'; exit 1; }
SERVER_REMOTE="$SSH_USER@$SERVER_PUBLIC_IP"
say "instance ${SERVER_INSTANCE_ID:-?} at ${SERVER_PUBLIC_IP:-?}"

say "=== 2/5  fail-safe: self-shutdown in ${SELF_SHUTDOWN_MIN} min ==="
# Runs on the box, so it survives this script, the SSH link, and the agent.
# up.sh already made shutdown mean *terminate*.
ssh "${SSH_OPTS[@]}" "$SERVER_REMOTE" \
    "sudo shutdown -h +${SELF_SHUTDOWN_MIN} 'bench self-terminate backstop' </dev/null >/dev/null 2>&1 &" \
    || say 'WARNING: could not arm the self-shutdown backstop'
ssh "${SSH_OPTS[@]}" "$SERVER_REMOTE" 'sudo shutdown --show 2>&1 | head -2' \
    || true

say "=== 3/5  install (DEPLOY_GIT=1 — ab_commit.sh needs the refs) ==="
DEPLOY_GIT=1 bash bench/aws/install.sh 2>&1 | tail -15 || {
    say 'install.sh failed'; exit 1; }

say "=== 4/5  launch: H1 control lane + H2 lane, ROUNDS=$ROUNDS ==="
# The change is HTTP/2-only (pseudo-headers ×4/req, frame type ×1/req), so
# /plaintext over HTTP/1.1 is a **negative control**: it must show ~0.  If it
# moves with the H2 lane, the session measured something other than the diff.
REF_BASE="$REF_BASE" REF_TREAT="$REF_TREAT" \
    URL_PATHS=/plaintext \
    H2_PROFILES=/plaintext \
    ROUNDS="$ROUNDS" \
    bash bench/aws/ab.sh launch 2>&1 | tail -20 || {
    say 'ab.sh launch failed'; exit 1; }

say "=== 5/5  finish: poll -> scp -> down (this is the long wait) ==="
bash bench/aws/ab.sh finish 2>&1 | tail -40 || { say 'ab.sh finish failed'; exit 1; }

say "=== done ==="
date -u +%FT%TZ > "$MARKER"
say "marker written: $MARKER"
