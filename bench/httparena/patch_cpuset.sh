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
# On the small instances the vCPUs are a contiguous 0..V-1 range (the sibling
# half collapses away), so the reference's half/half split becomes simply:
#
#     server    = 0 .. V/2-1        load-gen = V/2 .. V-1        redis = core 0
#
# which is exactly the reference's 0-63 primary span divided by D = 64/V:
#
#     c7i.2xlarge  V=8   D=8     server 0-3    load-gen 4-7
#     c7i.4xlarge  V=16  D=4     server 0-7    load-gen 8-15
#     c7i.6xlarge  V=24  D=8/3   server 0-11   load-gen 12-23
#     c7i.8xlarge  V=32  D=2     server 0-15   load-gen 16-31
#
# Computed from nproc so it self-adjusts to whichever instance we launch on.
#
# NOTE: pinning the server off the load-gen cores is a real STEADY-STATE win
# (≈4-6×: runs 2-3 went 356k/255k → ~1550k/1550k msg/s once the server stopped
# contending with the load generator).  It does NOT cure the run-1 cold-burst
# collapse — run 1 still returns near-total Unavailable even pinned — so that
# cold-start failure is a separate problem, not CPU contention.
V=$(nproc)                              # total vCPUs on this box
H=$(( V / 2 ))                          # half-way point
SERVER_CPUS="0-$(( H - 1 ))"            # server under test = lower half
LOADGEN_CPUS="${H}-$(( V - 1 ))"        # load generator     = upper half
REDIS_CPUS="0"                          # redis co-locates on the server's core 0
echo "patch_cpuset.sh: V=$V  server=$SERVER_CPUS  load-gen=$LOADGEN_CPUS  redis=$REDIS_CPUS" >&2

# redis.sh: default REDIS_CPUSET (0,64 → server core 0)
sed -i "s/-0,64}/-${REDIS_CPUS}}/" scripts/lib/redis.sh
# benchmark.sh: hardcoded Redis export for all profiles (0,64 → server core 0)
sed -i "s/export REDIS_CPUSET=\"0,64\"/export REDIS_CPUSET=\"${REDIS_CPUS}\"/" scripts/benchmark.sh
# benchmark.sh: CRUD gcannon load-gen cpuset (32-63,96-127 → upper half)
sed -i "s/export GCANNON_CPUS=\"32-63,96-127\"/export GCANNON_CPUS=\"${LOADGEN_CPUS}\"/" scripts/benchmark.sh
# profiles.sh: server-streaming server cpuset (0-31,64-95 → lower half; load-gen gets WRK_CPUS=$LOADGEN_CPUS)
sed -i "/\[stream-grpc\]=/s|0-31,64-95|${SERVER_CPUS}|" scripts/lib/profiles.sh
sed -i "/\[stream-grpc-tls\]=/s|0-31,64-95|${SERVER_CPUS}|" scripts/lib/profiles.sh

# profiles.sh: api-4 / api-16 are FIXED cpu-BUDGET profiles — the whole point is
# measuring efficiency under a hard 4- and 16-logical-CPU cap.  Upstream encodes
# them as reference-host cpusets (0-1,64-65 and 0-7,64-71) whose sibling cores
# (64+) don't exist on our box, so upstream's "exceeds available CPUs — using all
# cores" fallback SILENTLY DROPS THE CAP and the server runs on all vCPUs,
# defeating the profile (api-4 ≈ api-16 ≈ unconstrained instead of a real cliff).
# These budgets are ABSOLUTE — they must NOT scale with the box — so map each to
# the same COUNT of contiguous low vCPUs (server 0-3 / 0-15; load-gen 16-31 on
# c7i.8xlarge).  Needs V >= 16 for api-16's cap plus load-gen headroom.
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
