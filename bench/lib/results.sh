# bench/lib/results.sh — source it, then call `bench_run_dir <tool> [key=value ...]`.
#
# Sets and exports BENCH_RUN_DIR (the run's output directory, see results.py),
# BENCH_TMP (its tmp/) and TMPDIR=BENCH_TMP.  An already-set BENCH_RUN_DIR is
# joined, or created when it does not exist yet.
bench_run_dir() {
    BENCH_RUN_DIR="$(python3 "${BASH_SOURCE[0]%/*}/results.py" "$@" -- "$0")" || return
    BENCH_TMP="$BENCH_RUN_DIR/tmp"
    TMPDIR="$BENCH_TMP"
    export BENCH_RUN_DIR BENCH_TMP TMPDIR
}

# Print the newest run directory of <tool>, or nothing.
bench_latest_run() {
    python3 "${BASH_SOURCE[0]%/*}/results.py" --latest "$1"
}
