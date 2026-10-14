# bench/cloud/remote.sh — run a job on the instance independently of any ssh session.
#
# Sourced after config.sh.  A job's runner lives in its run directory on the
# instance, $REMOTE_REPO/<run>/: tmp/runner.sh, tmp/runner.pid, runner.log and
# `status`, the runner's exit code, written only once it has finished.  The
# runner is detached (new session, no terminal), so a dropped connection can
# never stop a measurement — only lose sight of it for a while.
#
#   remote_launch HOST RUNNER RUN      upload RUNNER, start it detached
#   remote_wait   HOST RUN LIMIT       poll until it finishes or LIMIT seconds pass
#   remote_pull   HOST RUN DIR         copy the run directory back
#   remote_deploy_git HOST             send the local git history; works from a worktree

: "${CLOUD_POLL_INTERVAL:=15}"
#: Consecutive failed polls before the instance counts as unreachable.
: "${CLOUD_SSH_FAILURES:=40}"

remote_launch() {
    local host="$1" runner="$2" run="$REMOTE_REPO/$3"
    ssh -n "${SSH_OPTS[@]}" "$host" "mkdir -p '$run/tmp'" || return 1
    scp "${SSH_OPTS[@]}" "$runner" "$host:$run/tmp/runner.sh" > /dev/null || return 1
    timeout --signal=TERM --kill-after=5s 30s ssh -n "${SSH_OPTS[@]}" "$host" \
        "cd '$run' && rm -f status tmp/runner.pid && \
         (nohup setsid bash -c 'echo \$\$ > tmp/runner.pid; bash tmp/runner.sh > runner.log 2>&1; \
             echo \$? > tmp/status && mv tmp/status status' </dev/null >/dev/null 2>&1 &) && \
         for _ in \$(seq 1 50); do [ -s tmp/runner.pid ] && break; sleep 0.1; done && \
         [ -s tmp/runner.pid ] && echo \"launched pid=\$(cat tmp/runner.pid)\""
}

# Sets REMOTE_STATUS to the runner's exit code, or `lost` when it died without
# one.  Returns 0 finished with 0, 1 finished otherwise, 2 still running at
# LIMIT, 3 unreachable for CLOUD_SSH_FAILURES polls in a row.  Calls the job's
# job_on_poll HOST, when defined, after every successful poll.
remote_wait() {
    local host="$1" run="$REMOTE_REPO/$2" limit="$3" start=$SECONDS failures=0 polls=0 state
    # The status file is checked again after the liveness check: the runner's
    # wrapper writes it and only then exits.
    local probe=": remote-state; cd '$run' || exit 1
        if [ -f status ]; then echo done \$(cat status)
        elif [ -s tmp/runner.pid ] && kill -0 \$(cat tmp/runner.pid) 2>/dev/null; then echo running
        elif [ -f status ]; then echo done \$(cat status)
        else echo lost; fi"
    REMOTE_STATUS=
    while :; do
        polls=$((polls + 1))
        if state="$(ssh -n "${SSH_OPTS[@]}" "$host" "$probe" 2>/dev/null)"; then
            failures=0
            case "$state" in
                'done '*) REMOTE_STATUS="${state#done }"; [ "$REMOTE_STATUS" = 0 ]; return $? ;;
                lost) REMOTE_STATUS=lost; return 1 ;;
            esac
            [ $((polls % 20)) -ne 0 ] || echo "  still running after $((SECONDS - start)) s"
            if declare -F job_on_poll > /dev/null; then job_on_poll "$host"; fi
        else
            failures=$((failures + 1))
            echo "  poll failed ($failures of $CLOUD_SSH_FAILURES in a row allowed)"
            [ "$failures" -lt "$CLOUD_SSH_FAILURES" ] || return 3
        fi
        [ $((SECONDS - start)) -lt "$limit" ] || return 2
        sleep "$CLOUD_POLL_INTERVAL"
    done
}

remote_pull() {
    local host="$1" run="$REMOTE_REPO/$2" dest="$3"
    for _ in 1 2 3; do
        rsync -az -e "ssh ${SSH_OPTS[*]}" "$host:$run/" "$dest/" && return 0
        sleep 5
    done
    return 1
}

# The instance's checkout is an rsync of the working tree; this gives it the
# history behind it.  A bundle of every ref plus HEAD is fetched into a fresh
# repository whose HEAD is the deployed commit, so it works from a git
# worktree, where .git is a file, too.
remote_deploy_git() {
    local host="$1" head bundle
    head="$(git -C "$REPO_ROOT" rev-parse HEAD)" || return 1
    bundle="${BENCH_TMP:-$REPO_ROOT/bench/.state}/repo.bundle"
    mkdir -p "$(dirname "$bundle")"
    git -C "$REPO_ROOT" bundle create "$bundle" --all HEAD 2>/dev/null || return 1
    scp "${SSH_OPTS[@]}" "$bundle" "$host:$REMOTE_REPO.bundle" > /dev/null || return 1
    rm -f "$bundle"
    ssh -n "${SSH_OPTS[@]}" "$host" \
        "cd '$REMOTE_REPO' && rm -rf .git && git init -q && \
         git fetch -q '$REMOTE_REPO.bundle' '+refs/*:refs/bundle/*' && \
         git update-ref --no-deref HEAD $head && git reset -q && rm -f '$REMOTE_REPO.bundle' && \
         git cat-file -e $head^{commit}"
}
