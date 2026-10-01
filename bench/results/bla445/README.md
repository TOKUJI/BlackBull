# BLA-445 — the reload accept window, measured

Instrument and raw data for BLA-445: the accept window that opens on a socket
that already has clients while `--reload` re-execs the master, measured against
the cold-start window.  The record — what was measured, how, and what it showed
— is in the issue; this directory is the instrument and its numbers.

## Contents

| file | what |
|---|---|
| `../../scratch/probe.py` | control (no window), cold window and reload window in one run; 40 connections burst mid-window, each with its fate and connect -> first-byte latency |
| `../../scratch/cold_stream.py` | the real-world cold window: one connection every 50 ms from spawn until served |
| `../../scratch/timeline.py` | a staggered connection stream through the reload, with the `ss` LISTEN state |
| `../../scratch/diag.py` | process liveness, fd counts and the LISTEN state across the reload |
| `*.json` | one raw run each; the file name names the family, backlog, park and warm count |

Run it against a checkout with the package importable:

    PYTHONPATH=/path/to/blackbull python ../../scratch/probe.py --family inet \
        --backlog 1024 --park 6 --reload [--warm 5] [--active-warm] \
        [--client-timeout 30]

`--park` extends the lifespan startup so the burst lands mid-window (6 s is
the controlled shape; 0 is the real-world one), `--warm` holds that many
keep-alive connections across the trigger, `--active-warm` keeps them sending
one request a second (so the keep-alive idle timeout never fires and the
window reaches the drain deadline), `--client-timeout` is the patience of the
burst connections.

The server is spawned through a runner file (`run_server.py`), never
`python -c`: the reload re-exec runs `[sys.executable, *sys.argv]`, and for a
`-c` spawn that is `python -c app:app ...` — the string `app:app` parses as an
annotation statement, a no-op, so the re-exec exits 0 and the probe would
record a vanished server as a window.  The instrument's control run (no window
-> 0 affected) is not optional; it is how that defect was found.
