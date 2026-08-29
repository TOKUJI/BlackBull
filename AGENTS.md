# AGENTS.md — BlackBull

## Identity

BlackBull is a **multi-protocol async framework with its own ASGI 3.0-compatible
server** — HTTP/1.1, HTTP/2, WebSocket, gRPC, and MQTT 5 coexist in one process,
no reverse proxy or sidecar required.  
An **actor-model core** gives every connection its own isolated inbox loop; the 
same message-passing runtime drives all protocols.

Internally native; ASGI is kept only at the two external boundaries.  
**Lightweight by design**: declarative DI, OpenAPI schema generation, and a rich 
router — but no built-in template engine, auth, or ORM.  
**Pure Python** (zero C extensions), **competitive throughput**, and
**RFC-grade conformance** (h2spec, Autobahn, http11probe, RFC 10008 HTTP QUERY).

A personal learning project — wire correctness over API stability (ZeroVer).

Workspace-wide rules — confidentiality, general operating principles, tool
preferences — live in `~/work/AGENTS.md` and load before this file.  What
follows is only what is specific to BlackBull; where the two overlap, this
file is the more specific one and wins.

---

## Operating principles

The workspace-wide principles are in `~/work/AGENTS.md`.  Below are the ones
that only make sense inside BlackBull.

- **Findings update proposals; memory holds only tooling gotchas.**  When a
  measurement or investigation answers a question an open proposal is
  pursuing, update that proposal — or create one — in the same turn (status
  line + INDEX.md row included).  Never park a proposal-relevant answer in
  `/memories/repo/`; repo memory is only for operational gotchas (tooling,
  harness, environment quirks) that no proposal and no `docs/` page covers.
  Before writing a new repo-memory file, grep `proposals/INDEX.md` for the
  proposal that owns the question; if one exists, update it instead.

- **Every limit names its triad column.**  When you add or change a resource
  limit, state which of the three it occupies — *how big may one unit be*,
  *how big may the total be*, *how long may it take* — and name the owner of
  the other two.  Almost every memory gap the attack-surface audit found was a
  unit cap mistaken for a total cap: the frame was capped and the message was
  not, the packet was capped and the session state was not.  The one exception
  names the second question to ask: the HTTP/2 priority tree had no total
  because nothing ever *read* what it stored, so nobody counted it as storage —
  **a write with no reader is still a growable path**.  Also check what state
  the protocol *shares*, because that constrains the answer — an HTTP/2 header
  block cannot be abandoned per-stream, since HPACK state is connection-wide.
  → `.claude/planning/research/attack-surface-audit-2026-08.md` [private]

- **Type-check before committing.** `just typecheck` catches contract
  violations statically.  → `.claude/skills/type-check/SKILL.md` [private]

- **When stuck on handler code, consult docs and examples.**  Consult
  `docs/getting-started/` and `examples/` before guessing.  A signature
  mistake (full ASGI vs simplified form) wastes EC2 hours.

### BlackBull addenda to the workspace rules

- *Docs follow code* — the page to update lives under `docs/`: `docs/guide/`
  covers user-facing features; `docs/about/` covers internals.

---

## Architecture principles

Non-negotiable structural rules.  Each cites its authoritative source —
the *why* lives there; here is the *what* so you don't violate it by accident.

- **Protocol ownership** — Every byte of HTTP/1.1, HTTP/2, WebSocket, gRPC,
  and MQTT is pure Python.  Never introduce a dependency on `h11`, `h2`,
  `wsproto`, or any third-party protocol library.
  → `docs/about/architecture.md`

- **Actor model** — Concurrency is message-passing, not shared-lock.
  `ConnectionActor` spawns a protocol actor per connection; each runs its own
  inbox loop.  State lives inside one actor, mutated only by that actor's loop.
  Per-connection `asyncio.TaskGroup` for structured concurrency.
  → `docs/about/internals.md`, `.claude/design/actor-model.md`

- **Native `Connection`, not ASGI scope** — BlackBull's own server threads a
  typed `Connection` end-to-end.  ASGI scope dicts exist only at two boundaries
  (external ASGI hosts and `BB_FORCE_ASGI_SCOPE=1`).  The word `scope` in code
  means a genuine ASGI scope dict — never a `Connection`.
  → `docs/about/architecture.md`

- **Multi-protocol, one process** — HTTP, WebSocket, gRPC, and MQTT share one
  runtime.  Non-HTTP protocols attach through `app.add_extension(...)`.
  → `docs/about/architecture.md`

- **Two-level event system** — Level A: internal actor-to-actor messages (not
  subscribable from application code).  Level B: `@app.on` (fire-and-forget,
  exceptions isolated) / `@app.intercept` (synchronous, can short-circuit).
  The four request-lifecycle events fire exactly once per request under any
  transport.  → `.claude/design/event-catalogue.md`, `docs/guide/events.md`

- **Send-path invariant** — Protocol senders never choose join-vs-vectored;
  they call `BaseSender._write_many(parts)`.  The size gate (32 KiB) decides.
  → `docs/about/internals.md` §Send-path invariant

---

## Conventions & gotchas

Rules that aren't architectural but will cause subtle bugs if you forget them.

- **Headers: bytes keys, lowercase index** — `headers.get(b'content-type')`.
  → `docs/guide/requests-and-responses.md`

- **WebSocket** — Server never masks outgoing frames (RFC 6455 §5.1).
  `FragmentAssembler` reassembles transparently; the app always receives one
  complete message.  RSV1 = per-message deflate (RFC 7692).
  → `docs/guide/websockets.md`

- **Handler signatures** — The router detects simplified vs full form at
  registration time.  Full form = both `receive` + `send` params present;
  simplified = return `str | bytes | dict | Response | None`.  Middleware
  and WebSocket handlers always use the full `(conn, receive, send)` form.
  → `docs/getting-started/first-app.md`

- **Middleware** — `(conn, receive, send, call_next)`.  Short-circuit by
  returning without calling `call_next`.  `@as_middleware` normalises
  `Response` objects into plain ASGI events.
  → `docs/guide/middleware.md`

- **Logging** — `@log` checks the logger level at decoration time (import);
  a zero-cost no-op when DEBUG is disabled.  Two hierarchies: `blackbull.*`
  (DEBUG) and `blackbull.access` (INFO).
  → `docs/guide/logging.md`

---

## Tool preferences

The general table — search priority `ast-grep` → `rg` → `grep`, plus `uv`,
`just`, `py-spy`, `jq`, `bc` — is in `~/work/AGENTS.md`.  BlackBull specifics:

- **`just`** — the repo's `justfile` carries `just typecheck`, `just test`,
  `just docs`.
- **`jq`** — filter conformance output, e.g.
  `jq '[.cases[] | select(.state=="failed")]'` on h2spec / http11probe results.
- **`uv`** — entry points are declared in `pyproject.toml` `[project.scripts]`.
- **`py-spy`** — the profiling workflow lives in
  `.claude/patterns/benchmarking.md` §Profiling [private].

---

## Working docs map

This file and `~/work/AGENTS.md` are the only docs auto-loaded every session.
The docs below are **not** loaded automatically — open the relevant one when
the trigger applies. Do not duplicate their content here; link, don't copy.

Entries under `.claude/` are **git-ignored** (stored in a private companion
repo; see `.claude/CLAUDE_DEV.md` for setup).  Each entry lists a public
fallback where one exists.

| When you are… | Read |
|---|---|
| Doing any framework change (workflow, testing, type rules) | `.claude/CLAUDE_DEV.md` [private] |
| Writing/adjusting tests | `.claude/patterns/testing.md` + `.claude/skills/create-test/SKILL.md` [both private] |
| Running benchmarks or profiling | `.claude/patterns/benchmarking.md` + `.claude/skills/bench-compare/SKILL.md` [both private] |
| Chaining dependent long-running steps (multi-phase measurement/build) | `.claude/patterns/chaining-long-running-steps.md` [private] |
| Running peer server comparisons (FastAPI / Sanic / etc.) locally | `.claude/skills/peer-compare/SKILL.md` [private] |
| High-precision A/B check (rule out regression / equivalence within ±Δ%) | `bench/peers/AB-HIGH-PRECISION.md` + `.claude/skills/ab-verify/SKILL.md` [skill private] — read the null phase before trusting a real verdict; pooled TOST on EC2 |
| Tracing a regression across sprints | `.claude/sprint-logs/` [private] — per-sprint bottleneck-attribution logs.  `bench/CHARACTERIZATION.md` is the public summary; sprint-logs hold the raw diagnostic numbers |
| Cutting a release / sprint close | `.claude/patterns/release.md` + `.claude/skills/sprint-close/SKILL.md` [both private] |
| Reasoning about actors / events | `.claude/design/actor-model.md` + `.claude/design/event-catalogue.md` [both private] |
| Checking a known gotcha before acting | `.claude/patterns/cautions.md` [private] |
| Adding or changing a resource limit / reviewing defence coverage | `.claude/planning/research/attack-surface-audit-2026-08.md` [private] — the mechanism × surface matrix, the limit-triad grid, and the closed gap register with its evidence pointers |
| Answering a user's question about BlackBull's security posture | `docs/about/security-model.md` — quote the published claim rather than improvising one; it is a projection of the audit above, so the two must not drift |
| Picking/triaging what to build next | `.claude/planning/proposals/INDEX.md` [private] |
| Reading a point-in-time design | `.claude/planning/designs/` [private] |

**Skills** (invocable, harness-surfaced; they live in `.claude/skills/` [private] —
`.github/skills` is an optional local symlink, see `.gitignore`):
`ab-verify`, `refactor`, `sprint-close`, `bench-compare`, `peer-compare`, `pre-release-docs`,
`update-roadmap`, `create-test`, `type-check`, `add-event`, `new-http2-frame`,
`protocol-handler`, `httparena-bench`, `run-http11probe`.

### Task-to-skill mapping

Before acting on a request, read the corresponding skill file first.

| Request type | Read first |
|---|---|
| Benchmark / performance comparison | `.claude/skills/bench-compare/SKILL.md` [private] |
| A/B regression check / rule out regression / equivalence within ±Δ% | `.claude/skills/ab-verify/SKILL.md` [private] + `bench/peers/AB-HIGH-PRECISION.md` |
| HttpArena (EC2 / local) | `.claude/skills/httparena-bench/SKILL.md` [private] |
| HttpArena local run details | `/memories/repo/httparena-local-run.md` |
| Type checking | `.claude/skills/type-check/SKILL.md` [private] |
| Test authoring | `.claude/skills/create-test/SKILL.md` [private] |
| New event | `.claude/skills/add-event/SKILL.md` [private] |
| New protocol handler | `.claude/skills/protocol-handler/SKILL.md` [private] |
| New HTTP/2 frame | `.claude/skills/new-http2-frame/SKILL.md` [private] |
| Pre-release audit | `.claude/skills/pre-release-docs/SKILL.md` [private] |
| Sprint close | `.claude/skills/sprint-close/SKILL.md` [private] |
| Roadmap update | `.claude/skills/update-roadmap/SKILL.md` [private] |

### Doc lifecycle (so docs don't rot)

Every doc under `.claude/planning/` carries a status line near the top:
`**Status**: active | shipped vX.Y.0 | superseded-by <file> | archived <date>`.
When a proposal/design ships or dies, move it to `.claude/planning/archives/`
(that pruning is a step in the `sprint-close` skill). `.claude/` is git-ignored,
so deletions are **not** recoverable — prune deliberately, archive when unsure.
Findings land in the owning proposal in-session; INDEX regeneration and
archival pruning stay sprint-close steps (see *Findings update proposals* above).

