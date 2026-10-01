"""Timeline probe: one connection every 250 ms through the reload window."""
import json, os, socket, subprocess, sys, threading, time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from probe import APP, CLI_ENTRY

def one_conn(family, addr, t0):
    rec = {'t': time.monotonic() - t0}
    try:
        if family == 'unix':
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        else:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(25.0)
        s.connect(addr)
        rec['connected'] = True
        s.sendall(b'GET /ready HTTP/1.1\r\nHost: x\r\n\r\n')
        data = b''
        while b'\r\n\r\n' not in data:
            chunk = s.recv(65536)
            if not chunk:
                rec['fate'] = 'eof'
                return rec
            data += chunk
        rec['fate'] = 'served'
        rec['latency'] = time.monotonic() - t0 - rec['t']
    except ConnectionRefusedError:
        rec['fate'] = 'refused'
    except ConnectionResetError:
        rec['fate'] = 'reset'
    except OSError as e:
        rec['fate'] = f'oserror:{e.__class__.__name__}:{e.errno}'
    return rec

def main():
    family = sys.argv[1] if len(sys.argv) > 1 else 'inet'
    backlog = int(sys.argv[2]) if len(sys.argv) > 2 else 1024
    park = float(sys.argv[3]) if len(sys.argv) > 3 else 6.0
    tmp = Path(f'/tmp/bla445/tl-{family}-{backlog}')
    tmp.mkdir(parents=True, exist_ok=True)
    (tmp / 'app.py').write_text(APP)
    if family == 'unix':
        addr = str(tmp / 's.sock')
        Path(addr).unlink(missing_ok=True)
        bind = f'unix:{addr}'
    else:
        with socket.socket() as s:
            s.bind(('127.0.0.1', 0))
            port = s.getsockname()[1]
        bind = addr = f'127.0.0.1:{port}'
        addr = ('127.0.0.1', port)
    env = dict(os.environ, WATCHFILES_FORCE_POLLING='1', PYTHONUNBUFFERED='1',
               PYTHONPATH='/home/toshio/work/blackbull-bla459',
               BB_SOCKET_BACKLOG=str(backlog), PARK_STARTUP=str(park))
    log_fh = open(tmp / 'master.log', 'w', buffering=1)
    proc = subprocess.Popen(
        [sys.executable, '-c', CLI_ENTRY, 'app:app', '--bind', bind, '--reload'],
        env=env, cwd=str(tmp), stdout=log_fh, stderr=subprocess.STDOUT,
        text=True, start_new_session=True)
    log_fh.close()

    # wait for steady serving (control point)
    deadline = time.monotonic() + park + 30
    while time.monotonic() < deadline:
        if one_conn(family, addr, 0)['fate'] == 'served':
            break
        time.sleep(0.3)
    time.sleep(0.5)
    control = [one_conn(family, addr, 0) for _ in range(3)]

    t0 = time.monotonic()
    (tmp / 'app.py').write_text(APP + f'# trigger {t0}\n')
    recs = []
    for i in range(int((park + 12) / 0.25)):
        recs.append(one_conn(family, addr, t0))
        time.sleep(0.25)
        if recs[-1]['fate'] == 'served' and recs[-1]['t'] > park / 2:
            break
    proc.terminate()
    try:
        proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        proc.kill()
    print(json.dumps({'family': family, 'backlog': backlog, 'park': park,
                      'control': control, 'timeline': recs}, indent=1))

main()
