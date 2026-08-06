# WebSocket channel A/B plan

**Status**: active — written 2026-08-06, after the v0.67.0 → v0.70.0 HttpArena
cross-check flagged `echo-ws-pipeline` and the hot-path reading showed the
cause was not where the report attributed it.

Companion to [`AB-HIGH-PRECISION.md`](AB-HIGH-PRECISION.md), which covers the
statistics and the null-phase discipline. This document is the *matrix*: what
to measure, under which conditions, and which question each cell answers.

---

## Why this plan exists

Three things went wrong in the last round, and the plan is shaped to stop each
from recurring.

1. **The instrument never touched the code under test.** Both bench apps'
   `/ws` handlers are raw `(conn, receive, send)` form, so every `echo-ws`
   number ever recorded measures the ASGI dict boundary. The native channel
   the PR is about was unmeasured. → `/ws-object` now exists; **channel is an
   explicit factor**, never an assumption.

2. **A regression was attributed to the wrong change.** `echo-ws` moved
   because `BB_WS_QUEUE_DEPTH` flipped 256 → 0 in v0.69.0, two releases before
   the commit being blamed. → **read mode is an explicit factor**, and builds
   are compared at *matched* read mode, not at their respective defaults,
   except in the one phase where production-vs-production is the question.

3. **An underpowered test was read as a verdict.** n=3 with a multiplicity
   correction cannot resolve 2 %; "not significant" was reported as "not a
   regression". → every phase declares its **resolution floor** up front, and
   any effect below that floor is reported as *unresolved*, never as *absent*.

---

## Instruments and what each can actually resolve

Pick the instrument from the effect size, not from habit. Effects below an
instrument's floor must go to a smaller instrument or stay unresolved.

| Instrument | Measures | Resolves down to | Use for |
|---|---|---|---|
| In-process ABBA microbenchmark | one method, ns/call | ~1 % of that method | dispatch order, per-message encoding |
| `ab_commit_ws.sh` (1 worker, pinned) | server msg/s | ~1 % (verify per null phase) | code changes under `blackbull/` |
| HttpArena `echo-ws*` (16 workers) | server msg/s | ~2–3 % (n=3) | cross-profile sweeps only |

**Worked example of why this matters.** At `echo-ws-pipeline/512` the head arm
ran 17.14 cores at 778,417 msg/s = **22.01 µs CPU per message**. The send-path
ordering fix is worth 61 ns = **0.28 % of that budget**. No server-level A/B in
this table can see it. It was measured in-process, and that microbenchmark —
not a server run — is its authoritative result. Do not schedule a server A/B to
"confirm" an effect the server cannot resolve.

---

## Factors

| Code | Factor | Levels |
|---|---|---|
| **B** | Build | `B0`=v0.67.0 · `B1`=2501bee (pre-native-world) · `B2`=a3805d5 (native world) · `B3`=db7dc02 (+ send-path fix) |
| **D** | `BB_WS_QUEUE_DEPTH` | `D0`=0 inline · `D256`=256 eager read-ahead |
| **C** | Channel | `Cd`=`/ws` dict (ASGI boundary) · `Cn`=`/ws-object` native |
| **S** | Pipelining (`WS_BURST`) | `S1`=1 serial · `S8`=8 in flight |
| **N** | Connections | 512 · 4096 · 16384 |
| **W** | Workers | `W1`=1 · `W16`=16 |

Full crossing is 4×2×2×2×3×2 = 192 cells. That is not a plan, it is a wish.
The phases below each **hold everything fixed but one factor**, which is what
makes a difference attributable at all.

---

## Phase 0 — Null floor (gate, run first)

**Question:** what is this rig's noise floor, per lane shape?

A/A: identical bytes under both labels (`PHASES=null`). Run it for **every
distinct lane shape** used later — the floor is not a constant, it varies with
connections and worker count.

| Cell | C | S | N | W |
|---|---|---|---|---|
| 0.1–0.8 | `Cd` | `S1`,`S8` | 512, 4096 | `W1`,`W16` |

**Gate:** if a shape's null |Δ| exceeds **1 %**, no real-phase result from that
shape may be quoted at finer than the observed null. Fix it (more rounds,
longer duration, better pinning) or widen the reported floor. Do not proceed
into that shape and hope.

---

## Phase 1 — Read mode  *(the leading hypothesis)*

**Question (Q1):** does inline-vs-eager explain `echo-ws-pipeline`?

Build fixed at `B3`. Channel fixed at `Cd` — this is the shape the historical
lanes used, so it is the one that must reproduce them. **`D0` vs `D256` is the
only thing that varies.**

| Cell | S | N | W | Prediction if the hypothesis holds |
|---|---|---|---|---|
| 1.1 | `S1` | 512 | `W16` | `D0` faster — nothing to read ahead |
| 1.2 | `S1` | 4096 | `W16` | `D0` faster, margin grows with N |
| 1.3 | `S8` | 512 | `W16` | **`D256` faster** — read-ahead has work to overlap |
| 1.4 | `S8` | 4096 | `W16` | `D256` faster |
| 1.5–1.8 | as 1.1–1.4 | | `W1` | same signs; `W1` also tests the harness contradiction below |

**Falsifiable:** the hypothesis predicts a *sign flip* between `S1` and `S8`.
If both burst levels move the same way, read mode is not the mechanism and
Phase 1 has refuted it — which is a result, not a failure.

Known so far, and *not* sufficient on its own: with 64 frames pre-loaded,
eager buffers 64 ahead of the first `receive()` and inline buffers 0. That
shows read-ahead *happens*, not that it *pays* — the probe had no concurrent
handler work. Phase 1 is what decides whether it pays.

---

## Phase 2 — Send-path ordering fix

**Question (Q3):** did the fix land, and is anything worse at server level?

`B2` vs `B3`, `Cd`, `S8`, `W16`, N ∈ {512, 4096}, D at whatever Phase 1 shows
is right for `S8`.

**This is a guard, not a measurement.** The effect is 0.28 % of the per-message
budget — below the floor of every server instrument here. The expected and
acceptable outcome is *indistinguishable from null*. Report it that way. The
authoritative number is the in-process one: **−61.0 ± ~15 ns/send, n=16/arm**.

A result *outside* the null floor in either direction is the interesting one —
it would mean the reordering did something beyond the two type checks.

---

## Phase 3 — The native channel  *(never yet measured)*

**Question (Q2):** does the native WS channel pay end-to-end?

`B3` fixed. **`Cd` vs `Cn`** is the only variable. Both endpoints are served by
the same process, so this comparison needs no build swap, no restart between
arms, and no cross-run differencing — the cleanest cell in the plan. Still run
it ABBA: the two arms are still two measurements.

| Cell | D | S | N | W |
|---|---|---|---|---|
| 3.1–3.2 | `D0` | `S1`,`S8` | 512 | `W16` |
| 3.3–3.4 | `D0` | `S1`,`S8` | 4096 | `W16` |
| 3.5–3.8 | `D256` | `S1`,`S8` | 512, 4096 | `W16` |

**Anchor:** the isolated receive channel is −4.4 % (2550 → 2438 ns/message) and
the isolated send channel avoids one dict build. Against a 22 µs per-message
budget those are fractions of a percent, so **a null result here is the honest
prior**, not a disappointment. What Phase 3 buys is the first direct evidence
either way, and a check that the native path is not *slower*.

Run `D256` as well as `D0` because the native channel's cost structure differs
between modes — `next_message()` skips the queue handoff that `__call__` pays,
and that saving only exists in eager mode.

---

## Phase 4 — Residual vs v0.67.0

**Question (Q4):** after Q1–Q3 are accounted for, is anything left?

Production-to-production, each build at **its own default** — the only phase
where defaults, not matched settings, are the point.

`B0` (`D256`, `Cd`) vs `B3` (`D0`, `Cd`), N ∈ {512, 4096, 16384}, W ∈ {`W1`,`W16`}.

Note `ab_commit_ws.sh` compares refs by swapping files under `PATHSPEC` inside
one tree. Across 29 commits that may not apply cleanly; if it fails, run the
two builds as separate installs **on one instance in one session**, ABBA
between them, and never difference two sessions.

**Anchor from the hot path** (receive channel, ns/message): v0.67.0 eager
2787.3 ± 38.8 → B3 inline 2793.9 ± 21.9, i.e. **+0.24 %** — flat. If Phase 4
shows a large residual, it is not in the receive encoding, and the next place
to look is the actor/event-loop layer, not the recipient.

---

## Cross-cutting: the harness contradiction

The single-worker A/B lane reported WS echo **−5.10 %** while 16-worker
HttpArena reported **+5.2…+13.7 %** on the same code path (both are `Cd` — this
was checked). That is unresolved, and worker count is the obvious suspect,
which is why `W` is a factor in Phases 1 and 4 rather than a fixed setting.

Until it is resolved, **neither harness's WS verdict overrides the other.**

---

## Execution rules

Non-negotiable, each one bought with a past mistake:

1. **One variable per comparison.** If two things differ, the result is not
   attributable, however clean the number looks.
2. **ABBA within a round, ≥4 rounds** (`ROUNDS=4`). ABBA cancels linear drift.
3. **Never difference two runs.** Both arms measured in one session on one
   instance. Cross-run deltas on this workload produced +14, +38, and −61 ns
   for the *same* change — drift, not signal.
4. **Null before real, every shape** (Phase 0). A real result is quotable only
   against its own shape's floor.
5. **Prove which bytes ran.** Keep the harness's import-hash proof; for
   separate installs, log `blackbull.__file__` and the wheel version from
   inside the serving process. An editable install's finder beats `sys.path`
   and will silently serve the wrong tree.
6. **Record CPU% and the generator's own CPU** every run. A lane where the
   generator is saturated measures the generator. Check headroom before
   believing a ceiling.
7. **Report mean ± SE with n**, plus the null floor beside it. An effect
   inside the floor is **unresolved**, never "no regression".
8. **Watch for dead runs.** HttpArena's gRPC lanes at 1024c drop a run to
   0 req/s; the WS lanes can drop connections. A run that measured nothing is
   excluded and *reported as excluded*, not averaged in.

---

## Reporting template

Per comparison, one row:

```
<phase>.<cell>  <factor>=A|B  N=<conns> W=<workers> S=<burst> D=<depth> C=<chan>
  A: <mean> ± <SE> msg/s (n=<runs>)   CPU <a>%   gen-CPU <g>%
  B: <mean> ± <SE> msg/s (n=<runs>)   CPU <b>%   gen-CPU <g>%
  Δ = <pct>% ± <se>%    null floor for this shape = <null>%
  verdict: resolved-faster | resolved-slower | UNRESOLVED (|Δ| < floor)
```

Excluded runs listed explicitly with the reason.

---

## Size

32 comparisons × 16 runs (ROUNDS=4, ABBA) = **512 runs**, ≈ **2.6 h** at 18 s
per run including restart and settle. Phases are independent — Phase 1 alone
(8 comparisons, 128 runs, ~40 min) answers the question that actually blocks
the PR narrative, and is the sensible first sitting.

**Suggested order:** Phase 0 → Phase 1 → stop and read. Phase 1's sign pattern
decides whether Phases 3 and 4 need their `D256` arms at all.
