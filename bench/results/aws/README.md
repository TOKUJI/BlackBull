# AWS measurement archive (Sprint 13+)

Results from `bench/aws/run.sh`.  Each `<TIMESTAMP>/` directory holds
one full or partial pass: a `run.log` (tee'd remote stdout) and a
`results/` subtree fetched from the EC2 box.

For the canonical Sprint 13 numbers and WSL→EC2 delta analysis, see
[`bench/CHARACTERIZATION.md`](../../CHARACTERIZATION.md) §
"EC2 baseline of record (Sprint 13, 2026-05-23)".

## Run inventory

| Timestamp | Status | Notes |
|---|---|---|
| `20260523-064323Z/` | partial (archaeology only) | oha not installed on remote (apt+cargo fallback only, no GitHub-releases path yet); nginx omitted from STACKS.  Lane B-oha shows `err` for every row.  Kept as the *before* side of the install fix. |
| `20260523-095507Z/` | **canonical baseline** | First complete pass: 5 ASGI stacks + nginx, all five lanes.  oha works.  Caveat: nginx Lane C `err%=100 %` because nginx.conf lacked `/ping` (k6 stress script default target).  Throughput numbers in that single block are still real (nginx serving 404s); use the 111726Z values for nginx Lane C. |
| `20260523-111726Z/` | nginx Lane C re-fix | Targeted re-run after `/ping` was added to `bench/peers/nginx.conf`.  Only `STACKS=nginx LANES=C`.  Use these two rows when citing nginx C1/C2. |

## How to add another run

```bash
bash bench/aws/up.sh && \
  bash bench/aws/install.sh && \
  bash bench/aws/run.sh && \
  bash bench/aws/down.sh
```

`run.sh` writes a new `bench/results/aws/<TS>Z/` directory on the
local box and tees the remote stdout.  Append a row to the inventory
table above so future readers can tell what each timestamp represents.

For a partial / scoped pass, override `STACKS` and/or `LANES` before
calling `bench/aws/run.sh` — see comments in that script.
