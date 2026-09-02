#!/usr/bin/env bash
# bench/aws/ab.sh — launch and finish an A/B commit comparison on EC2.
#
# The ab-verify counterpart to run.sh (which only drives compare_servers.sh).
# `launch` starts bench/peers/ab_commit.sh detached on the server instance —
# PATH-safe, in a new session with stdin/stdout/stderr detached so the ssh
# session returns immediately — and `finish` polls the raw.tsv results back,
# scp's them, and tears the instance down.  install.sh (run with
# DEPLOY_GIT=1) provisions uv + .git so ab_commit.sh works out of the box.
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
#   PORT(=8443) BB_UVLOOP(=0) BB_FORCE_ASGI_SCOPE(=0) PIPELINE(=1) PHASES(=null real)
#   SERVER_CPUS(=0-1) LOAD_CPUS(=2-5)
#   WRK_HEADERS (extra wrk header args, e.g. '-H Accept-Encoding:gzip')
#   URL_PATH(=/plaintext) or URL_PATHS (comma-separated — multiple sessions)
#   WRK_SCRIPT / WRK_SCRIPT_ARGS (applied to every URL) — or per-URL forms
#   WRK_SCRIPTS / WRK_SCRIPT_ARGSS (comma-separated, parallel to URL_PATHS;
#   empty entries fall back to the single values)
#   H2_PROFILES (comma-separated h2load URL paths — each runs ab_commit_h2.sh
#   after the H1 lanes; '' = no H2 lane) with H2_CONNS/H2_STREAMS/H2_N/H2_WARMUP
#   EXPECT_LINES (raw.tsv completeness per session; default
#                1+ROUNDS*4*number-of-PHASES)
#   AB_FINISH_LOG (finish progress log; default bench/results/ab-finish.log)
#   AB_LAUNCH_TIMEOUT(=30) — maximum seconds for the remote launch handshake
#   AB_POLLS(=300) AB_POLL_INTERVAL(=10) — finish polling budget (~50 min)
set -uo pipefail

source "$(dirname "$0")/config.sh"
_bench_aws_check_env
_bench_aws_load_state

SERVER_REMOTE="$SSH_USER@$SERVER_PUBLIC_IP"
REMOTE_REPO="/home/$SSH_USER/BlackBull"
REMOTE_EXPECTED_LINES="bench/results/ab_expected_lines"
REMOTE_EXPECTED_LINES_TMP="bench/results/ab_expected_lines.tmp"
REMOTE_EXPECTED_RESULTS="bench/results/ab_expected_results"
REMOTE_EXPECTED_RESULTS_TMP="bench/results/ab_expected_results.tmp"
REMOTE_RUNNER_STATUS="bench/results/ab_runner.status"
REMOTE_RUNNER_STATUS_TMP="bench/results/ab_runner.status.tmp"
REMOTE_RUNNER_STATUS_REQUIRED="bench/results/ab_runner.status.required"

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
BB_FORCE_ASGI_SCOPE="${BB_FORCE_ASGI_SCOPE:-0}"
PIPELINE="${PIPELINE:-1}"
PHASES="${PHASES:-null real}"
SERVER_CPUS="${SERVER_CPUS:-0-1}"
LOAD_CPUS="${LOAD_CPUS:-2-5}"
URL_PATH="${URL_PATH:-/plaintext}"
URL_PATHS="${URL_PATHS:-}"
PEER_MW="${PEER_MW:-}"
WRK_HEADERS="${WRK_HEADERS:-}"
WRK_SCRIPT="${WRK_SCRIPT:-}"
WRK_SCRIPT_ARGS="${WRK_SCRIPT_ARGS:-}"
#: Per-URL forms, parallel to URL_PATHS.  An empty entry falls back to the
#: single WRK_SCRIPT / WRK_SCRIPT_ARGS above (which default to '' = no script).
WRK_SCRIPTS="${WRK_SCRIPTS:-}"
WRK_SCRIPT_ARGSS="${WRK_SCRIPT_ARGSS:-}"
#: H2_PROFILES — comma-separated h2load URL paths, each run as its own
#: ab_commit_h2.sh session after the H1 lanes (default '' = no H2 lane).
#: The H2 knobs below pass through to ab_commit_h2.sh.  The h2 raw.tsv has
#: the same shape as the H1 one (header + ROUNDS*4*number-of-PHASES rows), so
#: EXPECT_LINES and the finish poll cover both.
H2_PROFILES="${H2_PROFILES:-}"
H2_CONNS="${H2_CONNS:-32}"
H2_STREAMS="${H2_STREAMS:-16}"
H2_N="${H2_N:-100000}"
H2_WARMUP="${H2_WARMUP:-10000}"
EXPECT_LINES_EXPLICIT=0
[ -n "${EXPECT_LINES:-}" ] && EXPECT_LINES_EXPLICIT=1
# Each phase contributes four rows per round (base, treat, treat, base), plus
# the raw.tsv header.  Derive the default from the actual phase list so a
# single-phase run cannot inherit the two-phase threshold.
if ! [[ "$ROUNDS" =~ ^[1-9][0-9]*$ ]]; then
    echo "bench/aws/ab.sh: ROUNDS must be a positive integer." >&2
    exit 1
fi
PHASES_NORMALIZED="${PHASES//$'\r'/ }"
PHASES_NORMALIZED="${PHASES_NORMALIZED//$'\n'/ }"
PHASES_NORMALIZED="${PHASES_NORMALIZED//$'\t'/ }"
read -r -a PHASE_LIST <<< "$PHASES_NORMALIZED"
PHASE_COUNT="${#PHASE_LIST[@]}"
if [ "$PHASE_COUNT" -eq 0 ]; then
    echo "bench/aws/ab.sh: PHASES must select at least one phase." >&2
    exit 1
fi
for phase in "${PHASE_LIST[@]}"; do
    if [ "$phase" != "null" ] && [ "$phase" != "real" ]; then
        echo "bench/aws/ab.sh: PHASES must contain only null and real." >&2
        exit 1
    fi
done
EXPECT_LINES="${EXPECT_LINES:-$((1 + ROUNDS * 4 * PHASE_COUNT))}"
AB_FINISH_LOG="${AB_FINISH_LOG:-$REPO_ROOT/bench/results/ab-finish.log}"
AB_LAUNCH_TIMEOUT="${AB_LAUNCH_TIMEOUT:-30}"
AB_POLLS="${AB_POLLS:-300}"
AB_POLL_INTERVAL="${AB_POLL_INTERVAL:-10}"

MODE="${1:-launch}"

if ! [[ "$EXPECT_LINES" =~ ^[1-9][0-9]*$ ]]; then
    echo "bench/aws/ab.sh: EXPECT_LINES must be a positive integer." >&2
    exit 1
fi

# --- build the env prefix for one ab_commit.sh invocation ------------------
ab_env() {  # $1 = url, $2 = wrk script ('' = none), $3 = wrk script args
    printf "REF_BASE='%s' REF_TREAT='%s' PATHSPEC='%s' URL_PATH='%s' ROUNDS='%s' " \
        "$REF_BASE" "$REF_TREAT" "$PATHSPEC" "$1" "$ROUNDS"
    printf "DURATION='%s' WARMUP='%s' THREADS='%s' CONNS='%s' PORT='%s' BB_UVLOOP='%s' BB_FORCE_ASGI_SCOPE='%s' " \
        "$DURATION" "$WARMUP" "$THREADS" "$CONNS" "$PORT" "$BB_UVLOOP" "$BB_FORCE_ASGI_SCOPE"
    printf "PIPELINE='%s' PHASES='%s' SERVER_CPUS='%s' LOAD_CPUS='%s' " \
        "$PIPELINE" "$PHASES" "$SERVER_CPUS" "$LOAD_CPUS"
    printf "PEER_MW='%s' " "$PEER_MW"
    printf "WRK_HEADERS='%s' " "$WRK_HEADERS"
    printf "WRK_SCRIPT='%s' WRK_SCRIPT_ARGS='%s' " "$2" "$3"
}

case "$MODE" in
launch)
    if [ -n "$URL_PATHS" ]; then
        if [[ ",$URL_PATHS," == *",,"* ]]; then
            echo "bench/aws/ab.sh: URL_PATHS must contain only non-empty paths." >&2
            exit 1
        fi
        IFS=',' read -r -a URLS <<< "$URL_PATHS"
    else
        URLS=("$URL_PATH")
    fi
    if [ -n "$H2_PROFILES" ]; then
        if [[ ",$H2_PROFILES," == *",,"* ]]; then
            echo "bench/aws/ab.sh: H2_PROFILES must contain only non-empty paths." >&2
            exit 1
        fi
        IFS=',' read -r -a H2URLS <<< "$H2_PROFILES"
    else
        H2URLS=()
    fi
    EXPECTED_RESULTS=$((${#URLS[@]} + ${#H2URLS[@]}))
    # Per-URL wrk script/args, parallel to URL_PATHS; an empty entry falls
    # back to the single WRK_SCRIPT / WRK_SCRIPT_ARGS (so the two forms mix).
    if [ -n "$WRK_SCRIPTS" ]; then
        IFS=',' read -r -a SCRIPTS <<< "$WRK_SCRIPTS"
    else
        SCRIPTS=()
    fi
    if [ -n "$WRK_SCRIPT_ARGSS" ]; then
        IFS=',' read -r -a ARGSS <<< "$WRK_SCRIPT_ARGSS"
    else
        ARGSS=()
    fi

    # Preflight: uv on PATH (install.sh symlinks it) and both refs present,
    # so ab_commit.sh's git-checkout swap can actually run.
    if ! ssh -n "${SSH_OPTS[@]}" "$SERVER_REMOTE" \
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
        echo 'set -euo pipefail'
        printf 'cd %q\n' "$REMOTE_REPO"
        for i in "${!URLS[@]}"; do
            u="${URLS[$i]}"
            # The URL path (e.g. /static/static_ab.js) becomes part of the
            # log filename; a nested slash must not, or the shell redirect
            # fails on the missing directory and the runner dies instantly.
            log="bench/results/ec2-ab-$(printf '%s' "${u#/}" | tr '/' '_').log"
            printf 'env %s bash bench/peers/ab_commit.sh > %q 2>&1\n' \
                "$(ab_env "$u" "${SCRIPTS[$i]:-$WRK_SCRIPT}" "${ARGSS[$i]:-$WRK_SCRIPT_ARGS}")" "$log"
        done
        # H2 lanes run after the H1 lanes, same session, same refs.  ab_env
        # already emits the vars ab_commit_h2.sh shares (REF_*, PATHSPEC,
        # URL_PATH, ROUNDS, PORT, BB_*, PHASES, pinning); the h2load knobs
        # are appended here.
        for u in "${H2URLS[@]}"; do
            log="bench/results/ec2-ab-h2-$(printf '%s' "${u#/}" | tr '/' '_').log"
            printf 'env %s H2_CONNS=%q H2_STREAMS=%q H2_N=%q H2_WARMUP=%q bash bench/peers/ab_commit_h2.sh > %q 2>&1\n' \
                "$(ab_env "$u" '' '')" \
                "$H2_CONNS" "$H2_STREAMS" "$H2_N" "$H2_WARMUP" "$log"
        done
    } > "$RUNNER"

    if ! scp "${SSH_OPTS[@]}" "$RUNNER" \
        "$SERVER_REMOTE:$REMOTE_REPO/bench/results/ab_runner.sh" \
        >/dev/null 2>&1; then
        echo "bench/aws/ab.sh: runner upload failed." >&2
        exit 1
    fi
    if ! timeout --signal=TERM --kill-after=5s "${AB_LAUNCH_TIMEOUT}s" \
        ssh -n "${SSH_OPTS[@]}" "$SERVER_REMOTE" \
        "cd $REMOTE_REPO && rm -rf bench/results/ab-commit-* bench/results/ab-h2-* && \
         rm -f $REMOTE_RUNNER_STATUS_REQUIRED $REMOTE_RUNNER_STATUS \
               $REMOTE_RUNNER_STATUS_TMP $REMOTE_EXPECTED_LINES_TMP \
               $REMOTE_EXPECTED_RESULTS_TMP && \
         printf '%s\\n' '$EXPECT_LINES' > $REMOTE_EXPECTED_LINES_TMP && \
         printf '%s\\n' '$EXPECTED_RESULTS' > $REMOTE_EXPECTED_RESULTS_TMP && \
         mv -f $REMOTE_EXPECTED_LINES_TMP $REMOTE_EXPECTED_LINES && \
         mv -f $REMOTE_EXPECTED_RESULTS_TMP $REMOTE_EXPECTED_RESULTS && \
         : > $REMOTE_RUNNER_STATUS_REQUIRED && \
         chmod +x bench/results/ab_runner.sh && \
         rm -f bench/results/ab_runner.pid && \
         (nohup setsid bash -c 'echo \$\$ > bench/results/ab_runner.pid; \
          bash bench/results/ab_runner.sh; rc=\$?; \
          printf \"%s\\n\" \"\$rc\" > $REMOTE_RUNNER_STATUS_TMP && \
          mv -f $REMOTE_RUNNER_STATUS_TMP $REMOTE_RUNNER_STATUS; exit \"\$rc\"' \
          </dev/null >/dev/null 2>&1 & \
          launcher_pid=\$!; pid=; alive=0; \
          for _ in 1 2 3 4 5; do \
              if [ -s bench/results/ab_runner.pid ]; then \
                  pid=\$(cat bench/results/ab_runner.pid); \
                  if kill -0 \"\$pid\" 2>/dev/null; then alive=1; break; fi; \
              fi; \
              sleep 0.1; \
          done; \
          if [ \"\$alive\" -ne 1 ]; then \
              kill \"\$launcher_pid\" 2>/dev/null || true; \
              echo 'remote A/B runner exited during launch' >&2; exit 1; \
          fi; \
          echo launched pid=\$pid)"; then
        echo "bench/aws/ab.sh: remote runner launch failed." >&2
        exit 1
    fi
    echo "ab_commit.sh launched on $SERVER_REMOTE"
    echo "  profiles : ${URLS[*]}${H2_PROFILES:+  h2: ${H2_PROFILES//,/, }}"
    echo "  base     : $REF_BASE   treat: $REF_TREAT"
    echo "  rounds   : $ROUNDS   duration: ${DURATION}s   phases: $PHASES"
    echo "  runner log per profile: bench/results/ec2-ab-*.log (on instance)"
    echo "  finish later with: bash bench/aws/ab.sh finish"
    ;;

finish)
    if ! (
        echo "ab.sh finish start: $(date -u)"
        # The marker is committed only after both launch-time sidecars.  Its
        # presence therefore selects the current protocol; partial mixtures
        # fail closed instead of being mistaken for a legacy run.  An explicit
        # EXPECT_LINES overrides only the line threshold, never this handshake
        # or the launch-declared number of result lanes.
        if ! launch_metadata=$(ssh -n "${SSH_OPTS[@]}" "$SERVER_REMOTE" \
            "cd $REMOTE_REPO && r=0; [ -e $REMOTE_RUNNER_STATUS_REQUIRED ] && r=1; \
             l=missing; if [ -e $REMOTE_EXPECTED_LINES ]; then l=invalid; \
                 if [ -f $REMOTE_EXPECTED_LINES ]; then v=\$(cat $REMOTE_EXPECTED_LINES) || exit 1; \
                     case \"\$v\" in ''|*[!0-9]*) ;; *) l=value:\$v;; esac; fi; fi; \
             q=missing; if [ -e $REMOTE_EXPECTED_RESULTS ]; then q=invalid; \
                 if [ -f $REMOTE_EXPECTED_RESULTS ]; then v=\$(cat $REMOTE_EXPECTED_RESULTS) || exit 1; \
                     case \"\$v\" in ''|*[!0-9]*) ;; *) q=value:\$v;; esac; fi; fi; \
             printf '%s %s %s\\n' \"\$r\" \"\$l\" \"\$q\"" \
            2>/dev/null); then
            echo "bench/aws/ab.sh: failed to read launch metadata." >&2
            exit 1
        fi
        launch_metadata_required=; launch_expected=; launch_results=; metadata_extra=;
        read -r launch_metadata_required launch_expected launch_results metadata_extra \
            <<< "$launch_metadata"
        if ! [[ "$launch_metadata_required" =~ ^[01]$ ]] || \
           [ -n "$metadata_extra" ]; then
            echo "bench/aws/ab.sh: invalid launch metadata protocol state." >&2
            exit 1
        fi
        if [ "$launch_metadata_required" = "1" ]; then
            case "$launch_expected" in
                value:*) launch_expected="${launch_expected#value:}" ;;
                missing)
                    echo "bench/aws/ab.sh: launch metadata is missing for a current-format run." >&2
                    exit 1
                    ;;
                *)
                    echo "bench/aws/ab.sh: invalid launch metadata in $REMOTE_EXPECTED_LINES." >&2
                    exit 1
                    ;;
            esac
            case "$launch_results" in
                value:*) EXPECTED_RESULTS="${launch_results#value:}" ;;
                missing)
                    echo "bench/aws/ab.sh: launch metadata is missing for a current-format run." >&2
                    exit 1
                    ;;
                *)
                    echo "bench/aws/ab.sh: invalid launch metadata in $REMOTE_EXPECTED_RESULTS." >&2
                    exit 1
                    ;;
            esac
            if ! [[ "$launch_expected" =~ ^[1-9][0-9]*$ ]]; then
                echo "bench/aws/ab.sh: invalid launch metadata in $REMOTE_EXPECTED_LINES." >&2
                exit 1
            fi
            if ! [[ "$EXPECTED_RESULTS" =~ ^[1-9][0-9]*$ ]]; then
                echo "bench/aws/ab.sh: invalid launch metadata in $REMOTE_EXPECTED_RESULTS." >&2
                exit 1
            fi
            [ "$EXPECT_LINES_EXPLICIT" = "1" ] || EXPECT_LINES="$launch_expected"
            run_format=current
        elif [ "$launch_expected" = "missing" ] && [ "$launch_results" = "missing" ]; then
            run_format=legacy
            EXPECTED_RESULTS=0
        else
            # Observe one runner state before rejecting this partial protocol.
            # This keeps terminal-state diagnostics deterministic while still
            # preventing any result copy or teardown.
            run_format=inconsistent
            EXPECTED_RESULTS=0
        fi
        echo "expected raw.tsv lines: $EXPECT_LINES"
        runner=-1; total=0; complete=0; results_complete=0
        status_required=0
        runner_status=missing
        # Liveness comes from this run's own pidfile, not from a pattern
        # match over the process table.  `pgrep -f 'ab_runner[.]sh'` matched
        # host-globally, so a second A/B job on the same box — or, in the unit
        # tests, a second test — was counted as this run's runner and the
        # finish waited out its whole poll budget for a runner that had
        # already exited.  The pidfile is per checkout, which is the scope the
        # question is actually about, and it is the same check the launch
        # above already uses to confirm the runner came up.
        #
        # `: ab-poll` is a no-op that names this command.  The launch command
        # also mentions ab_runner.pid, so a test double cannot tell the two
        # apart by their contents without guessing at an incidental substring
        # — which is how the double came to key on `pgrep -f` and break the
        # moment that went away.
        for i in $(seq 1 "$AB_POLLS"); do
            if ! state=$(ssh "${SSH_OPTS[@]}" "$SERVER_REMOTE" \
                "cd $REMOTE_REPO && : ab-poll; n=0; \
                 if [ -s bench/results/ab_runner.pid ]; then \
                 p=\$(cat bench/results/ab_runner.pid); \
                 kill -0 \"\$p\" 2>/dev/null && n=1; fi; \
                 t=0; c=0; for d in bench/results/ab-commit-* bench/results/ab-h2-*; do \
                 [ -d \"\$d\" ] || continue; t=\$((t+1)); f=\"\$d/raw.tsv\"; \
                 [ -f \"\$f\" ] && [ \"\$(wc -l < \"\$f\")\" -ge $EXPECT_LINES ] && c=\$((c+1)); done; \
                 r=0; [ -e $REMOTE_RUNNER_STATUS_REQUIRED ] && r=1; \
                 s=missing; [ -f $REMOTE_RUNNER_STATUS ] && s=\$(cat $REMOTE_RUNNER_STATUS); \
                 echo \"\$n \$t \$c \$r \$s\"" \
                2>/dev/null); then
                echo "bench/aws/ab.sh: failed to poll remote runner state." >&2
                exit 1
            fi
            state_extra=;
            read -r runner total complete status_required runner_status state_extra <<< "$state"
            if ! [[ "$runner" =~ ^[0-9]+$ && "$total" =~ ^[0-9]+$ && \
                    "$complete" =~ ^[0-9]+$ && "$status_required" =~ ^[01]$ ]] || \
               [ -n "$state_extra" ]; then
                echo "bench/aws/ab.sh: invalid remote runner state: $state" >&2
                exit 1
            fi
            case "$runner_status" in
                missing|[0-9]|[1-9][0-9]*) ;;
                *)
                    echo "bench/aws/ab.sh: invalid remote runner status: $runner_status" >&2
                    exit 1
                    ;;
            esac
            results_complete=0
            if [ "$run_format" = "current" ]; then
                if [ "$total" -eq "$EXPECTED_RESULTS" ] && \
                   [ "$complete" -eq "$EXPECTED_RESULTS" ]; then
                    results_complete=1
                fi
            elif [ "$total" -ge 1 ] && [ "$complete" -eq "$total" ]; then
                results_complete=1
            fi
            if [ "$runner" = "0" ]; then
                if [ "$run_format" = "inconsistent" ]; then
                    echo "bench/aws/ab.sh: launch metadata protocol marker is missing." >&2
                    exit 1
                elif [ "$run_format" = "current" ]; then
                    if [ "$status_required" != "1" ]; then
                        echo "bench/aws/ab.sh: current-format status protocol marker is missing." >&2
                        exit 1
                    fi
                    if [ "$runner_status" = "missing" ]; then
                        echo "bench/aws/ab.sh: remote A/B runner success marker is missing." >&2
                        exit 1
                    fi
                elif [ "$status_required" != "0" ] || [ "$runner_status" != "missing" ]; then
                    echo "bench/aws/ab.sh: legacy run has an inconsistent status protocol marker." >&2
                    exit 1
                fi
                if [ "$runner_status" != "missing" ] && [ "$runner_status" != "0" ]; then
                    echo "bench/aws/ab.sh: remote A/B runner failed (status=$runner_status)." >&2
                    exit 1
                fi
                if [ "$results_complete" = "1" ]; then break; fi
                echo "bench/aws/ab.sh: not all configured A/B results are complete." >&2
                exit 1
            fi
            sleep "$AB_POLL_INTERVAL"
        done
        echo "poll done: runner_procs=${runner:-?} result_dirs=${total:-?} complete_rawtsv=${complete:-?} at $(date -u)"

        if [ "$runner" != "0" ]; then
            echo "bench/aws/ab.sh: remote A/B runner did not finish within the poll budget." >&2
            exit 1
        fi
        if [ "$results_complete" != "1" ]; then
            echo "bench/aws/ab.sh: not all configured A/B results are complete." >&2
            exit 1
        fi

        echo "pulling results ..."
        if ! result_dirs=$(ssh "${SSH_OPTS[@]}" "$SERVER_REMOTE" \
            "cd $REMOTE_REPO && find bench/results -maxdepth 1 -type d \\
             \( -name 'ab-commit-*' -o -name 'ab-h2-*' \) -print"); then
            echo "bench/aws/ab.sh: failed to list remote A/B results." >&2
            exit 1
        fi
        if [ -z "$result_dirs" ]; then
            echo "bench/aws/ab.sh: no remote A/B result directories found." >&2
            exit 1
        fi
        for d in $result_dirs; do
            if ! scp "${SSH_OPTS[@]}" -r "$SERVER_REMOTE:$REMOTE_REPO/$d" "$REPO_ROOT/bench/results/"; then
                echo "bench/aws/ab.sh: failed to copy A/B result: $d" >&2
                exit 1
            fi
            echo "scp OK: $d"
        done
        for f in "$REPO_ROOT"/bench/results/ab-commit-*/raw.tsv \
                 "$REPO_ROOT"/bench/results/ab-h2-*/raw.tsv; do
            [ -f "$f" ] && echo "raw.tsv lines: $f -> $(wc -l < "$f")"
        done

        # TEARDOWN=0 leaves the instance up so a later stage (e.g. Sprint 100
        # Phase 1's compare_servers.sh run) can reuse the same box.  The
        # instance's own fail-safes (terminate-on-shutdown + scheduled
        # self-shutdown) remain the backstop; down.sh is just skipped here.
        if [ "${TEARDOWN:-1}" = "1" ]; then
            echo "tearing down: $(date -u)"
            if ! bash "$(dirname "$0")/down.sh" 2>&1 | tail -4; then
                echo "bench/aws/ab.sh: teardown failed; instance was left running." >&2
                exit 1
            fi
        else
            echo "teardown skipped (TEARDOWN=0) — instance left up for chained runs"
        fi
        echo "AB FINISH COMPLETE: $(date -u)"
    ) >> "$AB_FINISH_LOG" 2>&1; then
        echo "bench/aws/ab.sh: finish failed; details -> $AB_FINISH_LOG" >&2
        exit 1
    fi
    echo "finish running in background; progress -> $AB_FINISH_LOG"
    ;;

*)
    echo "usage: bash bench/aws/ab.sh [launch|finish]" >&2
    exit 1
    ;;
esac
