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

---

## Operating principles

- **Docs follow code.** When you change a feature, behaviour, or API,
  update the corresponding page under `docs/` in the same commit.
  `docs/guide/` covers user-facing features; `docs/about/` covers internals.

- **Test first.** Add or update tests before implementing.  Assert
  observable behaviour (events emitted / bytes on the wire), not
  implementation internals.  → `.claude/patterns/testing.md`

- **Type-check before committing.** `just typecheck` catches contract
  violations statically.  → `.claude/skills/type-check/SKILL.md` [private]

- **Comments explain *why*, never *when*.** If a comment contains a sprint
  number, a date, "still", "previously", "no longer", or "as of version X",
  delete those parts — `git log` owns the timeline.  A comment's only job is
  to capture non-obvious intent, design trade-offs, or invariants the next
  reader would otherwise have to reverse-engineer from the code.

- **Verified numbers only — estimates and calculations alike.** Never present
  arithmetic, percentages, or summed tables that a tool has not computed.  For
  any calculation use `bc` / `calc` / `python -c`; for any table with a Σ row,
  generate it from a script that checks its own sums (Σ == total) and paste
  the output verbatim — never hand-copy numbers into a response.  A number
  you cannot reproduce from a script output is a number you do not have.

- **Questions end the turn.** A pending decision gate is a stop, not a
  licence to continue.  If the answer to a gate question is not an explicit
  user choice, end the turn with the question restated — do not treat a
  "user unavailable / work autonomously" response as approval, and pause
  background steps that depend on the decision.  Resume only on explicit
  user input.  (Applies in Autopilot mode too; the agent must explicitly
  stop and wait.)

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

| Task | Tool | Why |
|---|---|---|
| Structural search / replace | `ast-grep` (`sg`) | Understands AST; no regex false positives |
| Plain-text grep | `rg` (ripgrep) | Fast, `.gitignore`-aware |
| File finding | `rg --files` or `rg -l` | One tool, less context switching |
| Python package management | `uv` | Single binary replaces `pip` + `venv`; see `pyproject.toml` `[project.scripts]` |
| Command runner | `just` | Project-specific commands in `justfile`.  `just typecheck`, `just docs`, etc. — no more memorising long `pytest`/`mkdocs` invocations |
| CPU profiling | `py-spy` | `py-spy record -f speedscope -o profile.json -- python app.py` — speedscope JSON, **not** SVG: each sample is already a full call-stack (list of frame indices into `shared.frames`), so it opens directly in VS Code / speedscope.app and parses with stdlib `json` (no SVG geometry reconstruction).  `py-spy top -- python app.py` (live).  Run as same user — if ptrace blocked, add `--nonblocking` |
| JSON filtering | `jq` | `jq '[.cases[] \| select(.state=="failed")]'` on h2spec/http11probe results |
| Arithmetic / sums | `bc` (or `calc`, `python -c`) | No mental math — pipe the operands into `bc` and let it add; paste script/table output verbatim (see *Verified numbers only*) |

- Prefer `rg` over `grep` / `find` for all text search.
- Prefer `ast-grep -p 'pattern' -l python` over `rg` when matching code
  structure (function defs, call sites, class hierarchies).
- Use `ast-grep -U` for automated structural replacements — it won't
  corrupt syntax like `sed` can.

---

## Working docs map

This file is the only doc auto-loaded every session. The docs below are **not**
loaded automatically — open the relevant one when the trigger applies. Do not
duplicate their content here; link, don't copy.

Entries under `.claude/` are **git-ignored** (stored in a private companion
repo; see `.claude/CLAUDE_DEV.md` for setup).  Each entry lists a public
fallback where one exists.

| When you are… | Read |
|---|---|
| Doing any framework change (workflow, testing, type rules) | `.claude/CLAUDE_DEV.md` [private] |
| Writing/adjusting tests | `.claude/patterns/testing.md` + `.claude/skills/create-test/SKILL.md` [both private] |
| Running benchmarks or profiling | `.claude/patterns/benchmarking.md` + `.claude/skills/bench-compare/SKILL.md` [both private] |
| Running peer server comparisons (FastAPI / Sanic / etc.) locally | `.claude/skills/peer-compare/SKILL.md` [private] |
| High-precision A/B check (rule out regression / equivalence within ±Δ%) | `bench/peers/AB-HIGH-PRECISION.md` + `.claude/skills/ab-verify/SKILL.md` [skill private] — read the null phase before trusting a real verdict; pooled TOST on EC2 |
| Tracing a regression across sprints | `.claude/sprint-logs/` [private] — per-sprint bottleneck-attribution logs.  `bench/CHARACTERIZATION.md` is the public summary; sprint-logs hold the raw diagnostic numbers |
| Cutting a release / sprint close | `.claude/patterns/release.md` + `.claude/skills/sprint-close/SKILL.md` [both private] |
| Reasoning about actors / events | `.claude/design/actor-model.md` + `.claude/design/event-catalogue.md` [both private] |
| Checking a known gotcha before acting | `.claude/patterns/cautions.md` [private] |
| Picking/triaging what to build next | `.claude/planning/proposals/INDEX.md` [private] |
| Reading a point-in-time design | `.claude/planning/designs/` [private] |

**Skills** (invocable, harness-surfaced; they live in `.claude/skills/` [private] —
`.github/skills` is an optional local symlink, see `.gitignore`):
`ab-verify`, `refactor`, `sprint-close`, `bench-compare`, `peer-compare`, `pre-release-docs`,
`update-roadmap`, `create-test`, `type-check`, `add-event`, `new-http2-frame`,
`protocol-handler`, `httparena-bench`, `run-http11probe`.

### Doc lifecycle (so docs don't rot)

Every doc under `.claude/planning/` carries a status line near the top:
`**Status**: active | shipped vX.Y.0 | superseded-by <file> | archived <date>`.
When a proposal/design ships or dies, move it to `.claude/planning/archives/`
(that pruning is a step in the `sprint-close` skill). `.claude/` is git-ignored,
so deletions are **not** recoverable — prune deliberately, archive when unsure.

