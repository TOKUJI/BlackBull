# AGENTS.md — BlackBull

This file extends the `AGENTS.md` in the parent directory; do not restate its
rules here.

## Implementation discipline

Before writing code, follow this order:

1. Search the Python standard library, available packages, and this repository
   for the required capability.
2. If an equivalent implementation exists, reuse it; do not add another.
3. If a similar implementation exists, determine whether both can share one
   abstraction or implementation.
4. If they can, refactor them to share it.
5. Add a new implementation only after establishing that neither reuse nor
   commonization is possible.

Add prose, including comments and documentation, only when omitting it would
cause a user or developer to make a mistake. Prefer a name, type, signature,
or test; do not restate one in prose. The same holds for a pull request
description: a summary, not a second copy of the commit message.

Run `just typecheck`, `just test`, and `just docs` as applicable. When behavior
or an API changes, update `docs/guide/` for users or `docs/about/` for internals.

## Architecture invariants

- **Protocol ownership:** HTTP/1.1, HTTP/2, WebSocket, gRPC, and MQTT wire
  handling stays pure Python. Do not depend on `h11`, `h2`, `wsproto`, or any
  other third-party protocol implementation.
  See `docs/about/architecture.md`.
- **Actors:** concurrency uses message passing, not shared locks.
  `ConnectionActor` creates one protocol actor and inbox loop per connection;
  only that loop mutates its state. Use a per-connection `asyncio.TaskGroup`.
  See `docs/about/internals.md`.
- **Connection boundary:** the native server carries typed `Connection`
  objects end to end. ASGI scope dictionaries exist only for external ASGI
  hosts and `BB_FORCE_ASGI_SCOPE=1`; `scope` always means such a dictionary.
  See `docs/about/internals.md` (Read-path invariant).
- **One runtime:** all protocols share one process and runtime. Attach non-HTTP
  protocols with `app.add_extension(...)`.
  See `docs/about/architecture.md`.
- **Events:** Level A messages are internal actor traffic. Level B exposes
  `@app.on` (fire-and-forget, isolated exceptions) and `@app.intercept`
  (synchronous, may short-circuit). Each of the four request-lifecycle events
  fires exactly once per request on every transport.
  See `docs/guide/events.md`.
- **Send path:** protocol senders pass parts to `BaseSender._write_many(parts)`;
  they do not choose joining or vectored writes. The 32 KiB size gate decides.
  See `docs/about/internals.md` (Send-path invariant).

## Required conventions

- Header keys are bytes and lookups use lowercase, for example
  `headers.get(b"content-type")`.
  See `docs/guide/requests-and-responses.md`.
- WebSocket servers never mask outgoing frames. `FragmentAssembler` gives the
  application complete messages; RSV1 denotes per-message deflate.
  See `docs/guide/websockets.md`.
- The router recognizes a full handler only when both `receive` and `send` are
  present; simplified handlers return `str | bytes | dict | Response | None`.
  WebSocket handlers always use `(conn, receive, send)`.
  See `docs/getting-started/first-app.md`.
- Middleware uses `(conn, receive, send, call_next)` and short-circuits by not
  calling `call_next`. `@as_middleware` converts `Response` objects to ASGI
  events. See `docs/guide/middleware.md`.
- `@log` checks its logger level at decoration time. Use `blackbull.*` for
  DEBUG and `blackbull.access` for INFO.
  See `docs/guide/logging.md`.

## YouTrack safety

- Use only the `just yt-*` commands in `justfile`; do not call the REST API
  directly, hard-code credentials, or print `YOUTRACK_URL` or
  `YOUTRACK_TOKEN`.
- Issues use `BLA-<n>` and Knowledge Base articles use `BLA-A-<n>`. Use
  `just yt-show` for issues and `just yt-article` for articles.
- Every `just yt-article-update` invocation requires the user's explicit
  permission for that specific edit. This gate does not apply to issues.

Tracker planning uses issue `Type` and the `active`, `candidate`, `archive`,
and `sprint-log` tags. Close superseded work; do not move or delete its record,
and do not infer staleness from age. Prioritize `tag: active #Unresolved` by
`Priority`.
