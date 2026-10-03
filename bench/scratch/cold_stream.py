"""Cold window at park=0: a staggered stream from spawn until served."""
import os, socket, sys, time
from pathlib import Path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from probe import APP, RUNNER, Conn, spawn

def main(family):
    tmp = Path(f'/tmp/bla445/cold-{family}'); os.makedirs(tmp, exist_ok=True)
    if family == 'unix':
        where = str(tmp / 's.sock'); Path(where).unlink(missing_ok=True)
        bind, addr = f'unix:{where}', where
    else:
        with socket.socket() as s:
            s.bind(('127.0.0.1', 0)); port = s.getsockname()[1]
        bind = f'127.0.0.1:{port}'; addr = ('127.0.0.1', port)
    conn = Conn(family, addr, 5.0)
    t0 = time.monotonic()
    proc = spawn(tmp, bind, 0.0, False)
    recs = []
    for i in range(60):
        r = conn.probe(t0)
        recs.append(r)
        if r['fate'].startswith('served'):
            break
        time.sleep(0.05)
    proc.terminate(); proc.wait(timeout=20)
    from collections import Counter
    print(family, 'first_serve_s=', round(recs[-1]['t'] + recs[-1].get('latency', 0), 3),
          'fates=', dict(Counter(r['fate'] for r in recs)),
          'first_refused_t=', next((round(r['t'], 3) for r in recs if r['fate'] == 'refused'), None))

main(sys.argv[1])
