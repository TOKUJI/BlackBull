#!/usr/bin/env bash
# bench/aws/ab.sh — launch and finish an A/B commit comparison on EC2.
#
# The ab-verify counterpart to run.sh (which only drives compare_servers.sh).
# `launch` starts bench/peers/ab_commit.sh detached on the server instance —
# PATH-safe, stdin/stdout detached so the ssh session returns immediately —
# and `finish` polls the raw.tsv results back, scp's them, and tears the
# instance down.  install.sh (run with DEPLOY_GIT=1) provisions uv + .git so
# ab_commit.sh works out of the box.
#
# Usage:
#   REF_BASE=b165880 REF_TREAT=fe83e4c URL_PATH=/conn ROUNDS=8 \
#     bash bench/aws/ab.sh launch
#   URL_PATHS=/conn,/plaintext REF_BASE=.. REF_TREAT=.. bash bench/aws/ab.sh launch
#   bash bench/aws/ab.sh finish          # poll + scp + teardown (run nohup'd locally)
#
# Env (defaults mirror ab_commit.sh; EC2-friendly defaults differ where noted):
#   REF_BASE(=HEAD~1) REF_TREAT(=HEAD) PATHSPEC(=blackbull/)
#   ROUNDS(=8) DURATION(=15) WARMUP(=5) THREADS(=4) CONNS(=32)
#   PORT(=8443) BB_UVLOOP(=0) PIPELINE(=1) PHASES(=null real)
#   SERVER_CPUS(=0-1) LOAD_CPUS(=2-5)
#   WRK_HEADERS (extra wrk header args, e.g. '-H Accept-Encoding:gzip')
#   URL_PATH(=/plaintext) or URL_PATHS (comma-separated — multiple sessions)
#   EXPECT_LINES (raw.tsv completeness per session; default 1+ROUNDS*8)
#   AB_FINISH_LOG (finish progress log; default bench/results/ab-finish.log)
#   AB_POLLS(=300) AB_POLL_INTERVAL(=10) — finish polling budget (~50 min)
set -uo pipefail

source "$(dirname "$0")/config.sh"
_bench_aws_check_env
_bench_aws_load_state

SERVER_REMOTE="$SSH_USER@$SERVER_PUBLIC_IP"
REMOTE_REPO="/home/$SSH_USER/BlackBull"

REF_BASE="${REF_BASE:-HEAD~1}"
REF_TREAT="${REF_TREAT:-HEAD}"
PATHSPEC="${PATHSPEC:-blackbull/}"
ROUNDS="${ROUNDS:-8}"
DURATION="${DURATION:-15}"
WARMUP="${WARMUP:-5}"
THREADS="${THREADS:-4}"
CONNS="${CONNS:-32}"
PORT="${PORT:-8443}"
BB_UVLOOP="${BB_UVLOOP:-0}"
PIPELINE="${PIPELINE:-1}"
PHASES="${PHASES:-null real}"
SERVER_CPUS="${SERVER_CPUS:-0-1}"
LOAD_CPUS="${LOAD_CPUS:-2-5}"
URL_PATH="${URL_PATH:-/plaintext}"
URL_PATHS="${URL_PATHS:-}"
PEER_MW="${PEER_MW:-}"
WRK_HEADERS="${WRK_HEADERS:-}"
EXPECT_LINES="${EXPECT_LINES:-$((1 + ROUNDS * 8))}"
AB_FINISH_LOG="${AB_FINISH_LOG:-$REPO_ROOT/bench/results/ab-finish.log}"
AB_POLLS="${AB_POLLS:-300}"
AB_POLL_INTERVAL="${AB_POLL_INTERVAL:-10}"

MODE="${1:-launch}"

# --- build the env prefix for one ab_commit.sh invocation ------------------
ab_env() {  # $1 = url
    printf "REF_BASE='%s' REF_TREAT='%s' PATHSPEC='%s' URL_PATH='%s' ROUNDS='%s' " \
        "$REF_BASE" "$REF_TREAT" "$PATHSPEC" "$1" "$ROUNDS"
    printf "DURATION='%s' WARMUP='%s' THREADS='%s' CONNS='%s' PORT='%s' BB_UVLOOP='%s' " \
        "$DURATION" "$WARMUP" "$THREADS" "$CONNS" "$PORT" "$BB_UVLOOP"
    printf "PIPELINE='%s' PHASES='%s' SERVER_CPUS='%s' LOAD_CPUS='%s' " \
        "$PIPELINE" "$PHASES" "$SERVER_CPUS" "$LOAD_CPUS"
    printf "PEER_MW='%s' " "$PEER_MW"
    printf "WRK_HEADERS='%s' " "$WRK_HEADERS"
}

case "$MODE" in
launch)
    if [ -n "$URL_PATHS" ]; then
        IFS=',' read -r -a URLS <<< "$URL_PATHS"
    else
        URLS=("$URL_PATH")
    fi

    # Preflight: uv on PATH (install.sh symlinks it) and both refs present,
    # so ab_commit.sh's git-checkout swap can actually run.
    if ! ssh "${SSH_OPTS[@]}" "$SERVER_REMOTE" \
        "command -v uv >/dev/null 2>&1 && cd $REMOTE_REPO && \
         git cat-file -e $REF_BASE^{commit} && git cat-file -e $REF_TREAT^{commit}" 2>/dev/null; then
        echo "bench/aws/ab.sh: preflight failed — uv or git refs missing." >&2
        echo "  run: DEPLOY_GIT=1 bash bench/aws/install.sh   (installs uv, deploys .git)" >&2
        exit 1
    fi

    # Build the runner script locally (avoids nested-quote hell through ssh),
    # scp it up, then launch it fully detached so the ssh call returns.
    RUNNER="$(mktemp)"
    trap 'rm -f "$RUNNER"' EXIT
    {
        echo '#!/usr/bin/env bash'
        echo 'set -uo pipefail'
        printf 'cd %q\n' "$REMOTE_REPO"
        for u in "${URLS[@]}"; do
            # The URL path (e.g. /static/static_ab.js) becomes part of the
            # log filename; a nested slash must not, or the shell redirect
            # fails on the missing directory and the runner dies instantly.
            log="bench/results/ec2-ab-$(printf '%s' "${u#/}" | tr '/' '_').log"
            printf 'env %s bash bench/peers/ab_commit.sh > %q 2>&1\n' "$(ab_env "$u")" "$log"
        done
    } > "$RUNNER"

    scp "${SSH_OPTS[@]}" "$RUNNER" "$SERVER_REMOTE:$REMOTE_REPO/bench/results/ab_runner.sh" \
        >/dev/null 2>&1
    ssh "${SSH_OPTS[@]}" "$SERVER_REMOTE" \
        "cd $REMOTE_REPO && rm -rf bench/results/ab-commit-* && \
         chmod +x bench/results/ab_runner.sh && \
         nohup bash bench/results/ab_runner.sh </dev/null >/dev/null 2>&1 & echo launched"
    echo "ab_commit.sh launched on $SERVER_REMOTE"
    echo "  profiles : ${URLS[*]}"
    echo "  base     : $REF_BASE   treat: $REF_TREAT"
    echo "  rounds   : $ROUNDS   duration: ${DURATION}s   phases: $PHASES"
    echo "  runner log per profile: bench/results/ec2-ab-*.log (on instance)"
    echo "  finish later with: bash bench/aws/ab.sh finish"
    ;;

finish)
    {
        echo "ab.sh finish start: $(date -u)"
        runner=-1; complete=0
        for i in $(seq 1 "$AB_POLLS"); do
            state=$(ssh "${SSH_OPTS[@]}" "$SERVER_REMOTE" \
                "cd $REMOTE_REPO && n=\$(pgrep -f 'bench/results/ab_runner.sh' | wc -l); \
                 c=0; for f in bench/results/ab-commit-*/raw.tsv; do [ -f \"\$f\" ] && \
                 [ \"\$(wc -l < \"\$f\")\" -ge $EXPECT_LINES ] && c=\$((c+1)); done; echo \"\$n \$c\"" \
                2>/dev/null)
            runner="${state%% *}"
            complete="${state##* }"
            [ "$runner" = "0" ] && [ "$complete" -ge 1 ] && break
            sleep "$AB_POLL_INTERVAL"
        done
        echo "poll done: runner_procs=${runner:-?} complete_rawtsv=${complete:-?} at $(date -u)"

        echo "pulling results ..."
        for d in $(ssh "${SSH_OPTS[@]}" "$SERVER_REMOTE" \
            "cd $REMOTE_REPO && ls -d bench/results/ab-commit-* 2>/dev/null"); do
            scp "${SSH_OPTS[@]}" -r "$SERVER_REMOTE:$REMOTE_REPO/$d" "$REPO_ROOT/bench/results/" \
                && echo "scp OK: $d" || echo "SCP FAILED: $d"
        done
        for f in "$REPO_ROOT"/bench/results/ab-commit-*/raw.tsv; do
            [ -f "$f" ] && echo "raw.tsv lines: $f -> $(wc -l < "$f")"
        done

        echo "tearing down: $(date -u)"
        bash "$(dirname "$0")/down.sh" 2>&1 | tail -4
        echo "AB FINISH COMPLETE: $(date -u)"
    } >> "$AB_FINISH_LOG" 2>&1
    echo "finish running in background; progress -> $AB_FINISH_LOG"
    ;;

*)
    echo "usage: bash bench/aws/ab.sh [launch|finish]" >&2
    exit 1
    ;;
esac
