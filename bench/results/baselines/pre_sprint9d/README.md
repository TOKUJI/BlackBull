# Pre-Sprint-9d benchmark archive

Runs in this directory were taken **before commit `4e28116`**
("feat: auto-reload + benchmark methodology + framework wins",
2026-05-22 13:47 JST), which landed the Sprint 9c/9d hot-path wins:

- Inlined `_make_capturing_send` (was ~7 % CPU)
- `emit_access_log()` gated on `isEnabledFor(INFO)` — makes
  `BB_ACCESS_LOG=0` actually free (~1.2 % CPU)
- 1-second Date-header cache in `sender.py` (~2.6 % CPU)
- Explicit `backlog=` passed to `asyncio.start_server` (no more
  silent override to 100)

The post-9d shape, measured at 2026-05-23 01:48 JST, sits roughly:

| Lane | BlackBull (post-9d, 2026-05-23 WSL) |
|---|---|
| B1 plaintext c=256 | 19.7–21.6 k req/s |
| B3 json c=256     | 22–24 k req/s |
| B6 echo-1k        | 19.8–23.4 k req/s |
| A1 mux=1          | 16.5–16.8 k req/s |

Compared to runs in this archive, where the pre-9d BlackBull B1 sat
around 16–20 k req/s.

**Do not use these files as a baseline for current code comparisons.**
They are kept for archaeological reference only — to confirm the
delta caused by the 9c/9d landing, and to debug any future regression
that looks like it might have re-introduced the lost wins.
