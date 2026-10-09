"""G2-1 gate: a peer that accepts connections and never answers must earn
zero PASS verdicts.

Run with ``just vuln-stub-gate``.  Two silent listeners stand in for the two
lanes; every check runs in the long tier (so the long-only checks are
covered) under tight bounds.  The gate passes only when no check reports
PASS — silence is never a verdict's evidence.
"""
from __future__ import annotations

import socket
import sys
import threading
import time
from pathlib import Path

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import probe
else:
    from tools.security import probe


def _silent_listener() -> tuple[int, socket.socket, threading.Thread]:
    """Accept connections and hold them open without ever writing a byte."""
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(('127.0.0.1', 0))
    srv.listen(64)
    held: list[socket.socket] = []

    def _serve() -> None:
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            held.append(conn)  # kept open: accepted, never answered

    thread = threading.Thread(target=_serve, daemon=True)
    thread.start()
    return srv.getsockname()[1], srv, thread


def run_gate(*, check_timeout: float, run_timeout: float,
             tier: str = 'long') -> list[probe.CheckResult]:
    port1, srv1, _ = _silent_listener()
    port2, srv2, _ = _silent_listener()
    try:
        deadline = time.monotonic() + run_timeout
        budget = probe.ConnectionBudget()
        rows: list[probe.CheckResult] = []
        for lane, url in (('h1', f'http://127.0.0.1:{port1}'),
                          ('h2', f'http://127.0.0.1:{port2}')):
            target = probe.parse_target(url)
            runner = probe.Probe(target, check_timeout, budget=budget, tier=tier)
            rows.extend(probe.run_checks(runner, probe.checks_for(lane, tier),
                                         deadline))
        return rows
    finally:
        srv1.close()
        srv2.close()


def main(argv: list[str] | None = None) -> int:
    rows = run_gate(check_timeout=0.5, run_timeout=300.0)
    passed = [r for r in rows if r.verdict == probe.PASS]
    print(probe.render_table(rows))
    print(f'G2-1 gate: {len(rows)} checks against silent peers, '
          f'{len(passed)} PASS')
    return 1 if passed else 0


if __name__ == '__main__':
    raise SystemExit(main())
