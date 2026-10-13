# bench/cloud/jobs/ab.sh — A/B of two commits on EC2 (`just ab-verify`).
#
# Runs bench/ab/ab_commit.sh once per HTTP/1.1 lane and ab_commit_h2.sh once
# per HTTP/2 lane, each into its own directory of the session's run, all in one
# session on one instance.  The defaults are the EC2 recipe of
# bench/ab/AB-HIGH-PRECISION.md; nothing needs setting except the refs.
#
# Env (defaults):
#   REF_BASE(=HEAD~1) REF_TREAT(=HEAD)  resolved to commits here.  REF_TREAT must
#       be the working tree under PATHSPEC: that tree is what gets deployed.
#   PATHSPEC(=blackbull/) ROUNDS(=8) PHASES(=null real) PORT(=8443)
#   DURATION(=15) WARMUP(=5) THREADS(=4) CONNS(=32) PIPELINE(=1)
#   BB_UVLOOP(=0) BB_FORCE_ASGI_SCOPE(=0) SERVER_CPUS(=0-1) LOAD_CPUS(=2-5)
#   BASE_EXTRA_ENV TREAT_EXTRA_ENV  identical refs: compare two environments
#   URL_PATHS(=/plaintext)  comma-separated HTTP/1.1 lanes
#   WRK_HEADERS WRK_SCRIPT WRK_SCRIPT_ARGS  for every HTTP/1.1 lane, or
#   WRK_SCRIPTS WRK_SCRIPT_ARGSS  comma lists parallel to URL_PATHS (empty = the above)
#   H2_PROFILES  comma-separated HTTP/2 lanes (default none), with
#   H2_CONNS H2_STREAMS H2_N H2_WARMUP H2_URIS H2_HEADER
#   APP APP_PYTHONPATH TLS_CERT TLS_KEY  instance paths; APP_ENV  VAR=value words for the app

AB_KNOBS=(PATHSPEC ROUNDS PHASES PORT DURATION WARMUP THREADS CONNS PIPELINE BB_UVLOOP
          BB_FORCE_ASGI_SCOPE SERVER_CPUS LOAD_CPUS BASE_EXTRA_ENV TREAT_EXTRA_ENV WRK_HEADERS
          H2_CONNS H2_STREAMS H2_N H2_WARMUP H2_URIS H2_HEADER APP APP_PYTHONPATH TLS_CERT TLS_KEY)

job_preflight() {
    : "${PATHSPEC:=blackbull/}" "${ROUNDS:=8}" "${PHASES:=null real}" "${PORT:=8443}"
    : "${DURATION:=15}" "${WARMUP:=5}" "${THREADS:=4}" "${CONNS:=32}" "${PIPELINE:=1}"
    : "${BB_UVLOOP:=0}" "${BB_FORCE_ASGI_SCOPE:=0}" "${SERVER_CPUS:=0-1}" "${LOAD_CPUS:=2-5}"
    : "${URL_PATHS:=/plaintext}" "${H2_PROFILES:=}"
    [[ "$ROUNDS" =~ ^[1-9][0-9]*$ ]] || { echo "ab: ROUNDS must be a positive integer" >&2; return 1; }
    local phase phases=0
    for phase in $PHASES; do
        [ "$phase" = null ] || [ "$phase" = real ] || { echo "ab: PHASES may name only null and real" >&2; return 1; }
        phases=$((phases + 1))
    done
    REF_BASE="$(git rev-parse --verify -q "${REF_BASE:-HEAD~1}^{commit}")" || { echo "ab: unknown REF_BASE" >&2; return 1; }
    REF_TREAT="$(git rev-parse --verify -q "${REF_TREAT:-HEAD}^{commit}")" || { echo "ab: unknown REF_TREAT" >&2; return 1; }
    git diff --quiet "$REF_TREAT" -- "$PATHSPEC" || {
        echo "ab: the working tree under $PATHSPEC differs from REF_TREAT — the deployed tree must be the treatment; check it out and commit" >&2
        return 1
    }
    IFS=, read -r -a H1_LANES <<< "$URL_PATHS"
    IFS=, read -r -a H2_LANES <<< "$H2_PROFILES"
    # Each arm: the measured run, its warm-up, and a server restart (~15 s);
    # an h2load arm of the default size takes well under 45 s.  Half again for
    # slack, plus the setup the runner does before its first arm.
    local arms=$((phases * ROUNDS * 4))
    JOB_WAIT_SECONDS=$(( (arms * (${#H1_LANES[@]} * (DURATION + WARMUP + 15) + ${#H2_LANES[@]} * 45)) * 3 / 2 + 600 ))
    JOB_DEPLOY_GIT=1
    JOB_SETTINGS=(ref_base="$REF_BASE" ref_treat="$REF_TREAT" h1_lanes="$URL_PATHS"
                  h2_lanes="$H2_PROFILES" phase_count="$phases" app_env="${APP_ENV:-}")
    local knob
    for knob in "${AB_KNOBS[@]}"; do JOB_SETTINGS+=("${knob,,}=${!knob:-}"); done
}

# <h1|h2>-<lane without its leading slash, other bytes than [A-Za-z0-9._-] as _>;
# job_collect derives the same names.
_ab_lane_dir() { printf '%s-%s' "$1" "$(printf '%s' "${2#/}" | LC_ALL=C tr -c 'A-Za-z0-9._-' '_')"; }

job_runner() {
    local knob i
    echo 'set -euo pipefail'
    printf 'cd %q\n' "$REMOTE_REPO"
    printf 'export REF_BASE=%q REF_TREAT=%q\n' "$REF_BASE" "$REF_TREAT"
    for knob in "${AB_KNOBS[@]}"; do printf 'export %s=%q\n' "$knob" "${!knob:-}"; done
    [ -z "${APP_ENV:-}" ] || printf 'export %s\n' "$APP_ENV"
    IFS=, read -r -a scripts <<< "${WRK_SCRIPTS:-}"
    IFS=, read -r -a script_args <<< "${WRK_SCRIPT_ARGSS:-}"
    for i in "${!H1_LANES[@]}"; do
        printf 'BENCH_RUN_DIR=%q URL_PATH=%q WRK_SCRIPT=%q WRK_SCRIPT_ARGS=%q bash bench/ab/ab_commit.sh\n' \
            "$REMOTE_RUN/$(_ab_lane_dir h1 "${H1_LANES[$i]}")" "${H1_LANES[$i]}" \
            "${scripts[$i]:-${WRK_SCRIPT:-}}" "${script_args[$i]:-${WRK_SCRIPT_ARGS:-}}"
    done
    for i in "${!H2_LANES[@]}"; do
        printf 'BENCH_RUN_DIR=%q URL_PATH=%q bash bench/ab/ab_commit_h2.sh\n' \
            "$REMOTE_RUN/$(_ab_lane_dir h2 "${H2_LANES[$i]}")" "${H2_LANES[$i]}"
    done
}

# Expectations come from the session's manifest, so `finish` in another shell
# judges the run that was launched, not the current environment.
job_collect() {
    python3 - "$LOCAL_RUN" <<'PY'
import json, re, sys
from pathlib import Path
run = Path(sys.argv[1])
s = json.loads((run / 'manifest.json').read_text())['settings']
want = 1 + int(s['rounds']) * 4 * int(s['phase_count'])
lanes = [('h1', u) for u in s['h1_lanes'].split(',') if u] + \
        [('h2', u) for u in s['h2_lanes'].split(',') if u]
ok, reports = True, []
for kind, url in lanes:
    name = re.sub(rb'[^A-Za-z0-9._-]', b'_', url.removeprefix('/').encode()).decode()
    lane = run / f'{kind}-{name}'
    raw = lane / 'raw.tsv'
    rows = raw.read_text().splitlines() if raw.exists() else []
    if len(rows) != want or any('\tNaN\t' in r for r in rows):
        print(f'ab: {lane.name}: {len(rows)} of {want} raw.tsv lines, or a failed run', file=sys.stderr)
        ok = False
    if (lane / 'report.md').exists():
        reports.append(f'## {kind.upper()} {url}\n\n' + (lane / 'report.md').read_text())
(run / 'report.md').write_text('\n'.join(reports))
sys.exit(0 if ok else 1)
PY
}
