#!/usr/bin/env bash
# Patch HttpArena cpusets for small instances (c7i.2xlarge: 8 vCPUs, no SMT).
# Run AFTER `git clone` on the EC2 instance.
# Idempotent — safe to run repeatedly on the same clone.
set -euo pipefail
cd ~/HttpArena
# redis.sh: default REDIS_CPUSET
sed -i 's/-0,64}/-0,2}/' scripts/lib/redis.sh
# benchmark.sh: hardcoded Redis export for all profiles
sed -i 's/export REDIS_CPUSET="0,64"/export REDIS_CPUSET="0,2"/' scripts/benchmark.sh
# benchmark.sh: CRUD profile gcannon cpuset (32-63,96-127 → 7 for 8 vCPUs)
sed -i 's/export GCANNON_CPUS="32-63,96-127"/export GCANNON_CPUS="7"/' scripts/benchmark.sh
