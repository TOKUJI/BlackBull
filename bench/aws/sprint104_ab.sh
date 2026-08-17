#!/usr/bin/env bash
# bench/aws/sprint104_ab.sh — Sprint 104 close A/B, one self-driving EC2 session.
#
# Answers the two measurements Sprint 104 owed before close (sprint-104.md,
# "Folded measurements, owed before close"): the combined-package EC2 A/B and
# "/upload with the defence work (never measured)" — in ONE session, per the
# measurement discipline (cross-session composition is forbidden).
#
#   base  : REF_BASE (default e736c10 — v0.76.1 master; the user chose the
#           full 103+104 span: the §3.8 run certified e736c10 -> 512e8b2,
#           this session re-certifies the whole window in one measurement)
#   treat : REF_TREAT (required — sprint-104 branch tip incl. review fixes)
#   lanes : /conn        — H1 keep-alive no-body control (defence-free path)
#           /upload 256KiB — body path WITH the BB_MAX_BODY_SIZE /
#           BB_MIN_BODY_RATE defence (per-chunk monotonic() + window compares;
#           this lane has never been measured with the defence enabled)
#           /1kb (H2)    — h2c lane via ab_commit_h2.sh: accident check for
#           the H2 watchdog + frame-rate meter sites (user decision)
#   design: ROUNDS=8 ABBA + null, m7a.2xlarge TOPO=single (8 physical cores,
#           no SMT), disjoint CPU pinning, import-hash swap proof — the
#           ab-verify methodology (bench/peers/AB-HIGH-PRECISION.md).
#
# Uses bench/aws/ab.sh launch/finish EXCLUSIVELY — never an ad-hoc runner on
# the instance (the hard rule in AB-HIGH-PRECISION.md §4 / ab-verify skill).
# Writes a completion marker; the instance carries its own fail-safes
# (terminate-on-shutdown from up.sh + a scheduled poweroff armed below) so
# the agent is never the sole teardown path.
#
# Usage:
#   REF_TREAT=<tip> bash bench/aws/sprint104_ab.sh
#
# Env (defaults):
#   REF_BASE(=e736c10) REF_TREAT(required) ROUNDS(=8) DURATION(=15)
#   WARMUP(=5) THREADS(=4) CONNS(=32) PORT(=8443)
#   H2_PROFILES(=/1kb) H2_CONNS(=32) H2_STREAMS(=16) H2_N(=100000)
#   H2_WARMUP(=10000)
#   SAFETY_SHUTDOWN_MINUTES(=180) KEEP_INSTANCE(=0)
#   SKIP_PROVISION(=0) SKIP_INSTALL(=0)
#   AB_POLLS(=720) AB_POLL_INTERVAL(=10)      # finish poll budget, ~120 min
#   MARKER(=bench/results/sprint104-ab-DONE)
#
# Output:
#   bench/results/aws/sprint104-ab-<TS>/driver.log   — full orchestration trail
#   bench/results/ab-commit-<TS>Z/{report.md,raw.tsv} — per lane, pulled by ab.sh
#   $MARKER                                          — completion marker

set -uo pipefail

# Sprint 100/103 instance: AMD, no SMT, monomodal — set before sourcing
# config.sh so its `: "${INSTANCE_TYPE:=...}"` no-ops.
: "${INSTANCE_TYPE:=m7a.2xlarge}"
export INSTANCE_TYPE

# shellcheck source=config.sh
source "$(dirname "$0")/config.sh"
_bench_aws_check_env || exit 1

: "${TOPO:=single}"
export TOPO

REF_BASE="${REF_BASE:-e736c10}"
REF_TREAT="${REF_TREAT:-}"
ROUNDS="${ROUNDS:-8}"
DURATION="${DURATION:-15}"
WARMUP="${WARMUP:-5}"
THREADS="${THREADS:-4}"
CONNS="${CONNS:-32}"
PORT="${PORT:-8443}"
H2_PROFILES="${H2_PROFILES:-/1kb}"
# The H1 lane set, overridable.  Defaults reproduce the original close run
# (/conn + /upload 256 KiB); the ±0.5 % equivalence re-run drops /upload, whose
# null floor has been dirty in every session so far, and spends the rounds on
# the two lanes that can actually return a verdict.  Empty WRK_SCRIPTS means
# "no lua for any lane" — the comma-separated lists are positional against
# URL_PATHS, so they have to be cleared together with it.
URL_PATHS="${URL_PATHS:-/conn,/upload}"
WRK_SCRIPTS="${WRK_SCRIPTS-,bench/wrk/post_echo.lua}"
WRK_SCRIPT_ARGSS="${WRK_SCRIPT_ARGSS-,262144}"
H2_CONNS="${H2_CONNS:-32}"
H2_STREAMS="${H2_STREAMS:-16}"
H2_N="${H2_N:-100000}"
H2_WARMUP="${H2_WARMUP:-10000}"
KEEP_INSTANCE="${KEEP_INSTANCE:-0}"
SKIP_PROVISION="${SKIP_PROVISION:-0}"
SKIP_INSTALL="${SKIP_INSTALL:-0}"
SAFETY_SHUTDOWN_MINUTES="${SAFETY_SHUTDOWN_MINUTES:-180}"
AB_POLLS="${AB_POLLS:-720}"
AB_POLL_INTERVAL="${AB_POLL_INTERVAL:-10}"
MARKER="${MARKER:-$REPO_ROOT/bench/results/sprint104-ab-DONE}"

# ---------------------------------------------------------------------------
# Preflight (fail fast, before any money is spent).
# ---------------------------------------------------------------------------
if [ -z "$REF_TREAT" ]; then
    echo "bench/aws/sprint104_ab.sh: REF_TREAT is required" >&2
    exit 1
fi
for ref in "$REF_BASE" "$REF_TREAT"; do
    if ! git rev-parse --verify --quiet "$ref^{commit}" >/dev/null 2>&1; then
        echo "bench/aws/sprint104_ab.sh: ref '$ref' does not exist locally" >&2
        exit 1
    fi
done
# No uncommitted changes under the SWAP SET (blackbull/ — PATHSPEC below):
# install.sh rsyncs the working tree to the instance, and ab_commit.sh
# refuses to swap files that differ from HEAD.  Files outside the swap set
# (e.g. this driver, an uncommitted harness tweak) ride along harmlessly —
# only dirtiness in the files actually swapped breaks the measurement.
if ! git diff --quiet -- blackbull/ || ! git diff --cached --quiet -- blackbull/; then
    echo "bench/aws/sprint104_ab.sh: uncommitted changes under blackbull/" >&2
    echo "  — commit or stash first (they would be deployed and the swap" >&2
    echo "  would trip ab_commit.sh's dirty-FILES guard)." >&2
    exit 1
fi

TS="$(date -u +%Y%m%d-%H%M%SZ)"
LOCAL_DEST="$REPO_ROOT/bench/results/aws/sprint104-ab-$TS"
mkdir -p "$LOCAL_DEST"

# Self-document the whole driver console into the result directory.
exec > >(tee -a "$LOCAL_DEST/driver.log") 2>&1

echo "=== bench/aws/sprint104_ab.sh ==="
echo "  destination:   $LOCAL_DEST"
echo "  instance type: $INSTANCE_TYPE  topology: $TOPO"
echo "  base:          $REF_BASE  treat: $REF_TREAT"
echo "  lanes:         /conn + /upload 256 KiB (post_echo.lua, body=262144)"
echo "  h2 lanes:      ${H2_PROFILES:-<none>} (h2load -c$H2_CONNS -m$H2_STREAMS -n$H2_N)"
echo "  rounds:        $ROUNDS ABBA + null  duration: ${DURATION}s"
echo "  safety:        poweroff in ${SAFETY_SHUTDOWN_MINUTES} min on instance"
echo "  finish budget: ${AB_POLLS} polls x ${AB_POLL_INTERVAL}s"
echo "  KEEP_INSTANCE=$KEEP_INSTANCE  SKIP_PROVISION=$SKIP_PROVISION  SKIP_INSTALL=$SKIP_INSTALL"
echo

# ---------------------------------------------------------------------------
# Step 1 — provision EC2 (m7a.2xlarge, TOPO=single).
# ---------------------------------------------------------------------------
if [ "$SKIP_PROVISION" != "1" ]; then
    echo ">>> bench/aws/up.sh ..."
    bash "$(dirname "$0")/up.sh" || exit 1
else
    echo ">>> SKIP_PROVISION=1 — reusing instance from bench/aws/.state"
fi

# ---------------------------------------------------------------------------
# Teardown trap: down.sh on EXIT unless KEEP_INSTANCE=1 or the finish poll
# timed out with the remote still measuring (then leave the instance up so
# the data survives; the scheduled poweroff is the backstop).
# ---------------------------------------------------------------------------
TEARDOWN_SKIP=0
_teardown() {
    local rc=$?
    if [ "$TEARDOWN_SKIP" = "1" ]; then
        echo "TEARDOWN_SKIP=1 — leaving instance up for recovery; fail-safe poweroff is armed."
        return $rc
    fi
    if [ "$KEEP_INSTANCE" = "1" ]; then
        echo "KEEP_INSTANCE=1 — leaving EC2 alive; remember 'bash bench/aws/down.sh'"
        return $rc
    fi
    echo ">>> bench/aws/down.sh (trap EXIT) ..."
    bash "$(dirname "$0")/down.sh" || true
    return $rc
}
trap _teardown EXIT

_bench_aws_load_state || exit 1
SERVER_REMOTE="$SSH_USER@$SERVER_PUBLIC_IP"
echo "    instance: $SERVER_PUBLIC_IP"

# Fail-safe: schedule a forced poweroff so an orphaned instance terminates
# within this window.  up.sh's terminate-on-shutdown turns it into
# termination.
echo ">>> arming EC2 safety shutdown timer: ${SAFETY_SHUTDOWN_MINUTES} min ..."
ssh "${SSH_OPTS[@]}" "$SERVER_REMOTE" \
    "sudo shutdown -h +${SAFETY_SHUTDOWN_MINUTES} </dev/null >/dev/null 2>&1" || true

# ---------------------------------------------------------------------------
# Step 2 — install BlackBull + bench deps + .git (A/B swap needs the refs).
# ---------------------------------------------------------------------------
if [ "$SKIP_INSTALL" != "1" ]; then
    echo ">>> bench/aws/install.sh (DEPLOY_GIT=1) ..."
    DEPLOY_GIT=1 bash "$(dirname "$0")/install.sh" || exit 1
else
    echo ">>> SKIP_INSTALL=1 — skipping install.sh"
fi

# ---------------------------------------------------------------------------
# Step 3 — launch the A/B measurement (detached on the instance) and finish
# (poll -> scp -> results local).  ab.sh handles both; the runner inside is
# ab_commit.sh, not an ad-hoc script.
# ---------------------------------------------------------------------------
# Snapshot the pre-existing result dirs so the analysis below only touches
# the ones THIS session produced (bench/results accumulates past runs).
mapfile -t PRE_EXISTING < <(ls -d "$REPO_ROOT"/bench/results/ab-commit-* \
                                  "$REPO_ROOT"/bench/results/ab-h2-* 2>/dev/null || true)
echo ">>> bench/aws/ab.sh launch ..."
REF_BASE="$REF_BASE" REF_TREAT="$REF_TREAT" PATHSPEC=blackbull/ \
    URL_PATHS="$URL_PATHS" \
    WRK_SCRIPTS="$WRK_SCRIPTS" WRK_SCRIPT_ARGSS="$WRK_SCRIPT_ARGSS" \
    H2_PROFILES="$H2_PROFILES" \
    H2_CONNS="$H2_CONNS" H2_STREAMS="$H2_STREAMS" H2_N="$H2_N" H2_WARMUP="$H2_WARMUP" \
    ROUNDS="$ROUNDS" DURATION="$DURATION" WARMUP="$WARMUP" \
    THREADS="$THREADS" CONNS="$CONNS" PORT="$PORT" \
    bash "$(dirname "$0")/ab.sh" launch || exit 1

# From here the instance holds data that exists nowhere else: the runner is
# nohup'd on the box and writes raw.tsv there, and nothing has been pulled yet.
# So the EXIT trap must stop tearing down.  Without this, a signal to this
# driver — the agent driving it being stopped, a terminal closing — runs
# down.sh and destroys an hour of measurement that was otherwise complete and
# recoverable.  The instance's own scheduled poweroff plus terminate-on-
# shutdown remain the backstop, so nothing is orphaned either way; the choice
# here is only between "lose the data now" and "keep it until someone pulls
# it, or the timer fires".
TEARDOWN_SKIP=1

echo ">>> bench/aws/ab.sh finish (TEARDOWN=0 — the EXIT trap owns teardown) ..."
AB_POLLS="$AB_POLLS" AB_POLL_INTERVAL="$AB_POLL_INTERVAL" \
    TEARDOWN=0 bash "$(dirname "$0")/ab.sh" finish
finish_rc=$?
if [ "$finish_rc" != "0" ]; then
    echo
    echo "!!! ab.sh finish failed (rc=$finish_rc).  Results may be partial;"
    echo "    instance left up for recovery (TEARDOWN=0)."
    echo "      bash bench/aws/ab.sh finish    # resume polling + pull"
    echo "    The instance poweroff timer (${SAFETY_SHUTDOWN_MINUTES} min) is the backstop."
    TEARDOWN_SKIP=1
    exit 3
fi

# finish pulled the results, so they now exist locally and the instance is
# expendable again — re-arm the trap so a normal completion still tears down
# rather than leaving a box idling until its poweroff timer.
TEARDOWN_SKIP=0

# ---------------------------------------------------------------------------
# Step 4 — local analysis (EC2 is monomodal: pooled Welch + round-paired,
# with the endpoint-trim robustness check).
# ---------------------------------------------------------------------------
echo
echo ">>> local analysis ..."
SUMMARY="$LOCAL_DEST/A-B-summary.md"
{
    echo "# Sprint 104 close A/B — $REF_BASE (base) vs $REF_TREAT (treat)"
    echo ""
    echo "Ran: $(date -u).  Lanes: /conn + /upload 256 KiB + ${H2_PROFILES:-<no h2>} (h2c),"
    echo "ROUNDS=$ROUNDS ABBA + null."
    echo ""
} >"$SUMMARY"
for tsv in "$REPO_ROOT"/bench/results/ab-commit-*/raw.tsv \
           "$REPO_ROOT"/bench/results/ab-h2-*/raw.tsv; do
    [ -f "$tsv" ] || continue
    dir="$(dirname "$tsv")"
    # Skip dirs that predate this session (stale from an earlier run).
    if printf '%s\n' "${PRE_EXISTING[@]}" | grep -qx "$dir"; then
        continue
    fi
    echo "=== $tsv ==="
    {
        echo ""
        echo "## $(basename "$(dirname "$tsv")")"
        echo ""
        echo '```'
        uv run python "$REPO_ROOT/bench/peers/ab_report.py" "$tsv"
        echo '```'
        echo ""
        echo '```'
        uv run python "$REPO_ROOT/bench/results/ab_analysis.py" "$tsv"
        echo '```'
    } >>"$SUMMARY"
done
echo "  summary: $SUMMARY"

echo
echo "=== bench/aws/sprint104_ab.sh done ==="
echo "  results pulled: $REPO_ROOT/bench/results/ab-commit-*/"
echo "  summary:        $SUMMARY"
echo "  driver log:     $LOCAL_DEST/driver.log"
echo "  (instance teardown follows via EXIT trap)"

# Completion marker — the chain's terminal proof (chaining-long-running-steps).
echo "sprint104-ab complete $TS base=$REF_BASE treat=$REF_TREAT rounds=$ROUNDS" \
    >"$MARKER"
