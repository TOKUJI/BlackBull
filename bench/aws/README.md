# bench/aws — single-instance EC2 benchmark harness

Sprint 13 deliverable.  Brings up one `c7i.xlarge` in `us-east-1`,
runs the full five-peer comparison from
[bench/peers/compare_servers.sh](../peers/compare_servers.sh), scp's
results back, and tears everything down.

Mirrors the WSL2 setup (server and load-gen on the same host, loopback
only) so that WSL → EC2 deltas reflect WSL overhead, not topology
changes.

## Prerequisites

- `aws configure` with credentials that can `ec2:Run/Terminate/Describe*`,
  `ec2:*KeyPair`, `ec2:*SecurityGroup`.  An IAM user dedicated to bench
  runs is recommended.
- `aws`, `ssh`, `rsync`, `jq`, `curl` on the local machine.
- Outbound port 22 from this machine (for SSH).  The instance's SG only
  permits ingress 22/tcp from this host's current public IP.

## Usage

```bash
# Up — provisions key pair, security group, instance.  ~30 s.
bash bench/aws/up.sh

# Install — rsyncs the repo, installs Python deps + bench tooling,
#           smoke-tests with pytest.  ~5 min (mostly apt + pip).
bash bench/aws/install.sh

# Run — executes compare_servers.sh on the remote and scp's results back
#       to bench/results/aws/<TIMESTAMP_UTC>/.  ~45–60 min.
bash bench/aws/run.sh

# Down — terminates instance, deletes SG + key pair, verifies clean.
bash bench/aws/down.sh
```

Override the matrix without editing scripts:

```bash
LANES="A B-wrk" STACKS="blackbull granian" bash bench/aws/run.sh
```

## What gets created (and what `down.sh` removes)

| Resource           | Default name         | Removed by  |
|--------------------|----------------------|-------------|
| Key pair           | `blackbull-bench-key`| `down.sh`   |
| Security group     | `blackbull-bench-sg` | `down.sh`   |
| EC2 instance       | (tagged `Project=BlackBull-bench`) | `down.sh` |
| Local SSH key file | `bench/aws/blackbull-bench-key.pem` | `down.sh` |
| Local state file   | `bench/aws/.state`   | `down.sh`   |
| Local known_hosts  | `bench/aws/.known_hosts` | `down.sh` |

EBS root volume has `DeleteOnTermination=true`, so it goes when the
instance does — no separate cleanup step.

## Leak check (paranoid mode)

`down.sh` runs these three queries automatically after deletion and
fails loudly if any return a non-empty result.  Run them by hand any
time you want to verify nothing was left behind:

```bash
aws ec2 describe-instances --region us-east-1 \
    --filters "Name=tag:Project,Values=BlackBull-bench" \
              "Name=instance-state-name,Values=pending,running,stopping,stopped" \
    --query 'Reservations[].Instances[].[InstanceId,State.Name]' --output table

aws ec2 describe-security-groups --region us-east-1 \
    --filters "Name=group-name,Values=blackbull-bench-sg" \
    --query 'SecurityGroups[].GroupId' --output text

aws ec2 describe-key-pairs --region us-east-1 \
    --filters "Name=key-name,Values=blackbull-bench-key" \
    --query 'KeyPairs[].KeyName' --output text
```

## Cost envelope

c7i.xlarge in us-east-1: $0.1712/hr on-demand (as of 2026-05).
A full pass (up + install + run + down) takes ~70 min wall, so
**roughly $0.20 per run** including a margin for retries.  The full
Sprint 13 measurement set, including a couple of repeats, should stay
under $1.

## Overrides (env vars consumed by `config.sh`)

| Variable        | Default                | Notes |
|-----------------|------------------------|-------|
| `REGION`        | `us-east-1`            | |
| `INSTANCE_TYPE` | `c7i.xlarge`           | x86_64, Sapphire Rapids — matches WSL2 ISA so the WSL→EC2 delta isn't ISA-confounded. |
| `KEY_NAME`      | `blackbull-bench-key`  | Override if you run concurrent passes. |
| `SG_NAME`       | `blackbull-bench-sg`   | Same. |
| `VOLUME_SIZE_GB`| `20`                   | Root EBS, gp3. |
| `AWS_PROFILE`   | (from aws CLI default) | Standard aws CLI selection. |

## Notes for future iteration

- Two-instance topology (load-gen on a separate EC2) is deferred to
  Sprint 14.  If the Sprint 13 results suggest load-gen contention is
  meaningfully distorting numbers (look for k6 CPU pegging in
  `compare_servers_*.md`), bring it forward.
- Multi-worker BlackBull (`BB_WORKERS=4`) is not exercised by Sprint 13
  — single-worker keeps parity with the WSL baseline.  Add a
  `--workers N` variant once the single-worker absolute baseline is
  established.
