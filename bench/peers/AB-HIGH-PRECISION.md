# High-precision A/B comparison — methodology & tools

How to measure "did commit X cost anything" accurately enough to *rule out a
regression* or *claim equivalence within ±Δ%*.  Distilled from the
keep-alive-tail refactor verification (commit 6931b77, `a5422e0`..`9c24876`).

The tools this documents: `ab_commit.sh` (measurement), `ab_report.py`
(standard report), `ab_equiv_report.py` (equivalence verdict).  All in this
directory.

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
- **Detect it:** `ab_report.py`'s `hi-mode` column (fraction of samples above
  the range midpoint).  Neither 0/n nor n/n ⇒ mixed ⇒ bimodal.
- **Handle it:** compare *within* a mode.  Either per-mode, or the combined
  estimator in `ab_equiv_report.py` (inverse-variance weighted delta across
  modes, Satterthwaite df).

### 2.2 Monomodality (EC2) — use the POOLED estimator

On EC2 (m7a, dedicated vCPUs, stable frequency) throughput is **monomodal**.
Here the bimodal split/combined estimator is the **wrong tool** — the
midpoint heuristic cuts one continuous distribution in two and shifts the
estimate (it falsely reported ±0.5 % equivalence on a monomodal sample).  For
a monomodal box, use a **pooled two-sample TOST** (or the round-paired
estimator below) on all samples.  Rule: split/combined only when `hi-mode`
shows true ~15 % separated clusters.

### 2.3 Round-paired (blocked) estimator

The ABBA design blocks on *round*.  Compute the per-round delta
(mean log treat − mean log base within each round) and average over rounds.
This cancels round-level drift and **nearly halves the SE** vs pooled
(0.127 vs 0.197 on the EC2 run) — the best use of the design, at no extra
measurement cost.

### 2.4 Outliers / transients

A single transient dip (e.g. −7.4 % in one null-phase run) can widen the CI
enough to flip an equivalence verdict.  Run an **endpoint-trim robustness
check** (trim k extremes per arm; k=1..3) and report whether the verdict
survives.  If the *null* phase fails only because of one outlier, the floor
is fine; if the *real* phase fails at every trim level, it is not an outlier
problem — it needs more samples.

## 3. Statistics — what "no regression" actually means

### 3.1 Framing

- **Detection** — reject "no difference" when a real δ exists.  Power analysis.
- **Equivalence** — *claim* "within ±Δ" by rejecting "|δ| ≥ Δ".  TOST: the
  95 % CI of the delta must fit strictly inside (−Δ, +Δ).
- If the true effect is exactly 0, **no finite N rejects the no-difference
  null** — you can only bound it.  So "prove no regression" = equivalence /
  CI-bounding, not hypothesis testing.

### 3.2 Rounds required (1 round = 4 wrk runs ≈ 100 s)

Measured locally (bimodal, within-mode CV 0.3–0.5 %):

| Goal | Rounds |
|---|---|
| Detect 1 % | ~4–5 |
| Detect 0.5 % | ~8–16 |
| Detect 0.2 % | ~30–80 |
| Equivalence ±1 % | ~3–4 |
| Equivalence ±0.5 % (combined / both-modes) | ~5–12 / ~12–32 |

EC2 single-worker CV measured ~0.6–0.7 % (higher than local within-mode), so
±0.5 % equivalence needs ~20–24 rounds there.  **Measure the box's actual
CV from the null phase before committing to a round count.**

### 3.3 Report format

Always: point estimate + 95 % CI + verdict, never just the median.  State the
null floor alongside.  A real Δ is "resolved" only if it clears the null
phase's own |bias| + SE (or its CI sits outside the null CI).

## 4. EC2 workflow (reproducible + fail-safe)

Instance lifecycle via `bench/aws/up.sh` / `install.sh` / `down.sh`, but
`ab_commit.sh` needs extra provisioning:

1. **Instance:** `INSTANCE_TYPE=m7a.2xlarge TOPO=single bash bench/aws/up.sh`
   (8 vCPU, **no SMT** — each vCPU is a physical core; 8 cores fits 1 worker
   + `wrk -t4` + OS.  xlarge = 4 cores is oversubscribed by `wrk -t4`.)
2. **Fail-safe (set immediately after up):** instance-initiated shutdown →
   `terminate`, plus a scheduled `sudo shutdown -h +180` on the instance, so
   it self-terminates even if nothing else runs.  The agent is never the sole
   teardown path.
3. **`bash bench/aws/install.sh`** — deploys repo + venv + wrk + toolchain.
   Caveats found:
   - It rsyncs with `--exclude '.git/'` → `ab_commit.sh`'s `git checkout`
     swap needs the refs: re-rsync the tree **including `.git`**.
   - Ubuntu 24.04 is PEP-668-externally-managed → install `uv` via the
     official installer (`~/.local/bin`), not pip.
   - `uv run` recreates the venv **without** the `blackbull` console script →
     fix with `uv pip install -e .`; verify the swap works via
     `blackbull.__file__` in the source tree + the import-hash proof.
4. **Run:** on the instance,
   `nohup env REF_BASE=.. REF_TREAT=.. ROUNDS=12 DURATION=15 THREADS=4
   CONNS=32 SERVER_CPUS=0-1 LOAD_CPUS=2-5 PHASES="null real"
   bash bench/peers/ab_commit.sh > bench/results/ec2-ab.log 2>&1 &` — nohup
   so it survives prompt end.
5. **Finish:** a local nohup'd script polls the remote `raw.tsv` to its
   expected line count, `scp`s the results dir back, then runs `down.sh`.
   (Instance self-termination is the backstop.)

## 5. Tools

| Tool | Purpose |
|---|---|
| `ab_commit.sh` | ABBA measurement: swap+import-proof, null+real, pinning, per-round raw.tsv |
| `ab_report.py` | Standard report: mean + 1 SE + hi-mode diagnostic; mean (not median) |
| `ab_equiv_report.py` | TOST equivalence within ±Δ; bimodal split/combined (use pooled for monomodal) |

## 6. Calibration numbers from the 6931b77 verification (2026-08-01)

- **Local box:** bimodal; within-mode null floor ±1.3 % at 4 rounds; 4 rounds
  cannot claim ±0.5 %.
- **EC2 m7a.2xlarge, single worker:** monomodal, ~31k req/s, CV ~0.6–0.7 %,
  12 rounds → 96 measurements.
- **Real Δ (no-op refactor):** MLE +0.287 %; pooled SE 0.197
  (CI [−0.11, +0.69]); round-paired SE 0.127 (CI [+0.007, +0.568]); t≈1.45,
  p≈0.15 — not significant.  Equivalent within **±1 %**; **±0.5 % not
  claimable** at 12 rounds (CI upper +0.57..+0.69 % at every trim level).
- **Bytecode cross-check:** hot path instruction stream identical; only
  OPTIONS*-only code removed (run() 1047→933 instr, branches 85→74).  This is
  the strongest evidence a benchmark could never beat: a strict no-op on the
  measured path.

## 7. Verdict rules

A regression (the change being *slower*) is essentially ruled out when:

1. The real Δ point estimate is ≥ 0 or its 95 % CI is not below 0, **and**
2. The real CI is inside the chosen equivalence bound (or at least inside
   ±1 %), **and**
3. The bytecode/hot-path analysis shows the measured path executes the same
   instructions (the deterministic proof the benchmark can only approximate).

A ±0.5 % equivalence claim specifically needs the real-phase 95 % CI inside
±0.5 % **and** the null phase to also pass (after outlier trim).  If the CI
upper bound sits at +0.57–0.69 % because the point estimate is +0.3 %, more
rounds (not outlier removal) are the only path.
