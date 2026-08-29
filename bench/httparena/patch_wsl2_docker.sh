#!/usr/bin/env bash
# bench/httparena/patch_wsl2_docker.sh — Docker-Desktop/WSL2 bind-mount
# compatibility layer for the HttpArena harness.
#
# WHY: on this Docker Desktop under WSL2 the host file-sharing transport is
# broken — bind-mounts of BOTH the WSL filesystem and /mnt/c come through
# mangled (a mounted file appears as a DIRECTORY; directory contents can be
# empty).  HttpArena bind-mounts several single files (dataset.json, the
# postgres seed) and data/certs dirs, so validation fails outright.
#
# WHAT: detect the mangling; when present, replace the harness's bind-mounts
# with Docker NAMED VOLUMES populated via `docker cp` (which goes through the
# daemon API, not the broken file-sharing transport — verified working).
# The harness's own host-side reads of $DATA_DIR / $CERTS_DIR (expected file
# sizes, cert-dir probes) are untouched and keep using the real files.
#
# No-op on a Docker whose bind mounts work (e.g. EC2) — the EC2 harness keeps
# the upstream mounts.  Touches only the LOCAL harness clone.
#
# KNOWN LOCAL-ONLY FAILURES: the four "static / static-tls file|variant follows
# the disk" checks.  They replace a file in $DATA_DIR/static on the host and
# expect the server to serve the new bytes within 2s.  A named volume is
# populated once by `docker cp`, so a host-side replacement never reaches the
# container and the check fails here and only here.  It is not a caching bug:
# StaticFiles defaults to cache=False and stats + re-reads on every request,
# verified directly (1000 B file replaced with 2500 B, served at 2500 B after
# 2s).  Expect these four to pass on EC2, where the bind mount is real.
#
# Usage: bash bench/httparena/patch_wsl2_docker.sh [HARENA_DIR]
#   HARENA_DIR  harness root (default ~/HttpArena)
#
# Volumes: httparena-data, httparena-certs, httparena-pgseed — recreated and
# re-populated on every invocation so a stale volume can never leak into a run.
set -euo pipefail

HARENA_DIR="${1:-$HOME/HttpArena}"
REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"

VOL_DATA="httparena-data"
VOL_CERTS="httparena-certs"
VOL_PGSEED="httparena-pgseed"
HELPER="httparena-vol-pop"

# --- detect broken bind mounts (file mounted as a directory) ---------------
PROBE="/tmp/bb-mount-probe-$$"
echo x > "$PROBE"
if docker run --rm -v "$PROBE:/f:ro" postgres:18 cat /f 2>/dev/null | grep -qx x; then
    rm -f "$PROBE"
    echo "patch_wsl2_docker: bind mounts work — no compatibility layer needed"
    exit 0
fi
rm -f "$PROBE"
echo "patch_wsl2_docker: file bind-mounts are mangled (Docker Desktop/WSL2) — switching the harness to named volumes + docker cp"

# --- populate a named volume from host paths via docker cp ------------------
# docker cp reads the host file through the daemon API and writes it into the
# container's volume — no bind-mount involved, so the broken file sharing is
# never on the path.
_vol_rm() { docker volume rm -f "$1" >/dev/null 2>&1 || true; }
_vol_up() {  # _vol_up <volume> — start a helper container backed by the volume
    docker rm -f "$HELPER" >/dev/null 2>&1 || true
    docker run -d --name "$HELPER" --mount "source=$1,target=/vol" \
        alpine tail -f /dev/null >/dev/null
}
_vol_dn() { docker rm -f "$HELPER" >/dev/null 2>&1 || true; }

CERT_SRC="$HARENA_DIR/certs"
[ -f "$CERT_SRC/server.crt" ] || CERT_SRC="$REPO_ROOT/tests"
[ -f "$CERT_SRC/server.crt" ] || CERT_SRC="$REPO_ROOT/bench/httparena/_local/certs"
if [ ! -f "$CERT_SRC/server.crt" ]; then
    echo "ERROR: no server.crt found (tried $HARENA_DIR/certs, $REPO_ROOT/tests, "
         "bench/httparena/_local/certs)" >&2
    exit 1
fi

echo ">>> populating volumes (data / certs / pg seed) ..."

# data volume: dataset.json (+ dataset-large.json when present) + static/
_vol_rm "$VOL_DATA"; docker volume create "$VOL_DATA" >/dev/null
_vol_up "$VOL_DATA"
docker cp "$HARENA_DIR/data/dataset.json" "$HELPER:/vol/dataset.json" >/dev/null
[ -f "$HARENA_DIR/data/dataset-large.json" ] \
    && docker cp "$HARENA_DIR/data/dataset-large.json" "$HELPER:/vol/" >/dev/null
[ -d "$HARENA_DIR/data/static" ] && docker cp "$HARENA_DIR/data/static" "$HELPER:/vol/" >/dev/null
_vol_dn

# certs volume: server.crt (+ server.key)
_vol_rm "$VOL_CERTS"; docker volume create "$VOL_CERTS" >/dev/null
_vol_up "$VOL_CERTS"
docker cp "$CERT_SRC/server.crt" "$HELPER:/vol/server.crt" >/dev/null
[ -f "$CERT_SRC/server.key" ] \
    && docker cp "$CERT_SRC/server.key" "$HELPER:/vol/server.key" >/dev/null
_vol_dn

# pg seed volume: a directory whose seed.sql the entrypoint runs at initdb
_vol_rm "$VOL_PGSEED"; docker volume create "$VOL_PGSEED" >/dev/null
_vol_up "$VOL_PGSEED"
docker cp "$HARENA_DIR/data/pgdb-seed.sql" "$HELPER:/vol/seed.sql" >/dev/null
_vol_dn

# --- patch the harness: bind-mounts → named volumes -------------------------
# postgres seed (two sites: postgres.sh lib + validate.sh sidecar).
sed -i "s|-v \"\$DATA_DIR/pgdb-seed.sql:/docker-entrypoint-initdb.d/seed.sql:ro\"|-v ${VOL_PGSEED}:/docker-entrypoint-initdb.d:ro|" \
    "$HARENA_DIR/scripts/lib/postgres.sh" "$HARENA_DIR/scripts/validate.sh"
# data: per-file mounts → one volume covering dataset.json + static/.
# The static mount is REPLACED with a no-op `:` (NOT deleted): bash rejects
# an empty then-block (`if …; then` immediately followed by `fi` is a parse
# error), and the httparena-data volume already serves /data/static.
sed -i "s|-v \"\$DATA_DIR/dataset.json:/data/dataset.json:ro\"|-v ${VOL_DATA}:/data:ro|" \
    "$HARENA_DIR/scripts/validate.sh" "$HARENA_DIR/scripts/lib/framework.sh"
# The static mount appears in two shapes upstream and both must go, or the
# surviving bind-mount shadows /data/static in the volume with an empty dir
# and every static asset 404s:
#   * a statement — replaced with `:`, NOT deleted, because bash rejects an
#     empty then-block;
#   * a bare line inside a docker_args=( … ) array — deleted, since `:` there
#     would become a literal argument.
for f in "$HARENA_DIR/scripts/validate.sh" "$HARENA_DIR/scripts/lib/framework.sh"; do
    sed -i 's|docker_args+=(-v "\$DATA_DIR/static:/data/static:ro")|: # static served from the data volume|' "$f"
    sed -i '/^[[:space:]]*-v "\$DATA_DIR\/static:\/data\/static:ro"[[:space:]]*$/d' "$f"
done
# A silent no-match here costs a whole run, so say so rather than carry on.
for f in "$HARENA_DIR/scripts/validate.sh" "$HARENA_DIR/scripts/lib/framework.sh"; do
    if grep -q -- '-v "\$DATA_DIR/static:/data/static:ro"' "$f"; then
        echo "ERROR: the static bind-mount survived in $f — upstream changed its"
        echo "       shape again; /data/static would be empty in the container." >&2
        exit 1
    fi
done
# certs: dir mount → volume.
sed -i "s|-v \"\$CERTS_DIR:/certs:ro\"|-v ${VOL_CERTS}:/certs:ro|" \
    "$HARENA_DIR/scripts/validate.sh" "$HARENA_DIR/scripts/lib/framework.sh"

# --- patch the harness: host networking → bridge + published ports -----------
# On Docker Desktop/WSL2, `--network host` binds inside the docker-desktop VM,
# whose loopback is NOT the WSL2 distro's — so the harness's localhost probes
# (run from the distro) never reach the containers.  Bridge + `-p` publishing
# forwards to the WSL2 host (verified).  Sidecars are reached by container name
# on the shared network instead of localhost.
echo ">>> patching network: --network host → httparena-net (bridge, published ports) ..."
docker network inspect httparena-net >/dev/null 2>&1 || docker network create httparena-net >/dev/null

# validate.sh — framework container: bridge + publish :8080 (+ h2/h1tls/h2c
# ports are already `-p`-published by the harness on those branches).
sed -i 's|docker_args=(-d --name "\$CONTAINER_NAME" --network host --security-opt seccomp=unconfined|docker_args=(-d --name "\$CONTAINER_NAME" --network httparena-net -p "\$PORT:8080" --security-opt seccomp=unconfined|' \
    "$HARENA_DIR/scripts/validate.sh"
# validate.sh + postgres.sh — postgres sidecar on the shared network.
sed -i 's|docker run -d --name "\$PG_CONTAINER" --network host \\|docker run -d --name "\$PG_CONTAINER" --network httparena-net \\|' \
    "$HARENA_DIR/scripts/validate.sh" "$HARENA_DIR/scripts/lib/postgres.sh"
sed -i 's|docker run -d --rm --name "\$PG_CONTAINER" --network host \\|docker run -d --rm --name "\$PG_CONTAINER" --network httparena-net \\|' \
    "$HARENA_DIR/scripts/lib/postgres.sh"
# validate.sh — redis sidecar on the shared network.
sed -i 's|docker run -d --rm --name "\$REDIS_CONTAINER" --network host \\|docker run -d --rm --name "\$REDIS_CONTAINER" --network httparena-net \\|' \
    "$HARENA_DIR/scripts/validate.sh"
# validate.sh — framework reaches sidecars by container name on the network.
sed -i 's|postgres://bench:bench@localhost:5432/benchmark|postgres://bench:bench@httparena-validate-postgres:5432/benchmark|' \
    "$HARENA_DIR/scripts/validate.sh"
sed -i 's|REDIS_URL="redis://localhost:6379"|REDIS_URL="redis://httparena-redis:6379"|' \
    "$HARENA_DIR/scripts/validate.sh"
# framework.sh — benchmark path, same network (consistency; local-only).
sed -i 's|--network host|--network httparena-net|' \
    "$HARENA_DIR/scripts/lib/framework.sh"

echo "patch_wsl2_docker: harness patched — volumes $VOL_DATA / $VOL_CERTS / $VOL_PGSEED + network httparena-net"
echo "  data volume contents:"
docker run --rm -v "$VOL_DATA:/d:ro" alpine ls -la /d/
