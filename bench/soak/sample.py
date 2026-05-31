"""bench/soak/sample.py — Sprint 28 Task 2 soak-harness sampler.

Polls server-process metrics every INTERVAL seconds and appends one
row per sample to sample.csv:

  ts_unix, vmrss_kb, vmsize_kb, threads, open_fds, established_conns,
  tracemalloc_current_bytes, tracemalloc_peak_bytes

Tracemalloc current/peak are pulled from the server's /tracemalloc
HTTP endpoint (BB_TRACEMALLOC=1).  When the endpoint is unreachable
(server starting, shutting down, transient) the row is still written
with the local metrics — only tracemalloc cols are blank.

Usage:
  python bench/soak/sample.py --pid <PID> --port <PORT> --out <DIR> \\
                              [--interval 60] [--duration 0]

  --duration 0 means run until SIGTERM/SIGINT.

At shutdown, dumps the final tracemalloc top-N snapshot to
tracemalloc-final.json if the endpoint is still up at that moment;
otherwise the final row in sample.csv is the last available data.
"""
import argparse
import json
import os
import signal
import sys
import time
import urllib.request
import urllib.error


def _read_proc_status(pid: int) -> dict[str, int]:
    """Return VmRSS, VmSize, Threads from /proc/<pid>/status (kB)."""
    out = {}
    try:
        with open(f'/proc/{pid}/status') as f:
            for line in f:
                if line.startswith('VmRSS:'):
                    out['vmrss_kb'] = int(line.split()[1])
                elif line.startswith('VmSize:'):
                    out['vmsize_kb'] = int(line.split()[1])
                elif line.startswith('Threads:'):
                    out['threads'] = int(line.split()[1])
    except FileNotFoundError:
        pass
    return out


def _open_fds(pid: int) -> int:
    try:
        return len(os.listdir(f'/proc/{pid}/fd'))
    except FileNotFoundError:
        return -1


def _established_conns(port: int) -> int:
    """Count TCP connections to <port> in ESTABLISHED state.

    Reads /proc/net/tcp and /proc/net/tcp6.  Server-side ESTABLISHED
    sockets have local-addr port = <port>; the entry's state field is
    01 (ESTABLISHED) per the kernel's tcp_states.h.
    """
    target_hex = f':{port:04X}'
    total = 0
    for path in ('/proc/net/tcp', '/proc/net/tcp6'):
        try:
            with open(path) as f:
                next(f)  # header
                for line in f:
                    parts = line.split()
                    if len(parts) < 4:
                        continue
                    local = parts[1]
                    state = parts[3]
                    if state == '01' and local.endswith(target_hex):
                        total += 1
        except FileNotFoundError:
            pass
    return total


def _fetch_tracemalloc(base: str, timeout: float = 2.0) -> dict | None:
    try:
        with urllib.request.urlopen(f'{base}/tracemalloc', timeout=timeout) as r:
            if r.status != 200:
                return None
            return json.loads(r.read().decode())
    except (urllib.error.URLError, OSError, ValueError):
        return None


def main():
    p = argparse.ArgumentParser(description='BlackBull soak sampler')
    p.add_argument('--pid', type=int, required=True,
                   help='Server process PID')
    p.add_argument('--port', type=int, default=8000,
                   help='Server listen port (for connection counting + tracemalloc)')
    p.add_argument('--out', required=True,
                   help='Output directory (must exist)')
    p.add_argument('--interval', type=float, default=60.0,
                   help='Sampling interval in seconds (default 60)')
    p.add_argument('--duration', type=float, default=0,
                   help='Total duration in seconds (0 = until signalled)')
    args = p.parse_args()

    csv_path = os.path.join(args.out, 'sample.csv')
    final_path = os.path.join(args.out, 'tracemalloc-final.json')
    base = f'http://localhost:{args.port}'

    headers = ['ts_unix', 'vmrss_kb', 'vmsize_kb', 'threads',
               'open_fds', 'established_conns',
               'tm_current_bytes', 'tm_peak_bytes']
    f = open(csv_path, 'w', buffering=1)  # line-buffered so tail -f works
    f.write(','.join(headers) + '\n')

    _running = True

    def _stop(*_):
        nonlocal _running
        _running = False

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    t_start = time.time()
    last_tm = None
    while _running:
        ts = int(time.time())
        proc = _read_proc_status(args.pid)
        fds = _open_fds(args.pid)
        conns = _established_conns(args.port)
        tm = _fetch_tracemalloc(base)
        if tm is not None:
            last_tm = tm

        row = [
            str(ts),
            str(proc.get('vmrss_kb', '')),
            str(proc.get('vmsize_kb', '')),
            str(proc.get('threads', '')),
            str(fds) if fds >= 0 else '',
            str(conns),
            str(tm['current_bytes']) if tm else '',
            str(tm['peak_bytes']) if tm else '',
        ]
        f.write(','.join(row) + '\n')

        if args.duration > 0 and (time.time() - t_start) >= args.duration:
            break

        # Sleep in 1-second chunks so SIGTERM lands fast.
        slept = 0.0
        while _running and slept < args.interval:
            time.sleep(min(1.0, args.interval - slept))
            slept += 1.0

    f.close()

    # Best-effort final snapshot dump — useful when the orchestrator
    # stops sampler before the server.
    final = _fetch_tracemalloc(base, timeout=5.0) or last_tm
    if final is not None:
        with open(final_path, 'w') as out:
            json.dump(final, out, indent=2)


if __name__ == '__main__':
    main()
