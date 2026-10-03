import os, socket, subprocess, sys, time
from pathlib import Path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from probe import APP, CLI_ENTRY

tmp = Path('/tmp/bla445/diag-run'); tmp.mkdir(exist_ok=True)
(tmp / 'app.py').write_text(APP)
with socket.socket() as s:
    s.bind(('127.0.0.1', 0)); port = s.getsockname()[1]
env = dict(os.environ, WATCHFILES_FORCE_POLLING='1', PYTHONUNBUFFERED='1',
           PYTHONPATH='/home/toshio/work/blackbull-bla459', PARK_STARTUP='6')
log_fh = open(tmp / 'master.log', 'w', buffering=1)
proc = subprocess.Popen([sys.executable, '-c', CLI_ENTRY, 'app:app',
                         '--bind', f'127.0.0.1:{port}', '--reload'],
                        env=env, cwd=str(tmp), stdout=log_fh,
                        stderr=subprocess.STDOUT, text=True)
log_fh.close()
def conn():
    try:
        s = socket.socket(); s.settimeout(1.0)
        s.connect(('127.0.0.1', port))
        s.sendall(b'GET /ready HTTP/1.1\r\nHost: x\r\n\r\n')
        ok = b'ready' in s.recv(4096); s.close()
        return 'served' if ok else 'no-ready'
    except ConnectionRefusedError:
        return 'refused'
    except OSError as e:
        return f'{type(e).__name__}'
def fds():
    try:
        n = len([f for f in os.listdir(f'/proc/{proc.pid}/fd')
                 if 'socket' in os.readlink(f'/proc/{proc.pid}/fd/{f}')])
    except OSError:
        n = -1
    kids = subprocess.run(['pgrep', '-P', str(proc.pid)], capture_output=True,
                          text=True).stdout.split()
    return f'master_fds={n} kids={kids}'
for _ in range(80):
    if conn() == 'served':
        break
    time.sleep(0.3)
print('steady:', fds())
t0 = time.monotonic()
(tmp / 'app.py').write_text(APP + f'# {t0}\n')
for i in range(30):
    print(f't={time.monotonic()-t0:5.2f} {conn():9} poll={proc.poll()} {fds()}')
    time.sleep(0.3)
print('log tail:', tmp.joinpath('master.log').read_text()[-400:])
