#!/usr/bin/env bash
# bench/cloud/session.sh — one cloud session: provision, measure, collect, tear down.
#
#   bash bench/cloud/session.sh <job>          start a session
#   bash bench/cloud/session.sh <job> finish   resume a session left measuring
#
# <job> is bench/cloud/jobs/<job>.sh: ab (just ab-verify), compare (peer servers),
# httparena (just httparena-bench).  A job defines
#   job_preflight   local checks; sets JOB_SETTINGS (key=value words for the
#                   manifest), JOB_WAIT_SECONDS, JOB_DEPLOY_GIT (0/1) and
#                   optionally JOB_HOST (server, the default, or loadgen) and
#                   JOB_INSTALL (0 skips install.sh; the job prepares the host)
# A job file may set INSTANCE_TYPE / TOPO defaults at its top level; it is
# sourced before config.sh.
#   job_setup HOST  optional remote preparation before the launch
#   job_runner      print the runner script; it runs on the instance from
#                   $REMOTE_REPO and writes into $REMOTE_RUN
#   job_collect     judge the pulled results in $LOCAL_RUN (status 0 = complete)
#   job_on_poll HOST  optional, after every successful poll
#
# The runner is detached on the instance; this script only polls it, so ssh
# can drop without harm.  The run directory (tool cloud_<job>, see
# bench/lib/results.sh)
# exists on both sides and is pulled back whenever the session stops.
#
# Exit status:
#   0  complete: results pulled, teardown verified, complete.json written
#   1  failed: whatever exists is pulled, then torn down
#   2  still measuring at the wait limit: results so far pulled, instance kept
#   3  instance unreachable: instance kept
# A kept instance terminates itself when the fail-safe shutdown fires, armed
# right after provisioning for the whole session's length.  A session
# interrupted after launch keeps the instance too; `finish` resumes it.
#
# Env: CLOUD_WAIT_SECONDS (overrides the job's), CLOUD_FAILSAFE_MINUTES,
#      CLOUD_POLL_INTERVAL, CLOUD_SSH_FAILURES (see remote.sh), STATE_FILE.

set -uo pipefail

JOB="${1:?usage: bash bench/cloud/session.sh <job> [finish]}"
MODE="${2:-run}"
CLOUD_DIR="$(cd "$(dirname "$0")" && pwd)"
[ -f "$CLOUD_DIR/jobs/$JOB.sh" ] || { echo "session: no job $JOB" >&2; exit 1; }
# shellcheck disable=SC1090
source "$CLOUD_DIR/jobs/$JOB.sh"
# shellcheck source=config.sh
source "$CLOUD_DIR/config.sh"
# shellcheck source=remote.sh
source "$CLOUD_DIR/remote.sh"
export INSTANCE_TYPE TOPO
_bench_aws_check_env || exit 1
SESSION_FILE="$STATE_DIR/session"
cd "$REPO_ROOT" || exit 1

die() { echo "session: $*" >&2; exit 1; }

LAUNCHED=0
KEEP=0
on_exit() {
    local rc=$?
    trap - EXIT
    if [ "$LAUNCHED" = 1 ] && [ "$rc" -ne 0 ] && [ "$rc" -ne 1 ]; then KEEP=1; fi
    if [ "$KEEP" = 0 ] && [ -f "$STATE_FILE" ]; then
        bash "$CLOUD_DIR/down.sh" || rc=1
    fi
    exit "$rc"
}

host_of() {
    if [ "${1:-server}" = loadgen ]; then echo "$SSH_USER@$LOADGEN_PUBLIC_IP"
    else echo "$SSH_USER@$SERVER_PUBLIC_IP"; fi
}

finish() {
    local rc ok=0
    echo "waiting up to ${WAIT_SECONDS} s for the runner ..."
    remote_wait "$HOST" "$RUN_REL" "$WAIT_SECONDS"
    rc=$?
    echo "pulling $RUN_REL ..."
    remote_pull "$HOST" "$RUN_REL" "$LOCAL_RUN" || echo "WARNING: pull failed; results stay on the instance" >&2
    case "$rc" in
        0) if job_collect; then ok=1; else echo "session: the runner finished but its results are incomplete" >&2; fi ;;
        1) echo "session: the runner failed (status ${REMOTE_STATUS:-?}); see $LOCAL_RUN/runner.log" >&2 ;;
        2|3)
            KEEP=1
            if [ "$rc" = 2 ]; then echo "Still measuring after ${WAIT_SECONDS} s."
            else echo "The instance stopped answering ssh."; fi
            echo "  results so far: $LOCAL_RUN"
            echo "  the instance terminates itself by $(date -u -d "@$FAILSAFE_AT" '+%H:%M UTC')"
            echo "  resume:   bash bench/cloud/session.sh $JOB finish"
            echo "  give up:  bash bench/cloud/down.sh"
            exit "$rc"
            ;;
    esac
    KEEP=1  # this function tears down itself, so the trap must not repeat it
    bash "$CLOUD_DIR/down.sh" || die "teardown failed; $STATE_FILE is retained — rerun bash bench/cloud/down.sh"
    [ ! -e "$STATE_FILE" ] && [ -f "$STATE_FILE.clean" ] || die "teardown is not verified"
    rm -f "$SESSION_FILE"
    [ "$ok" = 1 ] || exit 1
    printf '{"workflow":"%s","complete":true,"teardown_verified":true,"revision":"%s"}\n' \
        "$JOB" "$(git rev-parse HEAD)" > "$LOCAL_RUN/complete.json"
    echo "$LOCAL_RUN/complete.json"
}

if [ "$MODE" = finish ]; then
    [ -f "$SESSION_FILE" ] || die "no session to finish ($SESSION_FILE)"
    # shellcheck disable=SC1090
    source "$SESSION_FILE"
    [ "$SESSION_JOB" = "$JOB" ] || die "the open session is $SESSION_JOB, not $JOB"
    _bench_aws_load_state || exit 1
    WAIT_SECONDS="${CLOUD_WAIT_SECONDS:-$WAIT_SECONDS}"
    HOST="$(host_of "$JOB_HOST")"
    LAUNCHED=1
    exec > >(tee -a "$LOCAL_RUN/session.log") 2>&1
    trap on_exit EXIT
    trap 'exit 130' HUP INT TERM
    finish
    exit 0
fi
[ "$MODE" = run ] || die "unknown mode $MODE"

[ ! -e "$STATE_FILE" ] || die "a harness state exists at $STATE_FILE — finish that session or run bench/cloud/down.sh"
JOB_HOST=server
JOB_INSTALL=1
job_preflight || exit 1
WAIT_SECONDS="${CLOUD_WAIT_SECONDS:-$JOB_WAIT_SECONDS}"
FAILSAFE_MINUTES="${CLOUD_FAILSAFE_MINUTES:-$(( (WAIT_SECONDS + 1800) / 60 + 15 ))}"
FAILSAFE_AT=$(( $(date +%s) + FAILSAFE_MINUTES * 60 ))

source "$REPO_ROOT/bench/lib/results.sh"
bench_run_dir "cloud_$JOB" instance_type="$INSTANCE_TYPE" topo="${TOPO:-single}" \
    wait_seconds="$WAIT_SECONDS" failsafe_minutes="$FAILSAFE_MINUTES" "${JOB_SETTINGS[@]}" || exit 1
LOCAL_RUN="$BENCH_RUN_DIR"
RUN_REL="${LOCAL_RUN#"$REPO_ROOT"/}"
REMOTE_RUN="$REMOTE_REPO/$RUN_REL"
exec > >(tee -a "$LOCAL_RUN/session.log") 2>&1
echo "=== cloud session: $JOB -> $LOCAL_RUN"
echo "  wait limit ${WAIT_SECONDS} s, fail-safe shutdown in ${FAILSAFE_MINUTES} min"

trap on_exit EXIT
trap 'exit 130' HUP INT TERM
bash "$CLOUD_DIR/up.sh" || exit 1
_bench_aws_load_state || exit 1
HOST="$(host_of "$JOB_HOST")"
mkdir -p "$STATE_DIR"
printf 'SESSION_JOB=%q\nLOCAL_RUN=%q\nRUN_REL=%q\nREMOTE_RUN=%q\nWAIT_SECONDS=%q\nFAILSAFE_AT=%q\nJOB_HOST=%q\n' \
    "$JOB" "$LOCAL_RUN" "$RUN_REL" "$REMOTE_RUN" "$WAIT_SECONDS" "$FAILSAFE_AT" "$JOB_HOST" \
    > "$SESSION_FILE"

for ip in "$SERVER_PUBLIC_IP" "${LOADGEN_PUBLIC_IP:-}"; do
    [ -n "$ip" ] || continue
    ssh -n "${SSH_OPTS[@]}" "$SSH_USER@$ip" \
        "sudo shutdown -h +$FAILSAFE_MINUTES </dev/null >/dev/null 2>&1" \
        || die "could not arm the fail-safe shutdown on $ip"
done
if [ "$JOB_INSTALL" = 1 ]; then
    DEPLOY_GIT="$JOB_DEPLOY_GIT" bash "$CLOUD_DIR/install.sh" || exit 1
fi
if declare -F job_setup > /dev/null; then job_setup "$HOST" || exit 1; fi

job_runner > "$BENCH_TMP/runner.sh" || exit 1
remote_launch "$HOST" "$BENCH_TMP/runner.sh" "$RUN_REL" || die "the runner did not start"
LAUNCHED=1
finish
