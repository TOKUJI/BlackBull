#!/usr/bin/env bash
# Patch HttpArena post-clone quirks for our small-instance runs.
# Run AFTER `git clone` on the EC2 instance.
# Idempotent — safe to run repeatedly on the same clone.
set -euo pipefail
cd ~/HttpArena

# ---------------------------------------------------------------------------
# CPU-set remaps.  HttpArena hardcodes the cpusets of its reference host: 64
# physical cores presented as 128 threads, split in half — server under test on
# the lower half (cores 0-31, i.e. threads 0-31 + siblings 64-95), load
# generator on the upper half (cores 32-63, threads 32-63 + siblings 96-127),
# redis co-located on the server's core 0 (threads 0,64).  None of those core
# IDs exist on our small boxes, so every hardcoded cpuset is the SAME quirk —
# a reference-host cpuset that has to be rescaled onto this box's vCPUs.
#
# Split by physical core, as the reference does: the server gets the lower
# half of the cores with all their SMT threads, the load generator the upper
# half, redis the server's first core.  Read from the kernel's topology, since
# sibling numbering differs by instance family (c7i: N and N+V/2); on an
# instance without SMT (c7a) this is simply the lower and upper half of 0..V-1.
V=$(nproc)                              # total vCPUs on this box
_TOPO=$(lscpu -p=CPU,CORE | grep -v '^#')
_CORES=$(cut -d, -f2 <<<"$_TOPO" | sort -un | paste -sd' ')
read -r -a _CORE_IDS <<<"$_CORES"
H=$(( ${#_CORE_IDS[@]} / 2 ))           # half of the physical cores
_cpus_of() {                            # CPUs whose core rank is in [$1, $2)
    awk -F, -v lo="$1" -v hi="$2" -v ids="$_CORES" '
        BEGIN { n = split(ids, c, " "); for (i = 1; i <= n; i++) rank[c[i]] = i - 1 }
        rank[$2] >= lo && rank[$2] < hi { print $1 }' <<<"$_TOPO" \
    | sort -n | awk '            # "a-b" runs: framework.sh reads a cpuset only if it has a "-"
        NR == 1 { a = b = $1; next }
        $1 == b + 1 { b = $1; next }
        { out = out sep a "-" b; sep = ","; a = b = $1 }
        END { print out sep a "-" b }'
}
SERVER_CPUS=$(_cpus_of 0 "$H")
LOADGEN_CPUS=$(_cpus_of "$H" "${#_CORE_IDS[@]}")
REDIS_CPUS=$(_cpus_of 0 1)
echo "patch_cpuset.sh: V=$V  server=$SERVER_CPUS  load-gen=$LOADGEN_CPUS  redis=$REDIS_CPUS" >&2

# redis.sh: default REDIS_CPUSET (0,64 → server core 0)
sed -i "s/-0,64}/-${REDIS_CPUS}}/" scripts/lib/redis.sh
# benchmark.sh: hardcoded Redis export for all profiles (0,64 → server core 0)
sed -i "s/export REDIS_CPUSET=\"0,64\"/export REDIS_CPUSET=\"${REDIS_CPUS}\"/" scripts/benchmark.sh
# benchmark.sh: CRUD gcannon load-gen cpuset (32-63,96-127 → upper half)
sed -i "s/export GCANNON_CPUS=\"32-63,96-127\"/export GCANNON_CPUS=\"${LOADGEN_CPUS}\"/" scripts/benchmark.sh
# profiles.sh: remap the reference server-half cpuset (0-31,64-95 — 32 physical
# cores × 2 SMT threads on the reference Threadripper PRO) to our server cpuset
# for ALL profiles.  This covers: baseline, json, json-tls, static, baseline-h2,
# static-h2, echo-ws, echo-ws-pipeline, pipelined, limited-conn, json-comp,
# upload, crud, async-db, unary-grpc, unary-grpc-tls, stream-grpc, stream-grpc-tls.
# api-4 and api-16 have absolute budgets handled separately below.
sed -i "s|0-31,64-95|${SERVER_CPUS}|g" scripts/lib/profiles.sh

# profiles.sh: api-4 / api-16 are FIXED cpu-BUDGET profiles — the whole point is
# measuring efficiency under a hard 4- and 16-logical-CPU cap.  Upstream encodes
# them as reference-host cpusets (0-1,64-65 and 0-7,64-71) whose sibling cores
# (64+) don't exist on our box, so upstream's "exceeds available CPUs — using all
# cores" fallback SILENTLY DROPS THE CAP and the server runs on all vCPUs,
# defeating the profile (api-4 ≈ api-16 ≈ unconstrained instead of a real cliff).
# These budgets are ABSOLUTE — they must NOT scale with the box — so map each to
# the same COUNT of contiguous low vCPUs (server 0-3 / 0-15; load-gen 16-31 on
# c7i.8xlarge).  Needs V >= 16 for api-16's cap plus load-gen headroom.
if (( V < 8 )); then
    echo "patch_cpuset.sh: WARN — box has $V vCPUs; api-4 needs >=8 (4 server + load-gen headroom)" >&2
fi
if (( V < 16 )); then
    echo "patch_cpuset.sh: WARN — box has $V vCPUs; api-16 needs >=16 for its cap + load-gen headroom" >&2
fi
sed -i "/\[api-4\]=/s|0-1,64-65|0-3|"  scripts/lib/profiles.sh
sed -i "/\[api-16\]=/s|0-7,64-71|0-15|" scripts/lib/profiles.sh

# ---------------------------------------------------------------------------
# framework.sh: gRPC readiness fall-through guard (NOT a cpuset issue).
# Upstream's server-wait dispatch runs `_wait_grpc "$endpoint" && return 0` for
# every grpc/stream profile; when _wait_grpc FAILS the `&&` short-circuits and
# execution falls through into the HTTP curl loop, which references an unset
# `probe_url` under `set -u` and aborts the whole profile with
# `probe_url: unbound variable` (no benchmark ever runs).  Return _wait_grpc's
# own exit code instead so a grpc-wait failure is a clean non-zero the runner
# handles — and a success is a clean 0.  Guarded by grep so it's idempotent and
# shouts if upstream drops the line (then this patch — and the diagnosis behind
# it — needs revisiting).
if grep -q '_wait_grpc "\$endpoint" && return 0' scripts/lib/framework.sh; then
    sed -i 's/_wait_grpc "\$endpoint" && return 0/_wait_grpc "$endpoint"; return $?/' \
        scripts/lib/framework.sh
else
    echo "patch_cpuset.sh: WARN — grpc fall-through line not found in framework.sh (upstream changed?)" >&2
fi
