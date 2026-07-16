# GitHub Copilot Instructions — BlackBull

## Operating principles

### 1. Clarify ambiguity before acting

When a request is underspecified, do not self-interpret.  Ask the user
for clarification with concrete, narrow questions.  A wrong assumption
is always more expensive than a round-trip question.

### 2. Plan, then execute

Once the task is clear, create a work plan as a todo list.  Each item
must be a single verifiable action — not a phase, not a goal.  Share
the plan with the user before starting work.

### 3. Track progress; ask permission to deviate

Update the plan's status as each item is completed.  If something
cannot proceed as planned, stop immediately, explain the blocker to
the user, and get explicit approval before changing the plan.

### 4. Fail-safe for anything that outlasts a turn

If any step takes more than roughly one minute to complete, the agent
cannot reliably observe its completion across turns.  Such steps must
include a safety mechanism that works without the agent:

- A cloud resource must self-terminate; the agent is never the sole
  teardown path.
- A long-running script must write its result to a known file.  The
  agent reads the file afterward; it never polls a live process.
- A command that could hang must carry an explicit timeout.

### 5. Verifiable, ephemeral work products

Do not embed complex logic inline in prompt responses.  Instead:
create a script file, run it, capture the output, then delete the
script.  The script file is the auditable record of what was done.

### 6. Use skill philosophies to prevent failures

When a task matches a skill, use that skill.  When a task is similar
but not an exact match, apply the skill's philosophy to guide the work
and prevent known failure patterns.  If a skill lacks a clear
philosophy — a concise statement of its approach and guardrails at the
top of the file — write one before using it.

### 7. When stuck on handler code, consult docs and examples

When writing route handlers or debugging handler behaviour, consult
docs/getting-started/ and examples/ before guessing.  A
signature mistake (full ASGI vs simplified form) wastes EC2 hours.


## Task-to-skill mapping

Before acting on a request, read the corresponding skill file first.

| Request type | Read first |
|---|---|
| Benchmark / performance comparison | `.github/skills/bench-compare/SKILL.md` |
| HttpArena (EC2 / local) | `.github/skills/httparena-bench/SKILL.md` |
| HttpArena local run details | `/memories/repo/httparena-local-run.md` |
| Type checking | `.github/skills/type-check/SKILL.md` |
| Test authoring | `.github/skills/create-test/SKILL.md` |
| New event | `.github/skills/add-event/SKILL.md` |
| New protocol handler | `.github/skills/protocol-handler/SKILL.md` |
| New HTTP/2 frame | `.github/skills/new-http2-frame/SKILL.md` |
| Pre-release audit | `.github/skills/pre-release-docs/SKILL.md` |
| Sprint close | `.github/skills/sprint-close/SKILL.md` |
| Roadmap update | `.github/skills/update-roadmap/SKILL.md` |
