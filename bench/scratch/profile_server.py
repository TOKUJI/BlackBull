#!/usr/bin/env python3
"""Run native_app under cProfile; dump on SIGTERM.

Usage: profile_server.py <port> <out.prof>

Background jobs ignore SIGINT (POSIX), so the runner signals SIGTERM.  A
SIGTERM handler raises SystemExit, which unwinds app.run and lets the finally
block dump the profile — no run_path sys.path games, no kill -9 race.
"""
import cProfile
import signal
import sys


def _stop(signum, frame):  # noqa: ARG001
    raise SystemExit(0)


def main() -> None:
    port = int(sys.argv[1])
    out = sys.argv[2]
    signal.signal(signal.SIGTERM, _stop)
    # Import after argv parse so this module's own import cost stays out of
    # the profile (enabled below).
    from bench.peers.native_app import app  # noqa: PLC0415

    prof = cProfile.Profile()
    prof.enable()
    try:
        app.run(port=port, workers=1)
    finally:
        prof.disable()
        prof.dump_stats(out)


if __name__ == "__main__":
    main()
