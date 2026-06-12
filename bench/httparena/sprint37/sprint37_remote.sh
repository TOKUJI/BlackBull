#!/usr/bin/env bash
# bench/httparena/sprint37/sprint37_remote.sh — runs on EC2 instance.
#
# One full HttpArena pass over the profiles claimed in
# `~/HttpArena/frameworks/blackbull/meta.json`.  Builds the framework
# image, validates, runs benchmark.sh per profile, writes results
# under ~/sprint37-results-<run>/.
#
# The orchestrator (sprint37_drive.sh, runs locally) rsyncs the
# HttpArena tree to ~/HttpArena/ and invokes this with the run id.
#
# Args: <run_id>
# e.g.  bash sprint37_remote.sh run1

set -euo pipefail

RUN_ID="${1:?run_id required, eg run1 / run2}"
# Optional 2nd arg: comma-separated profile subset to run instead of
# the full list from meta.json["tests"].  Empty string = use meta.json.
PROFILES_OVERRIDE="${2:-}"
HARENA_DIR="${HOME}/HttpArena"
FRAMEWORK=blackbull
OUT_DIR="${HOME}/sprint37-results-${RUN_ID}"

if [ ! -d "$HARENA_DIR" ]; then
    echo "FAIL: $HARENA_DIR not found; orchestrator should rsync the HttpArena tree first" >&2
    exit 1
fi

mkdir -p "$OUT_DIR"
exec > >(tee -a "$OUT_DIR/remote.log") 2>&1
echo "=== sprint37 remote run $RUN_ID @ $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
echo "=== host: $(hostname) — $(uname -srm) — $(nproc) cores ==="

cd "$HARENA_DIR"

# ── 0. Loadgen choice ───────────────────────────────────────────────────────
# benchmark.sh defaults to NATIVE load-generator binaries.  `bench/aws/
# install.sh` already installs h2load + wrk on the instance, but NOT
# gcannon (which needs liburing 2.9 + a source build).  HttpArena's
# documented escape hatch is LOADGEN_DOCKER=true: benchmark.sh falls
# back to the same docker images benchmark-lite.sh uses.  First
# invocation builds whichever loadgen images are missing on-demand.
#
# Methodology note: docker-mode loadgens are NOT exactly the same as
# native-mode (one extra IPC layer).  For our purposes — Run #1 vs
# Run #2 reproducibility check + framework integration validation —
# this is fine.  The maintainer re-measures on his own hardware when
# accepting submissions, so absolute leaderboard parity isn't our
# deliverable.
export LOADGEN_DOCKER=true
echo "=== LOADGEN_DOCKER=true (docker-mode loadgens, gcannon image will build on first use) ==="

# ── 1. Build the framework image ────────────────────────────────────────────
echo "=== build image ==="
docker build -t "httparena-${FRAMEWORK}" "frameworks/${FRAMEWORK}/"

# ── 2. Run validate.sh ──────────────────────────────────────────────────────
echo "=== validate ==="
bash scripts/validate.sh "$FRAMEWORK" | tee "$OUT_DIR/validate.log"

# ── 3. Run benchmark.sh per profile ─────────────────────────────────────────
# Profile list source:
#   - If $PROFILES_OVERRIDE was passed in (2nd arg), use that comma-separated
#     subset — cheap-tier validation runs.
#   - Otherwise extract from meta.json["tests"] so the script doesn't drift
#     when we change what we claim upstream.
if [ -n "$PROFILES_OVERRIDE" ]; then
    PROFILES=$(echo "$PROFILES_OVERRIDE" | tr ',' ' ')
    echo "=== profile override: $PROFILES ==="
else
    PROFILES=$(python3 -c "
import json
with open('frameworks/${FRAMEWORK}/meta.json') as f:
    print(' '.join(json.load(f)['tests']))
")
    echo "=== claimed profiles (from meta.json): $PROFILES ==="
fi

for prof in $PROFILES; do
    echo "=== benchmark: $prof ==="
    # `--save` persists the per-cell result JSONs to
    # ~/HttpArena/results/<round>/<framework>/.  Without it,
    # benchmark.sh runs in dry-run mode and writes nothing to disk
    # (logs still print the numbers).
    if bash scripts/benchmark.sh "$FRAMEWORK" "$prof" --save 2>&1 | tee "$OUT_DIR/bench-${prof}.log"; then
        echo "OK $prof"
    else
        echo "WARN $prof exited non-zero — continuing"
    fi
done

# Count how many profiles we asked for, so the JSON-count sanity
# check below has something to compare against.
PROFILE_COUNT=$(echo "$PROFILES" | wc -w)

# ── 4. Collect HttpArena's own result JSONs ─────────────────────────────────
# benchmark.sh writes results to ~/HttpArena/results/<round>/<framework>/.
# Snapshot whatever landed.
if [ -d "results" ]; then
    mkdir -p "$OUT_DIR/results"
    rsync -a --include='*.json' --include='*/' --exclude='*' \
          "results/" "$OUT_DIR/results/"
fi

# ── 4b. Sanity-check the produced result JSONs ──────────────────────────────
# Two checks:
#   (a) the number of JSONs is at least the number of claimed profiles —
#       catches missing `--save` and benchmark.sh refusing to run an entire
#       profile silently.
#   (b) no JSON has rps=0 — catches silent loadgen failures (the
#       gcannon-missing pattern from the previous iteration).
# Either failure exits non-zero so the orchestrator surfaces the
# problem and we don't waste another iteration discovering it on
# the spend side.
echo "=== sanity check: JSON count >= profile count, rps > 0 in every JSON ==="
SANITY=$(python3 -c "
import json, pathlib
zero, ok = [], []
for p in sorted(pathlib.Path('$OUT_DIR/results').rglob('*.json')):
    try:
        d = json.load(open(p))
    except Exception:
        continue
    rps = d.get('rps')
    if rps in (None, 0, '0', '0.00'):
        zero.append(str(p))
    else:
        ok.append(str(p))
for z in zero:
    print('ZERO', z)
print('TOTAL_OK', len(ok))
print('TOTAL_ZERO', len(zero))
" | tee "$OUT_DIR/sanity.log")

OK_COUNT=$(echo "$SANITY" | awk '/^TOTAL_OK/  {print $2}')
ZERO_COUNT=$(echo "$SANITY" | awk '/^TOTAL_ZERO/{print $2}')
SANITY_FAILED=0

if [ "${ZERO_COUNT:-0}" -gt 0 ]; then
    echo "FAIL: $ZERO_COUNT result JSONs report rps=0 — likely missing loadgen or framework boot failure"
    SANITY_FAILED=1
fi
if [ "${OK_COUNT:-0}" -lt "${PROFILE_COUNT:-1}" ]; then
    echo "FAIL: only $OK_COUNT non-zero JSONs found, expected at least $PROFILE_COUNT (one per claimed profile)"
    echo "  → check whether benchmark.sh was invoked with --save and whether every profile actually ran"
    SANITY_FAILED=1
fi
if [ "$SANITY_FAILED" = 0 ]; then
    echo "OK: $OK_COUNT result JSONs, all rps > 0 (claimed profiles: $PROFILE_COUNT)"
fi

# Snapshot system + docker info so the run is self-describing.
# Pipelines that feed `head` need `|| true` to mask the SIGPIPE the
# producer gets when head closes the pipe early — `set -o pipefail`
# at the top of the script would otherwise propagate exit 141.
{
    echo '--- uname ---';        uname -a
    echo '--- nproc ---';        nproc
    echo '--- /proc/meminfo ---'; head -5 /proc/meminfo || true
    echo '--- docker info ---';  docker info 2>/dev/null | head -30 || true
    echo '--- ulimit -a ---';    bash -c 'ulimit -a'
    echo '--- git head ---';     git -C "$HARENA_DIR" rev-parse HEAD 2>/dev/null || echo 'not a git repo'
} > "$OUT_DIR/system.txt"

# Tarball for easy rsync-back.
tar -C "$HOME" -czf "${OUT_DIR}.tar.gz" "$(basename "$OUT_DIR")"
echo "=== done: results in $OUT_DIR and ${OUT_DIR}.tar.gz ==="

exit "${SANITY_FAILED:-0}"
