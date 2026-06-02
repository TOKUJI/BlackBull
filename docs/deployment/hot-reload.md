# Hot reload

`--reload` (or `reload=True` to `app.run()`) restarts workers
when source files change, so you don't have to stop and restart
the server every time you edit code.  Intended for development
only.

## Setup

Install the `reload` extra — it pulls in
[`watchfiles`](https://watchfiles.helpmanual.io/), the
notify-driven file watcher:

```bash
pip install 'blackbull[reload]'
```

## Use

```bash
# Watch the current working directory
blackbull myapp:app --bind 127.0.0.1:8000 --reload

# Watch specific directories (repeatable)
blackbull myapp:app --reload --reload-path src/ --reload-path templates/

# In Python
app.run(port=8000, reload=True)
app.run(port=8000, reload=True, reload_paths=['src', 'templates'])
```

## How it works

When `--reload` is on, BlackBull's master process:

1. Watches the configured paths for `*.py` file changes.
2. On a change, sends `SIGTERM` to the running workers.
3. Re-execs itself (`os.execvp`) with the original argv,
   marking listening sockets as inheritable so the new master
   can adopt them.
4. The fresh master forks workers from the new code.

The kernel multiplexes the same listening fd across master
generations — no socket is ever closed, so connections in
flight during a reload finish under the old workers, and new
connections route to new workers transparently.

## Default watch path

If `--reload-path` is not passed, BlackBull watches the current
working directory.

## Caveats

- **Development only.**  `--reload` exists for the edit-save
  loop.  Don't run it in production — re-execing the process on
  every file change is the opposite of what production wants.
- **First `*.py` change after start may be delayed.**  The
  inotify watcher needs ~1-2 seconds after arm before
  subsequent events fire reliably.  Real users editing files
  seconds after starting the server never notice this; it's
  visible in tests that touch a watched file within the first
  second.
- **Watches `*.py` only.**  Template / static file changes
  don't trigger a reload — those usually don't require a
  worker restart anyway since they're read on every request.

## Production alternative

Production deployments don't need hot-reload — they need
zero-downtime restarts on deploy.  For that, systemd socket
activation keeps the listening socket open across service
restarts; see
[Unix and fd inheritance](unix-and-fd.md#fd-inheritance-systemd-socket-activation).

## Next

- [Running BlackBull](running.md) — the broader entry-point
  overview.
- [Workers](workers.md) — multi-worker behaviour outside of
  reload (production deployment).
