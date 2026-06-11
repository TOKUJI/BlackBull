# Sprint 37 Phase B — EC2 c7i.8xlarge reproduction

Two clean runs on c7i.8xlarge to confirm the HttpArena profile sweep
reproduces within noise.  Per the
[sprint-37 plan](../../sprint-logs/sprint-37.md) workstream 3.

## Smoke first, then measure

Before spending c7i.8xlarge dollars on a real measurement run, do
one c7i.xlarge smoke run (~$0.18/hr vs $1.43/hr — 8× cheaper) to
debug the script chain end-to-end.  The smoke run's goal is
**validate.sh passes + benchmark.sh completes**, not benchmark
numbers — c7i.xlarge has 4 vCPUs so the harness's `0-31,64-95`
cpuset will clamp to "all cores" and r/s will be modest.  That's
fine for proving the orchestrator + remote scripts work; flip to
c7i.8xlarge once the smoke run is green.

```bash
# Smoke on c7i.xlarge first.
INSTANCE_TYPE=c7i.xlarge TOPO=single bash bench/aws/up.sh
bash bench/aws/install.sh    # BlackBull bench harness — NOT Docker
bash bench/httparena/sprint37/sprint37_drive.sh smoke
#   ↑ drive.sh internally uploads + execs sprint37_install_docker.sh
#     (SSH session 1) and sprint37_remote.sh (SSH session 2).
bash bench/aws/down.sh
```

If the smoke run completes without script-level errors, then:

## Workflow

```bash
# Once: clone HttpArena and vendor BlackBull locally.
git clone https://github.com/MDA2AV/HttpArena.git ~/work/HttpArena
cp -r bench/httparena/* ~/work/HttpArena/frameworks/blackbull/
# Drop the sprint-specific dev files from the vendor (validate.sh
# does not need them and they don't ship upstream).
( cd ~/work/HttpArena/frameworks/blackbull && \
  rm -rf __pycache__ _local *.whl USAGE.md Dockerfile.dev monitor.sh \
         postprocess.sh runner.sh patch_httparena.py sprint* logging_access.ini )
# Flip the VENDORED meta.json's enabled flag to true; leave the
# repo-local meta.json at enabled:false until merge.
sed -i 's/"enabled": false/"enabled": true/' \
    ~/work/HttpArena/frameworks/blackbull/meta.json

# Once per run: provision EC2, run, tear down.
# sprint37_drive.sh internally uploads + execs
# sprint37_install_docker.sh (SSH session 1) and
# sprint37_remote.sh (SSH session 2).
INSTANCE_TYPE=c7i.8xlarge TOPO=single bash bench/aws/up.sh
bash bench/aws/install.sh    # BlackBull bench harness — NOT Docker
bash bench/httparena/sprint37/sprint37_drive.sh run1
bash bench/aws/down.sh

# Re-up for the second clean run (re-using the same instance after a
# benchmark sweep contaminates page cache / kernel state more than is
# worth — fresh instance is the cleaner reproducibility test).
INSTANCE_TYPE=c7i.8xlarge TOPO=single bash bench/aws/up.sh
bash bench/aws/install.sh    # BlackBull bench harness — NOT Docker
bash bench/httparena/sprint37/sprint37_drive.sh run2
bash bench/aws/down.sh
```

Results land at `bench/results/httparena/sprint37-run{1,2}-<ts>/`.
Phase C diffs Run #1 vs Run #2 and decides whether to ship.

## Expected cost

c7i.8xlarge at ~$1.45/hr × ~2 hr per run × 2 runs ≈ **$6** plus
EBS/data-transfer pennies.  Set a $20 cap if validate.sh failures
cascade into multiple re-up cycles.

## Why this scripted shape

Per the `feedback_remote_scripts_via_upload` memory: write remote
scripts as local files, upload, then exec with args.  Don't embed
logic inside `ssh "..."` heredocs — escaping and backslash handling
introduces unnecessary retries.

`sprint37_drive.sh` runs locally — sources `bench/aws/config.sh`
to pick up the canonical `SSH_OPTS` / `SSH_USER` / `LOCAL_KEY` set
(the same interface every `bench/aws/*.sh` script uses), then:

1. rsyncs the local HttpArena tree to the instance,
2. uploads + execs `sprint37_install_docker.sh` (SSH session #1),
3. uploads + execs `sprint37_remote.sh` (SSH session #2 — a
   fresh login so the `usermod -aG docker` from step 2 is in
   effect),
4. rsyncs results back.

`sprint37_install_docker.sh` runs on the instance — installs
Docker Engine + adds the `ubuntu` user to the docker group.
Idempotent.  Required because `bench/aws/install.sh` sets up
BlackBull's bench harness (native Python), not Docker, and
HttpArena's framework images need Docker.

`sprint37_remote.sh` runs on the instance — sets
`LOADGEN_DOCKER=true`, builds the framework image, runs
validate.sh, iterates `scripts/benchmark.sh` over every profile
claimed in `meta.json["tests"]`, sanity-checks that every produced
result JSON reports `rps > 0` (catches silent loadgen failures),
snapshots system info, tarballs the output dir.

### Why `LOADGEN_DOCKER=true`?

`bench/aws/install.sh` installs `h2load` and `wrk` natively, but
not `gcannon` (HttpArena's io_uring HTTP/1.1 loadgen — needs
liburing 2.9 + a source build).  Most BlackBull profiles route
through gcannon (baseline, pipelined, limited-conn, json,
json-comp, upload, echo-ws), so a missing gcannon makes
`benchmark.sh` silently write `rps=0` and continue.

`LOADGEN_DOCKER=true` is HttpArena's documented escape hatch:
`benchmark.sh` falls back to the same docker images
`benchmark-lite.sh` uses, and benchmark.sh builds whichever loadgen
images are missing on first invocation.  Methodology note: docker
loadgens add one IPC hop relative to native binaries, so absolute
numbers won't exactly match the leaderboard (where MDA2AV runs
native loadgens).  For our purposes — Run #1 vs Run #2
reproducibility + framework integration — this is fine.  The
maintainer re-runs benchmarks on his own hardware when accepting
submissions, so absolute leaderboard parity isn't our deliverable.

## What we capture per run

- `validate.log` — full validate.sh output.
- `bench-<profile>.log` — full benchmark.sh output per profile.
- `results/` — copies of the JSON files HttpArena's harness writes.
- `system.txt` — uname / nproc / meminfo / docker info / ulimit /
  git head.  Makes the run self-describing if we revisit it months
  later.
- `remote.log` — tee'd shell trace of the whole remote pass.

## Verifying we have a clean run

Phase C diff criteria:

- Per (profile, conns) cell, r/s within ±5 % across run1 and run2.
- No profile that passed in run1 fails in run2 (or vice versa).
- validate.sh green on both runs.

If divergence is wider than ±5 %, **stop and investigate** before
flipping `meta.json` enabled:true on the repo side.  The Sprint 35
cautions show the static profile can present bimodal r/s under
small samples — pin loadgen CPUs per the Sprint 34 caution and
re-run if necessary.
