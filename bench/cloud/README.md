# bench/cloud — measurements on EC2

One entry point runs a whole cloud measurement as one session: provision,
arm a self-shutdown, install, run the job detached on the instance, wait,
pull the results, tear down, verify the teardown.

```bash
just ab-verify          # A/B of two commits        (jobs/ab.sh)
just httparena-bench    # HttpArena's own benchmark (jobs/httparena.sh)
bash bench/cloud/session.sh compare   # peer servers (jobs/compare.sh)
```

The `just` recipes refuse to start without `APPROVE_CLOUD=1`: every session
costs money.  Each job's header lists its settings and their defaults; the
defaults are the documented recipe, so `REF_BASE=… REF_TREAT=… just ab-verify`
is a complete A/B (method: [AB-HIGH-PRECISION.md](../ab/AB-HIGH-PRECISION.md)).

## What a session guarantees

- **The measurement outlives the connection.**  The job's runner is detached on
  the instance; the session only polls it.  A failed poll is retried — the
  session gives up only after `CLOUD_SSH_FAILURES` (40) failures in a row.
- **The wait fits the work.**  The ab job derives its wait limit from rounds ×
  phases × lanes × run length; others default to three hours
  (`CLOUD_WAIT_SECONDS` overrides).  The instance's self-shutdown is armed
  for the whole session's length right after provisioning.
- **Results are never thrown away.**  Whatever the instance has is pulled
  whenever the session stops.  A failed runner is torn down after the pull.
  A runner still measuring at the wait limit, or an instance that stopped
  answering, is *kept*: the session prints the resume command
  (`bash bench/cloud/session.sh <job> finish`) and the time the instance
  terminates itself.  An interrupted session keeps it too.
- **Complete means verified.**  `complete.json` appears in the run directory
  only after the job's results check passes *and* `down.sh` proved that no
  tagged resource remains.

Results land in `bench/results/cloud_<job>/<UTC>-<rev>/` with the session log;
runtime state (the up.sh snapshot, the open session, the key, known hosts) in
`bench/.state/`.  A second session refuses to start while a state exists.

## Jobs

| job | runs | instance |
|---|---|---|
| `ab` | `bench/ab/ab_commit.sh` per HTTP/1.1 lane, `ab_commit_h2.sh` per HTTP/2 lane | `m7a.2xlarge` |
| `compare` | `bench/compare/compare_servers.sh` | `m7a.2xlarge`; `TOPO=split` adds a load generator |
| `httparena` | `bench/httparena/run_httparena.sh` after preparing Docker and HttpArena | `c7a.2xlarge` |

A job is a file in `jobs/` defining `job_preflight`, `job_runner`,
`job_collect` and optionally `job_setup` / `job_on_poll` — see
[session.sh](session.sh).  The ab job deploys the git history (as a bundle,
so it works from a worktree) and refuses a `REF_TREAT` that is not the
deployed working tree.

## Prerequisites

- AWS credentials in the CLI chain (`aws login` recommended); the scripts
  check `aws sts get-caller-identity` first.  IAM: `ec2:Run/Terminate/Describe*`,
  `ec2:*KeyPair`, `ec2:*SecurityGroup*`.
- `aws`, `ssh`, `rsync`, `jq`, `curl` locally, and outbound port 22.  The
  security group admits 22/tcp from this host's public IP only.

## What gets created, and what `down.sh` removes

| Resource | Name | When |
|---|---|---|
| Key pair | `blackbull-bench-key` | always |
| Security group | `blackbull-bench-sg` | always |
| Server instance | tagged `Role=server` | always |
| Load-generator instance, cluster placement group, intra-SG 8000–9100 | `Role=loadgen`, `blackbull-bench-cpg` | `TOPO=split` |
| Local key, state, known hosts | `bench/.state/` | always |

`down.sh` verifies nothing tagged remains before it removes the local state
and writes the teardown proof; a failed verification keeps the state for a
retry.  EBS volumes are deleted with their instance.

## Settings (`config.sh`)

`REGION` (`us-east-1`), `INSTANCE_TYPE` (`m7a.2xlarge`: 8 physical cores, no
SMT), `LOADGEN_INSTANCE_TYPE` (`c7i.2xlarge`), `TOPO` (`single` | `split`),
`KEY_NAME`, `SG_NAME`, `VOLUME_SIZE_GB`, `AWS_PROFILE`.

In `TOPO=split` the server's `tests/cert.pem` is regenerated with
`bench-server.internal` and its private IP as SANs, trusted on the load
generator, and mapped in `/etc/hosts`; no load tool runs with `--insecure`.
The split adds ~150–300 µs of VPC round trip, so latencies are not comparable
across topologies.
