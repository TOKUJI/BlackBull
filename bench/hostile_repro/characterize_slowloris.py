"""Slowloris quantitative characterisation (Sprint 77).

Turns the qualitative "the three deadline timeouts work" claim in
`KNOWN_LIMITATIONS.md` into a defendable curve:

    with N concurrent Slowloris-header connections held open, what is the
    acceptance+service latency for a fresh *legitimate* HTTP/1.1 request?

A correctly-defended server reaps each slow connection at
`BB_HEADER_TIMEOUT` and keeps serving legitimate traffic with flat
latency as N grows; a vulnerable one lets held slots crowd out the
accept loop and latency climbs (or requests time out) with N.

Self-contained: boots its own minimal single-worker BlackBull server
(warmed before it accepts), so it needs no httparena dataset.  The
attacker holds N sockets that have sent a partial request head and never
finish it; the prober opens a burst of fresh connections and measures
each one's full round-trip.

Usage:
    python bench/hostile_repro/characterize_slowloris.py \
        [--sweep 0,256,512,1024,2048] [--probes 50] [--hold 8] \
        [--header-timeout 10] [--port 8099]

Output: a table (N, ok/total, p50, p90, p99, max ms) to stdout and, with
--out DIR, a CSV + a run-provenance note under that dir.  Numbers are
WSL2-local iteration; the EC2 window re-runs the same driver for the
record (see bench/patterns note in the sprint log).
"""
from __future__ import annotations

import argparse
import asyncio
import os
import socket
import statistics
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

SERVER_SRC = '''
import os
from blackbull import BlackBull
app = BlackBull()

@app.route(path="/healthz")
async def healthz():
    return "ok"

if __name__ == "__main__":
    app.run(port=int(os.environ["BB_PORT"]))
'''


def _wait_healthy(port: int, timeout: float = 15.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(('127.0.0.1', port), timeout=1) as s:
                s.sendall(b'GET /healthz HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n')
                if b'200' in s.recv(256):
                    return True
        except OSError:
            time.sleep(0.2)
    return False


async def _hold_slowloris(port: int, n: int, stop: asyncio.Event) -> list:
    """Open *n* connections, send a partial request head, then dribble one
    header byte every few seconds — never completing the head."""
    conns: list = []

    async def one():
        try:
            r, w = await asyncio.open_connection('127.0.0.1', port)
        except OSError:
            return
        # Partial head: request line + Host, but no terminating CRLF CRLF.
        w.write(b'GET /healthz HTTP/1.1\r\nHost: x\r\n')
        try:
            await w.drain()
        except OSError:
            return
        conns.append(w)
        # Dribble a throwaway header byte periodically to look alive.
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=3.0)
            except asyncio.TimeoutError:
                try:
                    w.write(b'X')
                    await w.drain()
                except OSError:
                    return

    await asyncio.gather(*(one() for _ in range(n)))
    return conns


async def _probe_once(port: int, timeout: float) -> float | None:
    """One fresh legitimate request; return full RTT in ms, or None on
    failure/timeout."""
    t0 = time.perf_counter()
    try:
        r, w = await asyncio.wait_for(
            asyncio.open_connection('127.0.0.1', port), timeout=timeout)
    except (OSError, asyncio.TimeoutError):
        return None
    try:
        w.write(b'GET /healthz HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n')
        await asyncio.wait_for(w.drain(), timeout=timeout)
        data = await asyncio.wait_for(r.read(256), timeout=timeout)
    except (OSError, asyncio.TimeoutError):
        return None
    finally:
        w.close()
        try:
            await w.wait_closed()
        except OSError:
            pass
    if b'200' not in data:
        return None
    return (time.perf_counter() - t0) * 1000.0


async def _probe_burst(port: int, probes: int, timeout: float) -> list:
    """Fire *probes* fresh requests with light concurrency, collect RTTs."""
    sem = asyncio.Semaphore(16)

    async def guarded():
        async with sem:
            return await _probe_once(port, timeout)

    return await asyncio.gather(*(guarded() for _ in range(probes)))


async def _run_point(port: int, n: int, probes: int, hold: float,
                     timeout: float) -> dict:
    stop = asyncio.Event()
    holders: list = []
    if n:
        holders = await _hold_slowloris(port, n, stop)
        # Let the slow connections settle into the server's parked state.
        await asyncio.sleep(min(hold, 2.0))

    results = await _probe_burst(port, probes, timeout)
    stop.set()
    for w in holders:
        w.close()

    oks = [r for r in results if r is not None]
    fails = len(results) - len(oks)
    oks.sort()

    def pct(p: float) -> float:
        if not oks:
            return float('nan')
        k = min(len(oks) - 1, int(round(p / 100 * (len(oks) - 1))))
        return oks[k]

    return {
        'n': n, 'ok': len(oks), 'total': len(results), 'fail': fails,
        'held': len(holders),
        'p50': pct(50), 'p90': pct(90), 'p99': pct(99),
        'max': max(oks) if oks else float('nan'),
        'mean': statistics.fmean(oks) if oks else float('nan'),
    }


async def _main_async(args, port: int) -> list:
    rows = []
    for n in args.sweep:
        row = await _run_point(port, n, args.probes, args.hold, args.timeout)
        rows.append(row)
        print(f'  N={n:<6} held={row["held"]:<6} ok={row["ok"]}/{row["total"]} '
              f'fail={row["fail"]:<4} p50={row["p50"]:.1f} p90={row["p90"]:.1f} '
              f'p99={row["p99"]:.1f} max={row["max"]:.1f} ms')
        # Give reaped slots time to fully clear before the next point.
        await asyncio.sleep(2.0)
    return rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('--sweep', default='0,256,512,1024,2048',
                    help='comma list of slow-connection counts')
    ap.add_argument('--probes', type=int, default=50)
    ap.add_argument('--hold', type=float, default=8.0)
    ap.add_argument('--header-timeout', type=int, default=10)
    ap.add_argument('--timeout', type=float, default=10.0,
                    help='per-probe deadline (s)')
    ap.add_argument('--port', type=int, default=8099)
    ap.add_argument('--out', default=None)
    args = ap.parse_args()
    args.sweep = [int(x) for x in args.sweep.split(',') if x != '']

    srv_path = REPO / 'bench' / 'hostile_repro' / '_slowloris_server.py'
    srv_path.write_text(SERVER_SRC)

    env = dict(os.environ)
    env['BB_PORT'] = str(args.port)
    env['BB_HEADER_TIMEOUT'] = str(args.header_timeout)
    env['BB_ACCESS_LOG'] = '0'

    print(f'=== Slowloris characterisation (BB_HEADER_TIMEOUT='
          f'{args.header_timeout}s, port={args.port}) ===')
    proc = subprocess.Popen([sys.executable, str(srv_path)], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        if not _wait_healthy(args.port):
            print('FATAL: server did not become healthy', file=sys.stderr)
            proc.terminate()
            sys.exit(1)
        rows = asyncio.run(_main_async(args, args.port))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        srv_path.unlink(missing_ok=True)

    if args.out:
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        csv = out / 'slowloris_curve.csv'
        with csv.open('w') as f:
            f.write('n,held,ok,total,fail,p50_ms,p90_ms,p99_ms,max_ms,mean_ms\n')
            for r in rows:
                f.write(f'{r["n"]},{r["held"]},{r["ok"]},{r["total"]},'
                        f'{r["fail"]},{r["p50"]:.2f},{r["p90"]:.2f},'
                        f'{r["p99"]:.2f},{r["max"]:.2f},{r["mean"]:.2f}\n')
        print(f'\nwrote {csv}')

    # Verdict: healthy if fresh-connection latency stays flat and no probe
    # fails as N climbs.  A rising p99 or any fail is the signal the accept
    # path is being crowded out.
    base = rows[0]['p99'] if rows else float('nan')
    worst = max((r['p99'] for r in rows if r['ok']), default=float('nan'))
    any_fail = any(r['fail'] for r in rows)
    print(f'\nbaseline(N=0) p99={base:.1f} ms ; worst p99={worst:.1f} ms ; '
          f'any-probe-failure={any_fail}')


if __name__ == '__main__':
    main()
