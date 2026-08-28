#!/usr/bin/env bash
# bench/httparena/validate_local.sh — run HttpArena's correctness validation
# (validate.sh, all 20 subscribed profiles) on the local box (WSL2).
#
# This is the correctness gate for the "validate once on WSL2, ready-check only
# on EC2" workflow: if this passes against wheel W, the EC2 run must upload the
# SAME wheel (BB_WHEEL_PATH="$WHEEL" LOCAL_BB_WHEEL=1) and skip the full EC2
# validate (SKIP_VALIDATE=1 → run_httparena.sh performs the minimal
# ready_check.sh instead).  Validation is correctness-only and
# hardware-independent, so a WSL2 pass transfers to EC2 for the app contract;
# the EC2 ready-check covers the EC2-environment residues (container start,
# port binds, wheel transfer, shim).
#
# Usage:
#   bash bench/httparena/validate_local.sh [REF|WHEEL]
#     - a path ending in .whl is treated as an existing wheel
#     - anything else is a git ref to build (default: HEAD)
#
# Env:
#   HARENA_DIR     harness clone root (default ~/HttpArena)
#   VALIDATE_WALL  wall-clock bound on validate.sh, incl. teardown hang
#                  (default 720; upstream validate.sh can hang forever in
#                  cleanup after printing its verdict)
#
# Output: bench/results/httparena-local/<UTC>/validate-blackbull.log (+ verdict)
# Prints the verdict and the result paths.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
HARENA_DIR="${HARENA_DIR:-$HOME/HttpArena}"
VALIDATE_WALL="${VALIDATE_WALL:-720}"
HARENA_REPO="https://github.com/MDA2AV/HttpArena.git"

# --- 1. resolve the wheel ---------------------------------------------------
ARG="${1:-HEAD}"
if [[ "$ARG" == *.whl ]]; then
    WHEEL="$(cd "$(dirname "$ARG")" && pwd)/$(basename "$ARG")"
    WHEEL_REF="(supplied: $(basename "$ARG"))"
    echo "using wheel: $WHEEL"
else
    echo ">>> building wheel from ref $ARG ..."
    WHEEL="$(bash "$REPO_ROOT/bench/httparena/build_wheel.sh" "$ARG")"
    WHEEL_REF="$(git -C "$REPO_ROOT" rev-parse --short "$ARG")"
fi
[ -f "$WHEEL" ] || { echo "ERROR: wheel not found: $WHEEL" >&2; exit 1; }
WHEEL_NAME="$(basename "$WHEEL")"
WHEEL_SHA="$(awk '{print $1}' "$WHEEL.sha256" 2>/dev/null || sha256sum "$WHEEL" | awk '{print $1}')"
echo "    sha256: $WHEEL_SHA"

# --- 2. harness clone -------------------------------------------------------
if [ ! -d "$HARENA_DIR/.git" ]; then
    echo ">>> cloning harness to $HARENA_DIR ..."
    git clone --depth 1 "$HARENA_REPO" "$HARENA_DIR"
elif [ "${HARENA_REFRESH:-1}" = "1" ]; then
    # EC2 clones fresh on every run; this one persisted and did not, so the
    # two drifted until a profile the entry subscribes to existed on one side
    # and not the other -- and upstream force-pushes, so a fetch is not enough.
    # The local patches below are re-applied every run, so a hard reset costs
    # nothing.  HARENA_REFRESH=0 pins the clone when that is what you want.
    echo ">>> refreshing harness at $HARENA_DIR ..."
    git -C "$HARENA_DIR" fetch --depth 1 origin main
    git -C "$HARENA_DIR" reset --hard FETCH_HEAD
else
    echo "harness present at $HARENA_DIR"
fi

# --- 3. patch the harness ---------------------------------------------------
# patch_httparena.py asserts exactly-one match per substitution, so it is NOT
# idempotent: apply only when the framework.sh WEB_WORKERS injection is absent.
if grep -q 'WEB_WORKERS' "$HARENA_DIR/scripts/lib/framework.sh"; then
    echo "harness already patched (WEB_WORKERS injection present)"
else
    echo ">>> applying patch_httparena.py ..."
    python3 "$REPO_ROOT/bench/httparena/patch_httparena.py" "$HARENA_DIR"
fi

# patch_cpuset.sh remaps the reference host's cpusets onto this box's vCPUs
# (redis 0,64 / gcannon 32-63,96-127 do not exist here).  It hardcodes
# `cd ~/HttpArena` internally, so HARENA_DIR must be ~/HttpArena for it.
if [ "$HARENA_DIR" != "$HOME/HttpArena" ]; then
    echo "WARN: patch_cpuset.sh hardcodes ~/HttpArena; HARENA_DIR=$HARENA_DIR "
         "not patched" >&2
else
    echo ">>> applying patch_cpuset.sh ..."
    bash "$REPO_ROOT/bench/httparena/patch_cpuset.sh" 2>&1 | tail -3
fi

# --- 3b. Docker-Desktop/WSL2 bind-mount compatibility ------------------------
# Docker Desktop under WSL2 can mount files-as-directories; when it does, the
# harness's per-file mounts (dataset.json, postgres seed) fail.  The layer
# detects the mangling and swaps the local harness's bind-mounts for named
# volumes populated via `docker cp`.  No-op where bind mounts work (e.g. EC2);
# the harness's own $DATA_DIR / $CERTS_DIR host reads are left untouched.
bash "$REPO_ROOT/bench/httparena/patch_wsl2_docker.sh" "$HARENA_DIR"

# --- 4. stage the framework dir --------------------------------------------
FW_DIR="$HARENA_DIR/frameworks/blackbull"
echo ">>> staging framework at $FW_DIR ..."
mkdir -p "$FW_DIR"
cp "$REPO_ROOT/bench/httparena/app.py" \
   "$REPO_ROOT/bench/httparena/launcher.py" \
   "$REPO_ROOT/bench/httparena/meta.json" \
   "$REPO_ROOT/bench/httparena/db.py" \
   "$REPO_ROOT/bench/httparena/grpc_bench.py" \
   "$FW_DIR/"
rm -f "$FW_DIR/.dockerignore"   # upstream vendors one that excludes db.py/grpc_bench.py
cp "$WHEEL" "$FW_DIR/"

cat > "$FW_DIR/Dockerfile" <<EOF
# Auto-generated by bench/httparena/validate_local.sh — installs the locally
# validated wheel.  Mirrors the Dockerfile httparena_compare.sh generates on
# EC2 (LOCAL_BB_WHEEL=1) so the local correctness gate and the EC2 benchmark
# use the same image recipe.
FROM python:3.13-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \\
    PYTHONUNBUFFERED=1 \\
    PIP_NO_CACHE_DIR=1 \\
    PIP_DISABLE_PIP_VERSION_CHECK=1
COPY ${WHEEL_NAME} /tmp/
RUN cd /tmp && pip install --no-cache-dir "/tmp/${WHEEL_NAME}[compression,speed]" asyncpg redis
VOLUME /results
COPY app.py launcher.py db.py grpc_bench.py /app/
ENV BB_UVLOOP=0
EXPOSE 8080 8081 8443
CMD ["python", "launcher.py"]
EOF

# build.sh lets validate.sh build without --no-cache so the pre-build below is
# reused (validate.sh has a 300 s overall watchdog; a cold pip layer blows it).
cat > "$FW_DIR/build.sh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
FRAMEWORK="$(basename "$SCRIPT_DIR")"
IMAGE_NAME="httparena-${FRAMEWORK}"
docker build -t "$IMAGE_NAME" "$SCRIPT_DIR"
EOF
chmod +x "$FW_DIR/build.sh"

# --- 5. pre-build the image (beats validate.sh's 300 s watchdog) -----------
echo ">>> pre-building httparena-blackbull ..."
( cd "$HARENA_DIR" && bash "$FW_DIR/build.sh" )

# --- 6. run validate.sh -----------------------------------------------------
TS="$(date -u +%Y%m%d-%H%M%SZ)"
RESULT_DIR="$REPO_ROOT/bench/results/httparena-local/$TS"
mkdir -p "$RESULT_DIR"
LOG="$RESULT_DIR/validate-blackbull.log"

echo ">>> running validate.sh (wall bound ${VALIDATE_WALL}s) ..."
# No sudo here — WSL2 docker runs as the user; `timeout` can kill the process
# tree.  VALIDATE_TIMEOUT matches the EC2 invocation.
set +e
( cd "$HARENA_DIR" \
    && timeout -k 30 "$VALIDATE_WALL" \
           env VALIDATE_TIMEOUT=600 ./scripts/validate.sh blackbull ) \
    | tee "$LOG"
RC="${PIPESTATUS[0]}"
set -e

VERDICT="UNKNOWN"
if [ "$RC" -eq 124 ] || [ "$RC" -eq 137 ]; then
    VERDICT="TEARDOWN-HANG-BOUNDED (rc=$RC; verdict in log)"
elif [ "$RC" -eq 0 ]; then
    VERDICT="PASS"
else
    VERDICT="FAIL (rc=$RC)"
fi

# Cross-check with the log's own pass/fail summary when present.
if grep -qE '[0-9]+ passed, [0-9]+ failed' "$LOG"; then
    SUMMARY="$(grep -oE '[0-9]+ passed, [0-9]+ failed' "$LOG" | tail -1)"
    echo "    validate.sh summary: $SUMMARY"
fi

echo "$VERDICT" > "$RESULT_DIR/verdict.txt"
{
    echo "wheel : $WHEEL"
    echo "sha256: $WHEEL_SHA"
    # Two refs, not one.  The wheel comes from the ref that was asked for;
    # app.py and launcher.py are copied from the working tree.  Recording only
    # HEAD says the wheel came from somewhere it did not, and hides the pairing
    # that has to be right for an A/B arm to mean anything.
    echo "wheel ref   : $WHEEL_REF"
    echo "harness ref : $(git -C "$REPO_ROOT" rev-parse --short HEAD)"
    echo "rc    : $RC"
    echo "verdict: $VERDICT"
} > "$RESULT_DIR/provenance.txt"

echo
echo "=== verdict: $VERDICT ==="
echo "    log      : $LOG"
echo "    verdict  : $RESULT_DIR/verdict.txt"
[ "$VERDICT" = "PASS" ] || exit 1
