# Sprint 37 Phase B — EC2 c7i.8xlarge reproduction

Two clean runs on c7i.8xlarge to confirm the HttpArena profile sweep
reproduces within noise.  Per the
[sprint-37 plan](../../sprint-logs/sprint-37.md) workstream 3.

## Smoke first, then measure

Before spending c7i.8xlarge dollars on a real measurement run, do
one cheap-tier validation run on c7i.xlarge with a 1-2 profile
subset.  Goal: **validate.sh passes + benchmark.sh completes +
trap-on-EXIT pulls results back even on failure** — not benchmark
numbers.  c7i.xlarge has 4 vCPUs so r/s will be modest; that's
fine for proving the orchestrator end-to-end.

The orchestrator accepts an optional 2nd positional arg —
comma-separated profile subset.  Empty / omitted = use the full
list from `meta.json["tests"]`.

```bash
# Cheap-tier validation: c7i.xlarge + 2 profiles (~10 min, ~$0.04).
# sprint37_drive.sh handles vendoring (wipe + re-clone HttpArena +
# copy bench/httparena/ on top + strip dev files + flip enabled).
# Set SKIP_VENDOR=1 to reuse an existing ~/work/HttpArena tree.
INSTANCE_TYPE=c7i.xlarge TOPO=single bash bench/aws/up.sh
# bench/aws/install.sh is OPTIONAL for the HttpArena path — it sets
# up BlackBull's native bench harness (h2load + wrk + wrk2 + …),
# but sprint37_remote.sh sets LOADGEN_DOCKER=true so HttpArena's
# docker loadgens cover everything we need.  Skipping install.sh
# saves ~5 min per run.  Docker itself is installed by
# sprint37_drive.sh regardless (SSH session 1 runs
# sprint37_install_docker.sh).
bash bench/httparena/sprint37/sprint37_drive.sh smoke baseline,static
#   ↑ 2nd arg = profile subset; benchmark.sh only on the listed
#     subset.  Empty / omitted = use the full meta.json["tests"]
#     list (current "real" run shape).  validate.sh always runs
#     against every claimed profile regardless.
bash bench/aws/down.sh
```

If the smoke run completes cleanly (sanity log shows non-zero rps
on both profiles, JSON count >= 2), the orchestrator is proven —
then the full measurement run:

## Workflow

Vendoring (wipe `~/work/HttpArena`, re-clone upstream, copy
`bench/httparena/` on top, strip sprint-specific dev files, flip
the vendored `meta.json` `enabled:true`) is automatic inside
`sprint37_drive.sh`.  No pre-step required.  Set `SKIP_VENDOR=1`
to reuse the existing tree if you've hand-edited it for a
one-off.

```bash
# Once per run: provision EC2, run, tear down.
# sprint37_drive.sh internally:
#   0. wipes + re-clones ~/work/HttpArena + vendors bench/httparena/
#   1. rsyncs the tree to the instance
#   2. uploads + execs sprint37_install_docker.sh (SSH session 1)
#   3. uploads + execs sprint37_remote.sh (SSH session 2)
#   4. rsyncs results back (EXIT trap — runs even on failure)
INSTANCE_TYPE=c7i.8xlarge TOPO=single bash bench/aws/up.sh
bash bench/aws/install.sh    # OPTIONAL — BlackBull bench harness, NOT Docker
bash bench/httparena/sprint37/sprint37_drive.sh run1
bash bench/aws/down.sh

# Re-up for the second clean run (re-using the same instance after a
# benchmark sweep contaminates page cache / kernel state more than is
# worth — fresh instance is the cleaner reproducibility test).
INSTANCE_TYPE=c7i.8xlarge TOPO=single bash bench/aws/up.sh
bash bench/aws/install.sh    # OPTIONAL — BlackBull bench harness, NOT Docker
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
