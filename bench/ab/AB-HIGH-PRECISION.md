# High-precision A/B comparison — methodology & tools

How to measure "did commit X cost anything" accurately enough to *rule out a
regression* or *claim equivalence within ±Δ%*.  Distilled from the
keep-alive-tail refactor verification (commit 6931b77, `a5422e0`..`9c24876`).

The tools this documents, all in this directory: `ab_commit.sh` (HTTP/1.1),
`ab_commit_h2.sh` (HTTP/2) and `ab_commit_ws.sh` (WebSocket) share the
measurement core `abba.sh`; `report.py` turns any of their `raw.tsv` files into
the verdict.  On EC2 they run through `just ab-verify`.

---

## 1. Design principles (what `ab_commit.sh` does)

- **One variable.**  One stack, two code states, everything else fixed.  Only
  the files that actually differ are swapped in place, so both arms load
  through the same venv / editable install.
- **ABBA interleaving inside one session.**  A delta from run A vs run B
  measures the gap *between sessions* as much as between commits.  Interleave
  base/treat within a session, and flip the order each round so one arm never
  owns the cold first slot.
- **Null (A/A) phase in the same session.**  Serve byte-identical code under
  both labels.  Its delta is known to be 0, so whatever it reports is this
  box's resolution floor *for this session* — never recalled from another.
- **Import-hash proof.**  After each swap, import the module and hash the
  file the interpreter actually loaded (`importlib` + sha1), not the path we
  think it used.  This catches editable-install / stale-`__pycache__` swaps.
- **Disjoint pinning.**  Server and wrk load generator on disjoint cores
  (`taskset`).  Unpinned, they fight for the same cores and the distribution
  goes bimodal.

## 2. Known confounds — measure the box before trusting the delta

### 2.1 Bimodality (local desktop boxes) — the #1 trap

On the local box (Ryzen, WSL2), each server restart lands the process in a
**fast or slow state at startup and holds it, ~15 % apart**.  Pinning does
not remove it.  Against a two-mode sample:

- **The median is a coin toss.**  An arm that lands 5/8 fast vs 3/8 fast
  differ by the full mode gap — the median Δ swings ±12 % with the code held
  byte-identical.  `MAD` stays small on a bimodal sample, so a fake delta
  reads as "outside the noise floor".  **Read the mean, never the median.**
- **Detect it:** `report.py` splits a phase at its largest relative gap when
  that gap is ≥ 5 % and reports `hi-mode` (samples in the upper mode) per arm.
- **Handle it:** compare *within* a mode.  `report.py` does so automatically
  on a bimodal sample: Δ per mode, combined by inverse variance, with a
  bootstrap CI.

### 2.2 Monomodality (EC2) — use the POOLED estimator

On EC2 (m7a, dedicated vCPUs, stable frequency) throughput is **monomodal**.
Here a split estimator is the **wrong tool** — cutting one continuous
distribution in two shifts the estimate (a midpoint split once falsely
reported ±0.5 % equivalence on a monomodal sample).  `report.py` therefore
splits only at a real gap (≥ 5 %), and otherwise uses the round-paired
estimator below on all samples.

### 2.3 Round-paired (blocked) estimator

The ABBA design blocks on *round*.  Compute the per-round delta
(mean log treat − mean log base within each round) and average over rounds.
This cancels round-level drift and **nearly halves the SE** vs pooled
(§6) — the best use of the design, at no extra measurement cost.

### 2.4 Outliers / transients

A single transient dip in one run can widen the CI enough to flip an
equivalence verdict.  Run an **endpoint-trim robustness
check** (trim k extremes per arm; k=1..3) and report whether the verdict
survives.  If the *null* phase fails only because of one outlier, the floor
is fine; if the *real* phase fails at every trim level, it is not an outlier
problem — it needs more samples.

### 2.5 Local (WSL2) = screener, EC2 = verdict — the asymmetry

The two boxes do not play the same role.  The local WSL2 box's null floor
sits at or above a ±1 % bound (§6), and a box whose null cannot clear the
bound can never certify equivalence: **local cannot produce an OK.**

Local CAN flag a large regression — a delta larger than 2–3× that day's
floor shows up in the mean — but even the flag is not always conclusive: a
local CI can still cross 0 where EC2 resolves the same change (§6).

Use the harness asymmetrically:

- **Local WSL2** — cheap early screen.  A clear NG (> 2–3× the session's
  null floor) → revert consideration immediately.  Everything else (an OK
  candidate, a borderline NG) is inconclusive by construction: do not burn
  rounds waiting for a local OK, and never trust a local OK at ±1 %.
- **EC2 (m7a.2xlarge)** — the authority for both OK and NG: its null phase
  passes ±1 % (§6), so a verdict from this box is meaningful.

## 3. Statistics — what "no regression" actually means

### 3.1 Framing

- **Detection** — reject "no difference" when a real δ exists.  Power analysis.
- **Equivalence** — *claim* "within ±Δ" by rejecting "|δ| ≥ Δ".  TOST: the
  95 % CI of the delta must fit strictly inside (−Δ, +Δ).
- If the true effect is exactly 0, **no finite N rejects the no-difference
  null** — you can only bound it.  So "prove no regression" = equivalence /
  CI-bounding, not hypothesis testing.

### 3.2 Rounds required (1 round = 4 runs per phase)

The CI half-width shrinks as 1/√rounds.  Measure the box in the session you
are in: run a few rounds, read the null phase's CI half-width *h*, and the
rounds needed for a target half-width *t* are about rounds × (*h* / *t*)².
Equivalence within ±Δ needs *t* comfortably below Δ, since the point
estimate is rarely exactly 0.  `report.py` gives no verdict below four
rounds.  Rounds measured on past boxes are on the issues in §6.

### 3.3 Report format

Always: point estimate + 95 % CI + verdict, never just the median.  State the
null floor alongside.  A real Δ is "resolved" only if it clears the null
phase's own |bias| + SE (or its CI sits outside the null CI).

## 4. EC2 workflow (reproducible + fail-safe)

```bash
REF_BASE=<base> REF_TREAT=<treatment> URL_PATHS=/conn,/plaintext H2_PROFILES=/1kb \
    APPROVE_CLOUD=1 just ab-verify
```

`just ab-verify` is one session (`bench/cloud/session.sh ab`): an
**m7a.2xlarge** (8 vCPU, **no SMT** — each vCPU a physical core, enough for one
worker + `wrk -t4` + the OS; an xlarge's 4 cores are oversubscribed by
`wrk -t4`), a self-shutdown armed for the session's length, the working tree
and its git history deployed, one `ab_commit.sh` per HTTP/1.1 lane and one
`ab_commit_h2.sh` per HTTP/2 lane run detached, then the results pulled and
the instance torn down.  The defaults (8 rounds, 15 s runs, null + real,
server on cores 0–1, load on 2–5) are this document's recipe.  `REF_TREAT`
must be the checked-out tree — that tree is what is deployed.

A session that is still measuring at its wait limit, or loses ssh, keeps the
instance and its results and prints `bash bench/cloud/session.sh ab finish`;
see [bench/cloud/README.md](../cloud/README.md).  The verdict is in
`bench/results/cloud_ab/<run>/report.md`, one section per lane.

## 5. Tools

| Tool | Purpose |
|---|---|
| `abba.sh` | The shared core: commit resolution, in-place swap with import-hash proof, ABBA + null schedule, pinning, `raw.tsv` per round |
| `ab_commit.sh` / `ab_commit_h2.sh` / `ab_commit_ws.sh` | The wrk / h2load / k6 lanes |
| `report.py` | Verdict: round-paired Δ (per mode when bimodal), bootstrap 95 % CI, null floor, equivalence within `--equiv` |

## 6. Calibration evidence

The numbers behind this method live on their issues, not here:

- **BLA-163** — the 6931b77 no-op verification on EC2: monomodal, CV ~0.6–0.7 %,
  ±1 % equivalent at 12 rounds, ±0.5 % not claimable; the paired estimator's SE
  against the pooled one.
- **BLA-543** — local WSL2 against EC2 on the same changes, the rounds table,
  and a two-`await` extraction caught at −3 % and reverted.
- **BLA-542** — the first run of `just ab-verify` through `bench/cloud/session.sh`.

## 7. Verdict rules

A regression (the change being *slower*) is essentially ruled out when:

1. The real Δ point estimate is ≥ 0 or its 95 % CI is not below 0, **and**
2. The real CI is inside the chosen equivalence bound (or at least inside
   ±1 %), **and**
3. The bytecode/hot-path analysis shows the measured path executes the same
   instructions (the deterministic proof the benchmark can only approximate).

A ±0.5 % equivalence claim specifically needs the real-phase 95 % CI inside
±0.5 % **and** the null phase to also pass (after outlier trim).  If the CI
edge sits just outside the bound because the point estimate is not 0, more
rounds (not outlier removal) are the only path.

4. **Regression vs. no-resolve — read the null phase first.**  When the
   null (A/A) phase passes equivalence but the real CI **exceeds** the
   bound, the change is a real regression: revert or fix, do not re-measure.
   When the null phase *fails* equivalence, the box cannot resolve ±Δ that
   session — escalate or add rounds before trusting any real-phase verdict.
