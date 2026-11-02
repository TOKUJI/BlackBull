# A frozen client probe through the standard provision/run/pull/teardown session.
# AB_CLIENT_RUN contains run.py, its helpers, base/treat snapshots and sources.json.

CLIENT_AB_FILES=(run.py driver.py probe.py proof.inc monitor.py report.py estimator.py sources.json)

job_preflight() {
    : "${AB_CLIENT_RUN:?client-ab: AB_CLIENT_RUN is required}"
    [ "${TOPO:-single}" = single ] || { echo 'client-ab: single topology required' >&2; return 1; }
    local files
    files="$(python3 - "$AB_CLIENT_RUN" "$REPO_ROOT" "${CLIENT_AB_FILES[@]}" <<'PY'
import hashlib, json, re, sys
from pathlib import Path
run, repo = map(Path, sys.argv[1:3])
sys.path.insert(0, str(repo))
from bench.lib.results import results_root
if not run.resolve().is_relative_to(results_root().resolve()):
    sys.exit('client-ab: input must be a bench run directory')
sources = json.loads((run / 'sources.json').read_text())
manifest = sources.get('files')
if manifest is not None and (not isinstance(manifest, dict) or 'run.py' not in manifest):
    sys.exit('client-ab: files manifest requires run.py')
names = [*manifest, 'sources.json'] if manifest is not None else sys.argv[3:]
for name in names:
    relative = Path(name)
    if (not re.fullmatch(r'[A-Za-z0-9_./-]+', name) or relative.is_absolute()
            or '..' in relative.parts or relative.as_posix() != name):
        sys.exit(f'client-ab: invalid harness path {name}')
    if manifest is not None and name != 'sources.json':
        if relative.suffix not in ('.py', '.inc') or relative.parts[0] in ('base', 'treat', 'tmp'):
            sys.exit(f'client-ab: invalid harness source path {name}')
    path = run / relative
    if not path.is_file() or any(p.is_symlink() for p in (path, *path.parents) if p != run):
        sys.exit(f'client-ab: missing or linked input {name}')
    if manifest is not None and name != 'sources.json':
        if hashlib.sha256(path.read_bytes()).hexdigest() != manifest[name]:
            sys.exit(f'client-ab: harness differs from manifest: {name}')
def hashes(root):
    if any(p.is_symlink() for p in (root / 'blackbull').rglob('*')):
        sys.exit('client-ab: linked snapshot source')
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in (root / 'blackbull').rglob('*.py')}
for arm in ('base', 'treat'):
    if not sources.get(arm) or hashes(run / arm) != sources[arm]:
        sys.exit(f'client-ab: {arm} snapshot differs from sources.json')
if hashes(repo) != sources['treat']:
    sys.exit('client-ab: working source differs from treatment snapshot')
print('\n'.join(names))
PY
    )" || return 1
    mapfile -t CLIENT_AB_FILES <<< "$files"
    CLIENT_AB_TIMEOUT_SECONDS="${AB_CLIENT_TIMEOUT_SECONDS:-1800}"
    [[ "$CLIENT_AB_TIMEOUT_SECONDS" =~ ^[1-9][0-9]{0,4}$ ]] && \
        [ "$CLIENT_AB_TIMEOUT_SECONDS" -le 86400 ] || return 1
    JOB_WAIT_SECONDS=$((CLIENT_AB_TIMEOUT_SECONDS + 1800))
    JOB_DEPLOY_GIT=0
    export CLOUD_SMOKE_PYTHON_VERSION=3.14.6
    JOB_SETTINGS=(client_input="$AB_CLIENT_RUN" python_version=3.14.6
                  sources_sha256="$(sha256sum "$AB_CLIENT_RUN/sources.json" | cut -d' ' -f1)")
}

job_setup() {
    local host="$1" file
    ssh -n "${SSH_OPTS[@]}" "$host" "mkdir -p '$REMOTE_RUN'" || return 1
    for file in "${CLIENT_AB_FILES[@]}"; do
        rsync -azR -e "ssh ${SSH_OPTS[*]}" "$AB_CLIENT_RUN/./$file" "$host:$REMOTE_RUN/" || return 1
    done
    rsync -az --exclude=__pycache__ -e "ssh ${SSH_OPTS[*]}" \
        "$AB_CLIENT_RUN/base" "$AB_CLIENT_RUN/treat" "$host:$REMOTE_RUN/" || return 1
    ssh "${SSH_OPTS[@]}" "$host" \
        "cd '$REMOTE_REPO' && export UV_PROJECT_ENVIRONMENT=.venv-client-ab && uv sync --locked --python 3.14.6 --extra testing --extra speed --extra compression --extra profiling --extra fault-injection --extra reload" || return 1
}

job_runner() {
    printf 'set -euo pipefail\ncd %q\n' "$REMOTE_REPO"
    printf 'mkdir -p %q %q\n' "$REMOTE_RUN/samples" "$REMOTE_RUN/servers"
    printf 'lscpu --json > %q\n' "$REMOTE_RUN/lscpu.json"
    printf '.venv-client-ab/bin/python - %q <<\x27PY\x27\n' "$REMOTE_RUN/environment.json"
    cat <<'PY'
import json, os, platform, sys
from pathlib import Path
assert sys.version_info[:3] == (3, 14, 6), sys.version
for cpu in (4, 6):
    assert cpu in os.sched_getaffinity(0)
    assert Path(f'/sys/devices/system/cpu/cpu{cpu}/topology/thread_siblings_list').read_text().strip() == str(cpu)
Path(sys.argv[1]).write_text(json.dumps(dict(python=sys.version, platform=platform.platform(),
    affinity=sorted(os.sched_getaffinity(0))), indent=2))
PY
    printf 'PY\n'
    printf 'uv pip freeze --python .venv-client-ab/bin/python > %q\n' "$REMOTE_RUN/packages.txt"
    printf 'timeout %s .venv-client-ab/bin/python %q\n' "$CLIENT_AB_TIMEOUT_SECONDS" "$REMOTE_RUN/run.py"
}

job_collect() {
    [ -s "$LOCAL_RUN/COMPLETE" ] && [ -s "$LOCAL_RUN/analysis.json" ] && \
        [ -s "$LOCAL_RUN/environment.json" ] && [ -s "$LOCAL_RUN/summary.md" ] || return 1
    cp "$LOCAL_RUN/summary.md" "$LOCAL_RUN/report.md"
}

job_on_poll() {
    local line
    line="$(timeout 15s ssh -n "${SSH_OPTS[@]}" "$1" "tail -n 1 '$REMOTE_RUN/runner.log'" 2>/dev/null)" || return 0
    if [ -n "$line" ] && [ "$line" != "${CLIENT_AB_LAST_LINE:-}" ]; then
        printf '%s\n' "$line"
        CLIENT_AB_LAST_LINE="$line"
    fi
}
