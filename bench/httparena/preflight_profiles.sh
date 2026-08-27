#!/bin/bash
# Answer, before any instance is provisioned, the two questions that cost a
# whole run when they were left unasked:
#
#   1. Does every profile this repo's meta.json subscribes to still exist
#      upstream?  A profile upstream has dropped makes benchmark.sh reject the
#      framework outright, before a single request is sent.
#   2. Does every framework named in $FRAMEWORKS actually subscribe to every
#      profile in $PROFILES — reading the *upstream* meta.json, since that is
#      what the run will use.
#
# Usage: HTTPARENA_DIR=<clone> PROFILES="..." FRAMEWORKS="..." bash $0
set -u
DIR="${HTTPARENA_DIR:-$HOME/work/HttpArena}"
: "${PROFILES:?set PROFILES}"
: "${FRAMEWORKS:?set FRAMEWORKS}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

[ -d "$DIR/frameworks" ] || { echo "FAIL: no HttpArena clone at $DIR"; exit 1; }

python3 - "$DIR" "$REPO_ROOT" "$PROFILES" "$FRAMEWORKS" <<'PY'
import json, os, sys
clone, repo, profiles, frameworks = sys.argv[1:5]
profiles = profiles.split(); frameworks = frameworks.split()

known = set()
for name in os.listdir(f'{clone}/frameworks'):
    try:
        known |= set(json.load(open(f'{clone}/frameworks/{name}/meta.json')).get('tests') or [])
    except Exception:
        pass

fail = []
local = json.load(open(f'{repo}/bench/httparena/meta.json')).get('tests') or []
gone = [t for t in local if t not in known]
if gone:
    fail.append(f'bench/httparena/meta.json subscribes to profiles no upstream '
                f'entry knows: {gone}')

for fw in frameworks:
    if fw.startswith('blackbull'):
        subs = local
        where = 'bench/httparena/meta.json'
    else:
        path = f'{clone}/frameworks/{fw}/meta.json'
        if not os.path.exists(path):
            fail.append(f'{fw}: no upstream entry at {path}'); continue
        subs = json.load(open(path)).get('tests') or []
        where = f'upstream frameworks/{fw}/meta.json'
        shadow = f'{repo}/bench/httparena/{fw}/meta.json'
        if os.path.exists(shadow):
            fail.append(f'{fw}: this repo carries bench/httparena/{fw}/, which '
                        f'shadows the upstream entry — upstream must win')
    missing = [p for p in profiles if p not in subs]
    if missing:
        fail.append(f'{fw} does not subscribe to {missing} (per {where})')

if fail:
    print('PRE-FLIGHT FAILED:')
    for f in fail: print(f'  - {f}')
    sys.exit(1)
print(f'pre-flight OK: {frameworks} all subscribe to {profiles}')
PY
