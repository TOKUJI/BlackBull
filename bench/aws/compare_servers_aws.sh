#!/usr/bin/env bash
# bench/aws/compare_servers_aws.sh — full EC2 peer-comparison driver.
#
# Bundles the individual bench/aws steps (up.sh, install.sh, run.sh,
# down.sh) into ONE lifecycle, httparena-style, so a single nohup'd
# invocation provisions → installs → measures → pulls → tears down
# without the agent being the sole teardown path.
#
# Usage (nohup operation — survives terminal/SSH drops):
#   nohup bash bench/aws/compare_servers_aws.sh </dev/null >/dev/null 2>&1 &
#
# The driver self-documents into <results>/driver.log (see below), so the
# nohup stdout/stderr redirect can be /dev/null.  run.sh launches the
# remote measurement detached (nohup on the instance) and polls + pulls,
# so even a hard ssh drop mid-measurement cannot lose the data.
#
# Env knobs:
#   INSTANCE_TYPE   EC2 instance type (default: m7a.2xlarge — Sprint 100
#                   default; config.sh's c7i.xlarge is the legacy default)
#   TOPO            single (default) | split — passed through to run.sh
#   DEPLOY_GIT      install.sh deploys .git for ab_commit.sh (default 1 —
#                   matches Sprint 100 practice; not needed for the
#                   compare_servers matrix itself)
#   KEEP_INSTANCE   1 → leave the EC2 instance running on exit (debugging;
#                   REMEMBER to `bash bench/aws/down.sh`)
#   SKIP_PROVISION  1 → reuse an already-running instance (requires a valid
#                   bench/aws/.state from a previous KEEP_INSTANCE run)
#   SKIP_INSTALL    1 → skip install.sh (instance already provisioned)
#   SAFETY_SHUTDOWN_MINUTES  instance poweroff timer covering the whole
#                   lifecycle (default 180; run.sh's poll budget defaults
#                   to the same window — see RUN_POLLS below)
#
#   --- run.sh matrix knobs (forwarded verbatim) ---
#   RUNS DURATION LANES STACKS  and the Sprint 100 instruments:
#   BB_UVLOOP BB_GC_STATS BB_LOOP_STAMP BB_RESP_TIMING
#
# Results: everything lands under
#   bench/results/aws/compare-driver-<ts>/
#     driver.log          — this driver's console
#     run.log             — run.sh's console (launch + poll + pull)
#     results/            — the pulled compare_servers_*.md + scratch files
#
# Recovery: if the local side dies mid-run, the remote measurement keeps
# going detached.  On the next session:
#   bash bench/aws/run.sh finish   # poll + pull the completed run
#   bash bench/aws/down.sh         # terminate when done recovering
#
# Fail-safes (never the sole teardown path):
#   1. up.sh sets --instance-initiated-shutdown-behavior=terminate, so any
#      poweroff on the instance terminates it.
#   2. This driver arms `sudo shutdown -h +N` on the instance (N =
#      SAFETY_SHUTDOWN_MINUTES) — an orphaned instance self-terminates.
#   3. down.sh runs in the EXIT trap unless KEEP_INSTANCE=1 or the run
#      timed out with the remote still measuring (TEARDOWN_SKIP).

set -euo pipefail

# Sprint 100 default instance (AMD, no SMT, monomodal).  Set before
# sourcing config.sh so config.sh's `: "${INSTANCE_TYPE:=...}"` no-ops.
: "${INSTANCE_TYPE:=m7a.2xlarge}"
export INSTANCE_TYPE

# shellcheck source=config.sh
source "$(dirname "$0")/config.sh"
_bench_aws_check_env

# Topology passthrough (run.sh reads it from .state; export for clarity).
: "${TOPO:=single}"
export TOPO

KEEP_INSTANCE="${KEEP_INSTANCE:-0}"
SKIP_PROVISION="${SKIP_PROVISION:-0}"
SKIP_INSTALL="${SKIP_INSTALL:-0}"
DEPLOY_GIT="${DEPLOY_GIT:-1}"
SAFETY_SHUTDOWN_MINUTES="${SAFETY_SHUTDOWN_MINUTES:-180}"

TS="$(date -u +%Y%m%d-%H%M%SZ)"
LOCAL_DEST="$REPO_ROOT/bench/results/aws/compare-driver-$TS"
mkdir -p "$LOCAL_DEST"

# run.sh writes its results under this base (results/ subdir), so the
# driver.log and the artefacts live in one destination.
export RUN_BASE_DIR="$LOCAL_DEST"

# Self-document the whole driver console (provisioning, install, run.sh
# poll heartbeats, teardown) into the result directory so the orchestration
# trail lives alongside the artefacts.  The caller may pipe to its own log;
# this is independent.
exec > >(tee -a "$LOCAL_DEST/driver.log") 2>&1

echo "=== bench/aws/compare_servers_aws.sh ==="
echo "  destination:    $LOCAL_DEST"
echo "  instance type:  $INSTANCE_TYPE"
echo "  topology:       $TOPO"
echo "  safety shutdown: ${SAFETY_SHUTDOWN_MINUTES} min (armed on instance)"
echo "  --- run.sh matrix ---"
echo "  RUNS=${RUNS:-<default>}  DURATION=${DURATION:-<default>}  LANES=${LANES:-<default>}  STACKS=${STACKS:-<default>}"
echo "  BB_UVLOOP=${BB_UVLOOP:-<default>}  BB_GC_STATS=${BB_GC_STATS:-0}  BB_LOOP_STAMP=${BB_LOOP_STAMP:-0}  BB_RESP_TIMING=${BB_RESP_TIMING:-0}"
echo "  KEEP_INSTANCE=$KEEP_INSTANCE  SKIP_PROVISION=$SKIP_PROVISION  SKIP_INSTALL=$SKIP_INSTALL  DEPLOY_GIT=$DEPLOY_GIT"
echo

# ---------------------------------------------------------------------------
# Step 1 — provision EC2.
# ---------------------------------------------------------------------------
if [ "$SKIP_PROVISION" != "1" ]; then
    echo ">>> bench/aws/up.sh ..."
    bash "$(dirname "$0")/up.sh"
else
    echo ">>> SKIP_PROVISION=1 — reusing instance from bench/aws/.state"
fi

# ---------------------------------------------------------------------------
# Teardown trap: down.sh on EXIT unless KEEP_INSTANCE=1 or the run timed
# out with the remote still measuring (then the instance stays up so the
# data survives; the scheduled poweroff is the backstop).
# ---------------------------------------------------------------------------
TEARDOWN_SKIP=0
_teardown() {
    local rc=$?
    if [ "$TEARDOWN_SKIP" = "1" ]; then
        echo "TEARDOWN_SKIP=1 — leaving instance up for recovery; fail-safe poweroff is armed."
        return $rc
    fi
    if [ "$KEEP_INSTANCE" = "1" ]; then
        echo "KEEP_INSTANCE=1 — leaving EC2 alive; remember to run 'bash bench/aws/down.sh'"
        return $rc
    fi
    echo ">>> bench/aws/down.sh (trap EXIT) ..."
    bash "$(dirname "$0")/down.sh" || true
    return $rc
}
trap _teardown EXIT

_bench_aws_load_state

SERVER_REMOTE="$SSH_USER@$SERVER_PUBLIC_IP"
echo "    instance: $SERVER_PUBLIC_IP"

# Safety net: schedule a forced poweroff on the instance so an orphaned
# instance (terminal loss, local shutdown, network partition) terminates
# within this window.  up.sh's terminate-on-shutdown turns the poweroff
# into termination.
echo ">>> arming EC2 safety shutdown timer: ${SAFETY_SHUTDOWN_MINUTES} min ..."
ssh "${SSH_OPTS[@]}" "$SERVER_REMOTE" \
    "sudo shutdown -h +${SAFETY_SHUTDOWN_MINUTES} </dev/null >/dev/null 2>&1" || true

# ---------------------------------------------------------------------------
# Step 2 — install BlackBull + bench deps on the instance.
# ---------------------------------------------------------------------------
if [ "$SKIP_INSTALL" != "1" ]; then
    echo ">>> bench/aws/install.sh (DEPLOY_GIT=$DEPLOY_GIT) ..."
    DEPLOY_GIT="$DEPLOY_GIT" bash "$(dirname "$0")/install.sh"
else
    echo ">>> SKIP_INSTALL=1 — skipping install.sh"
fi

# ---------------------------------------------------------------------------
# Step 3 — run the measurement (run.sh: detached remote launch + poll + pull).
# run.sh exit codes: 0 = completed+pulled, 1 = remote died early (partial
# pull, teardown OK), 2 = poll budget exhausted while still running (leave
# instance up; do NOT tear down).
# ---------------------------------------------------------------------------
run_rc=0
bash "$(dirname "$0")/run.sh" || run_rc=$?

if [ "$run_rc" = "2" ]; then
    echo
    echo "!!! run.sh: poll budget exhausted while the remote measurement is"
    echo "    still running.  Results pulled so far are partial.  Leaving the"
    echo "    instance up for recovery:"
    echo "      bash bench/aws/run.sh finish    # poll + pull the completed run"
    echo "      bash bench/aws/down.sh          # terminate when done recovering"
    echo "    The instance poweroff timer (${SAFETY_SHUTDOWN_MINUTES} min) is the backstop."
    TEARDOWN_SKIP=1
    exit 3
elif [ "$run_rc" != "0" ]; then
    echo
    echo "!!! run.sh failed (rc=$run_rc).  Partial results were pulled where"
    echo "    possible; tearing down."
fi

echo
if compgen -G "$LOCAL_DEST/results/compare_servers_*.md" >/dev/null; then
    summary=$(ls -1 "$LOCAL_DEST/results"/compare_servers_*.md | tail -1)
    echo "=== compare_servers_aws.sh done ==="
    echo "  results:    $LOCAL_DEST/results/"
    echo "  summary:    $summary"
    echo "  driver log: $LOCAL_DEST/driver.log"
else
    echo "=== compare_servers_aws.sh done (WARNING: no compare_servers_*.md pulled) ==="
    echo "  results dir: $LOCAL_DEST"
    echo "  driver log:  $LOCAL_DEST/driver.log"
fi
echo "  (instance teardown follows via EXIT trap)"
