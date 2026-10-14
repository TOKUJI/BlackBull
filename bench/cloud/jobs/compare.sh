# bench/cloud/jobs/compare.sh — the peer-server comparison on EC2.
#
# Runs bench/compare/compare_servers.sh into the session's run directory.
# TOPO=single: on the one instance.  TOPO=split: on the load generator, which
# starts and stops each server on the server instance over ssh.
#
# Env (defaults): RUNS(=5) LANES(=A B-wrk B-oha C D)
#   STACKS(=blackbull uvicorn hypercorn granian daphne) DURATION(=30)
#   BB_UVLOOP(=1) and compare_servers.sh's instrument toggles, passed through:
#   BB_GC_STATS BB_LOOP_STAMP BB_RESP_TIMING BB_TIMING_SNAP BB_HANDLER_TIMING
#   BB_PARSE_TIMING BB_DISPATCH_TIMING BB_READ_TIMING BB_GATE_STAMP BB_NULL_SEAM
#   BB_NO_EVENTS BB_BENCH_TASKSET CALIBRATE(=1) CALIBRATE_RUNS(=3)
#   CLOUD_WAIT_SECONDS(=10800) — the matrix's length depends on the stacks and lanes

COMPARE_KNOBS=(RUNS LANES STACKS DURATION BB_UVLOOP BB_GC_STATS BB_LOOP_STAMP BB_RESP_TIMING
               BB_TIMING_SNAP BB_HANDLER_TIMING BB_PARSE_TIMING BB_DISPATCH_TIMING BB_READ_TIMING
               BB_GATE_STAMP BB_NULL_SEAM BB_NO_EVENTS BB_BENCH_TASKSET CALIBRATE CALIBRATE_RUNS)

job_preflight() {
    : "${RUNS:=5}" "${LANES:=A B-wrk B-oha C D}" "${DURATION:=30}" "${BB_UVLOOP:=1}"
    : "${STACKS:=blackbull uvicorn hypercorn granian daphne}" "${CALIBRATE:=1}" "${CALIBRATE_RUNS:=3}"
    JOB_WAIT_SECONDS=10800
    JOB_DEPLOY_GIT=0
    [ "${TOPO:-single}" = split ] && JOB_HOST=loadgen
    JOB_SETTINGS=()
    local knob
    for knob in "${COMPARE_KNOBS[@]}"; do JOB_SETTINGS+=("${knob,,}=${!knob:-}"); done
}

job_runner() {
    local knob
    echo 'set -uo pipefail'
    printf 'cd %q\n' "$REMOTE_REPO"
    echo 'source .venv/bin/activate'
    for knob in "${COMPARE_KNOBS[@]}"; do printf 'export %s=%q\n' "$knob" "${!knob:-}"; done
    if [ "${TOPO:-single}" = split ]; then
        # The load generator drives the server with its copy of the bench key
        # (install.sh puts it in ~/.ssh/server.pem); the target is a private
        # VPC address reachable only through the security group.
        printf 'export BENCH_TARGET_HOST=%q BENCH_REMOTE_LIFECYCLE=1 BENCH_REMOTE_REPO=%q BENCH_BIND_HOST=0.0.0.0\n' \
            "$SERVER_DNS_NAME" "$REMOTE_REPO"
        printf 'export BENCH_REMOTE_SSH=%q\n' \
            "ssh -i /home/$SSH_USER/.ssh/server.pem -o StrictHostKeyChecking=no -o UserKnownHostsFile=/home/$SSH_USER/.ssh/server_known_hosts -o ConnectTimeout=10 -o ServerAliveInterval=30 $SSH_USER@$SERVER_PRIVATE_IP"
    fi
    printf 'BENCH_RUN_DIR=%q bash bench/compare/compare_servers.sh\n' "$REMOTE_RUN"
}

job_collect() { [ -s "$LOCAL_RUN/report.md" ]; }
