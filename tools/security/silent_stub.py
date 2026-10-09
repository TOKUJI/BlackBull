"""G2-1/G2-2 gates: verdicts must rest on observed answers.

``just vuln-stub-gate`` (G2-1): two silent listeners stand in for the two
lanes; every check runs in the long tier (so the long-only checks are
covered) under tight bounds.  The gate passes only when no check reports
PASS — silence is never a verdict's evidence.

``just vuln-canary-gate`` (G2-2): a stub answers the first two canaries with
200 "ok" and then dies mid-run; every check after the death must record a
canary failure and be marked FAIL (High).
"""
from __future__ import annotations

import socket
import subprocess
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


def _dying_listener(serves: int) -> tuple[int, socket.socket, threading.Thread]:
    """Answer the first *serves* requests with 200 "ok", then die mid-run."""
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(('127.0.0.1', 0))
    srv.listen(16)
    state = {'served': 0}

    def _serve() -> None:
        while state['served'] < serves:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            try:
                conn.recv(65536)
                conn.sendall(b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\n'
                             b'Connection: keep-alive\r\n\r\nok')
                state['served'] += 1
            finally:
                conn.close()
        srv.close()  # the server dies here; later connections are refused

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
                                         deadline, lane=lane,
                                         canary=probe.Probe.canary))
        return rows
    finally:
        srv1.close()
        srv2.close()


def run_proc_gate() -> list[probe.CheckResult]:
    """G2-3: a stub that holds every accepted socket must show residuals.

    The stub runs as its own process so the /proc observation targets it
    exactly the way it targets a real server; every stub row opens one
    connection the stub never releases."""
    proc = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), '--hold-serve'],
        stdout=subprocess.PIPE, text=True)
    try:
        port = int(proc.stdout.readline().split()[1])
        observer = probe.ProcObserver(proc.pid)
        target = probe.parse_target(f'http://127.0.0.1:{port}')
        runner = probe.Probe(target, 1.0)

        def stub(row: int):
            def _run(probe_runner):
                probe.Probe.canary(probe_runner, 'h1')  # held by the stub
                return probe.Verdict(probe.PASS, f'stub row {row}')
            return probe.Check(f'STUB-{row:03d}', 'stub', 'Low', 'CWE-400',
                               _run, 'h1')

        return probe.run_checks(runner, tuple(stub(i) for i in range(1, 5)),
                                time.monotonic() + 60, lane='h1',
                                observer=observer, observe_settle=0.3)
    finally:
        proc.kill()
        proc.wait(timeout=5)


def run_dying_gate() -> list[probe.CheckResult]:
    """G2-2: checks after a mid-run server death record canary failures."""
    port, srv, _ = _dying_listener(serves=2)
    try:
        target = probe.parse_target(f'http://127.0.0.1:{port}')
        runner = probe.Probe(target, 2.0)

        def stub(row: int):
            def _run(probe_runner):
                return probe.Verdict(probe.PASS, f'stub row {row}')
            return probe.Check(f'STUB-{row:03d}', 'stub', 'Low', 'CWE-400',
                               _run, 'h1')

        rows = probe.run_checks(runner, tuple(stub(i) for i in range(1, 5)),
                                time.monotonic() + 30, lane='h1',
                                canary=probe.Probe.canary)
        return rows
    finally:
        srv.close()


def main(argv: list[str] | None = None) -> int:
    if argv and argv[0] == '--hold-serve':
        port, _, _ = _silent_listener()
        print(f'port {port}', flush=True)
        while True:
            time.sleep(60)
    if argv and argv[0] == '--proc-gate':
        rows = run_proc_gate()
        print(probe.render_table(rows))
        residual = [r.check_id for r in rows if r.proc.startswith('RESIDUAL')]
        unmarked = [r.check_id for r in rows if not r.proc.startswith('RESIDUAL')]
        print(f'G2-3 gate: {len(residual)}/{len(rows)} rows recorded residuals, '
              f'unmarked: {unmarked}')
        return 1 if unmarked else 0
    if argv and argv[0] == '--dying':
        rows = run_dying_gate()
        print(probe.render_table(rows))
        survived = [r.check_id for r in rows[2:]
                    if r.verdict != probe.FAIL or 'canary FAILED' not in r.detail]
        early = [r.check_id for r in rows[:2] if r.verdict != probe.PASS]
        print(f'G2-2 gate: canaries before the death passed ({len(early)} early rows off), '
              f'rows after it unmarked: {survived}')
        return 1 if survived or early else 0
    rows = run_gate(check_timeout=0.5, run_timeout=300.0)
    passed = [r for r in rows if r.verdict == probe.PASS]
    print(probe.render_table(rows))
    print(f'G2-1 gate: {len(rows)} checks against silent peers, '
          f'{len(passed)} PASS')
    return 1 if passed else 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
