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

# Module invocation is preserved across reloads
python -m blackbull.cli myapp:app --bind 127.0.0.1:8000 --reload

# Watch specific directories (repeatable)
blackbull myapp:app --reload --reload-path src/ --reload-path templates/

# In Python
app.run(port=8000, reload=True)
app.run(port=8000, reload=True, reload_paths=['src', 'templates'])
```

## How it works

The master keeps the listeners open while a new Python interpreter runs the
original invocation. The replacement workers finish lifespan startup before
they take new connections on shared listeners. The master then retires the
previous workers. If replacement startup fails or times out, the master logs
the failure and keeps the active workers. Save a corrected file to try again.

During the handoff, workers from two generations can hold connections, memory,
and lifespan resources at the same time. Connection limits apply per worker.
Saves during a handoff are folded into the next reload. Restart the server to
change its listener layout or update the master itself.

Single-owner listeners begin accepting on the replacement only after the old
workers exit. This also applies to a shared listener that accepts a stateful
raw protocol when running with one worker. Connections can wait in the kernel
backlog during this part of the handoff. If a replacement exits after the old
workers have been retired, the master starts a fresh generation.

Reload respects the configured HTTP worker count with a port-bound protocol.
Set `workers=1` if you were relying on the former automatic downgrade.

On a retiring worker:

- HTTP/1.1 closes an idle connection. An underway request or a head already
  in the receive buffer can finish on that worker.
- HTTP/2 sends GOAWAY and lets admitted streams finish. The client can retry
  streams above the advertised last stream ID on a new connection.
- An established WebSocket is closed with code 1001 after a send in progress
  finishes. Reconnect to resume application traffic.
- Raw handlers, including MQTT, own their sessions until they return or the
  drain deadline expires. Reconnect after reload. In-memory MQTT broker state
  is replaced with the worker.

`BB_WORKER_DRAIN_TIMEOUT` bounds all four cases. A request cut off at that
deadline can have an unknown outcome.

Each step logs at `INFO`, so a reload that doesn't happen can be
attributed rather than guessed at:

```
auto-reload: watching /srv/app for *.py changes     # watcher armed
auto-reload: change detected in /srv/app/views.py   # watcher saw the save
reload: change detected — recycling workers         # master acted on it
reload: replacement reached handoff; draining previous workers
```

A missing line names the stage that stalled: no `change detected`
means the watcher never saw the edit (see the clock caveat below);
`change detected` with no `recycling workers` means the master did
not act on it.

## Default watch path

If `--reload-path` is not passed, BlackBull watches the current
working directory.

## Caveats

- **Development only.** Use `--reload` for the edit-save loop. Use the
  deployment supervisor to roll out code in production.
- **First `*.py` change after start may be delayed.**  The
  inotify watcher needs ~1-2 seconds after arm before
  subsequent events fire reliably.  Real users editing files
  seconds after starting the server never notice this; it's
  visible in tests that touch a watched file within the first
  second.
- **A save can be missed if the system clock steps backwards.**
  `watchfiles` polls file mtimes (and forces polling by default
  on WSL), and its poller only treats a file as modified when
  the mtime moves *forward*.  If the clock is stepped back — an
  NTP correction, a VM resume, WSL2 time resync — every save for
  the next few seconds carries an mtime older than the one the
  poller recorded, and those saves are dropped silently.  Saving
  again once the clock has caught up is picked up normally.  If a
  reload doesn't happen, check for `auto-reload: change detected`
  in the log before suspecting the reload machinery: no line
  means the watcher never saw the edit.
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
