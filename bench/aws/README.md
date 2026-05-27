# bench/aws — EC2 benchmark harness

Brings up one or two EC2 instances in `us-east-1`, runs the full
peer comparison from
[bench/peers/compare_servers.sh](../peers/compare_servers.sh), scp's
results back, and tears everything down.

Two topologies (controlled by `TOPO`):

| `TOPO`   | Hosts | Topology | Sprint introduced | Compared with |
|----------|-------|----------|-------------------|---------------|
| `single` (default) | One `c7i.xlarge` runs both the server under test and the load tools, loopback only | 13 | WSL2 baseline (same shape) |
| `split` | One `c7i.xlarge` server + one `c7i.2xlarge` load generator in a cluster placement group, VPC private networking | 20 | Sprint 13–16 single-host numbers (with topology delta documented) |

`single` mirrors the WSL2 setup (server and load-gen on the same
host) so WSL → EC2 deltas reflect WSL overhead, not topology
changes.  `split` removes the load-gen-on-server CPU contention
Sprint 16 surfaced (`+17 %` at `w=4`) at the cost of injecting
~150–300 µs of VPC RTT — direct latency comparisons across
topologies should subtract that floor.

## Prerequisites

- Working AWS credentials in the standard CLI resolution chain.  The
  scripts call `aws sts get-caller-identity` as a preflight, so any
  authenticated session works — pick whichever fits your account:
  - **`aws login`** *(recommended)* — IAM Identity Center / Builder ID
    SSO.  Short-lived auto-expiring session tokens, no static secret
    on disk.  Re-run if the session expires mid-pass (rare — a full
    pass is ~70 min, comfortably inside a default 1-hour session).
  - `aws sso login --profile <name>` — same mechanism if you have a
    named SSO profile configured.
  - `aws configure` — long-lived static IAM keys.  Works but
    discouraged for human users; fine for a dedicated bench IAM user.
- IAM permissions: `ec2:Run/Terminate/Describe*`, `ec2:*KeyPair`,
  `ec2:*SecurityGroup*`.  Any role/policy granting these (e.g.
  `AmazonEC2FullAccess`) suffices.
- `aws`, `ssh`, `rsync`, `jq`, `curl` on the local machine.
- Outbound port 22 from this machine (for SSH).  The instance's SG only
  permits ingress 22/tcp from this host's current public IP.

## Usage

Single-host (default; legacy):

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

Split-topology (Sprint 20):

```bash
TOPO=split bash bench/aws/up.sh        # 2 instances + cluster placement group
TOPO=split bash bench/aws/install.sh   # both hosts; cert SAN regen; /etc/hosts wiring
TOPO=split bash bench/aws/run.sh       # orchestrator runs on loadgen, drives server over SSH
TOPO=split bash bench/aws/down.sh      # both instances + placement group + SG + key pair
```

(`TOPO` is also persisted to `bench/aws/.state` by `up.sh`, so the
subsequent `install.sh` / `run.sh` / `down.sh` calls don't strictly
need it on the command line — but passing it consistently keeps
the intent explicit.)

Override the matrix without editing scripts:

```bash
LANES="A B-wrk" STACKS="blackbull granian" bash bench/aws/run.sh
```

## What gets created (and what `down.sh` removes)

| Resource           | Default name         | Created when    | Removed by  |
|--------------------|----------------------|-----------------|-------------|
| Key pair           | `blackbull-bench-key`| always          | `down.sh`   |
| Security group     | `blackbull-bench-sg` | always          | `down.sh`   |
| EC2 server         | tagged `Role=server`  | always          | `down.sh`   |
| EC2 loadgen        | tagged `Role=loadgen` | `TOPO=split` only | `down.sh`   |
| Cluster placement group | `blackbull-bench-cpg` | `TOPO=split` only | `down.sh`   |
| Intra-SG TCP 8000-9100 ingress | (anonymous)  | `TOPO=split` only | `down.sh` (with SG) |
| Local SSH key file | `bench/aws/blackbull-bench-key.pem` | always | `down.sh` |
| Local state file   | `bench/aws/.state`   | always          | `down.sh`   |
| Local known_hosts  | `bench/aws/.known_hosts` | always      | `down.sh`   |

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

`c7i.xlarge` in us-east-1: $0.1712/hr on-demand (as of 2026-05).
`c7i.2xlarge`: $0.3424/hr.  A full pass (up + install + run + down)
takes ~70 min wall.

| `TOPO`   | Combined $/hr | Per-run cost | Sprint matrix budget (incl. retries) |
|----------|---------------|--------------|--------------------------------------|
| `single` | $0.17         | ~$0.20       | < $1 |
| `split`  | $0.51         | ~$0.65       | < $2 |

## Overrides (env vars consumed by `config.sh`)

| Variable                 | Default                  | Notes |
|--------------------------|--------------------------|-------|
| `REGION`                 | `us-east-1`              | |
| `INSTANCE_TYPE`          | `c7i.xlarge`             | Server.  x86_64, Sapphire Rapids — matches WSL2 ISA so the WSL→EC2 delta isn't ISA-confounded. |
| `LOADGEN_INSTANCE_TYPE`  | `c7i.2xlarge`            | Load generator (`TOPO=split` only).  2× server vCPU so the generator is never the bottleneck. |
| `TOPO`                   | `single`                 | `single` (server+loadgen on one host) or `split` (two hosts, cluster placement group). |
| `PLACEMENT_GROUP_NAME`   | `blackbull-bench-cpg`    | `TOPO=split` only.  Cluster strategy. |
| `SERVER_DNS_NAME`        | `bench-server.internal`  | DNS name the loadgen uses for the server.  Lives in `/etc/hosts` + cert SAN. |
| `KEY_NAME`               | `blackbull-bench-key`    | Override if you run concurrent passes. |
| `SG_NAME`                | `blackbull-bench-sg`     | Same. |
| `VOLUME_SIZE_GB`         | `20`                     | Root EBS, gp3. |
| `AWS_PROFILE`            | (from aws CLI default)   | Standard aws CLI selection. |

## Notes

- In `TOPO=split` mode, the server's `tests/cert.pem` is **regenerated
  in place** at install time so its SAN list covers
  `bench-server.internal`, `localhost`, `127.0.0.1`, and the server's
  VPC private IP.  The regenerated cert is then deployed to the
  loadgen and trusted in its CA store; both instances also drop a
  `bench-server.internal → <server-private-ip>` line into
  `/etc/hosts`.  No load tool runs with `--insecure` — the existing
  TLS path keeps working unchanged.
- Multi-worker BlackBull (`BB_WORKERS=4`) is exercised through
  `STACKS="blackbull-w2 blackbull-w4 …"` after Sprint 16 added the
  `-w<N>` suffix.  `TOPO=split` is where the multi-worker scaling
  story can be measured cleanly.
