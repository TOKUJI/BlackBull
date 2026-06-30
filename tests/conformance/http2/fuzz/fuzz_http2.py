"""Atheris H2 fuzz harness V2 — FuzzedDataProvider + differential + keep_going.

Strategies (controlled by FuzzedDataProvider):
  70% raw byte mutation
  25% structured H2 frame generation (valid types, HPACK blocks, etc.)
   5% known attack (Rapid Reset RST flood)

Differential mode: BB_FUZZ_NGINX_H2_HOST=127.0.0.1 BB_FUZZ_NGINX_H2_PORT=18443
Use -keep_going=1 to collect multiple divergences without stopping.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import ssl as _ssl_mod
import sys
import time
from multiprocessing import Process
from pathlib import Path

import atheris

with atheris.instrument_imports():
    import requests
    from blackbull import BlackBull
    from blackbull.server import ASGIServer

# ---- app ----
app = BlackBull()

@app.route(path='/')
async def index():
    return 'ok'

@app.route(path='/echo', methods=['POST'])
async def echo(scope, receive, send):
    from blackbull import read_body
    body = await read_body(receive)
    await send({'type': 'http.response.start', 'status': 200,
                'headers': [(b'content-type', b'application/octet-stream')]})
    await send({'type': 'http.response.body', 'body': body})

# ---- config ----
_NGINX_HOST = os.environ.get('BB_FUZZ_NGINX_H2_HOST')
_NGINX_PORT = int(os.environ.get('BB_FUZZ_NGINX_H2_PORT', '0'))
_DIFF = bool(_NGINX_HOST and _NGINX_PORT)

_PREFACE = (b'PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n'
            b'\x00\x00\x00\x04\x00\x00\x00\x00\x00')

_CERT = Path(__file__).parents[3] / 'tests'
_CTX: _ssl_mod.SSLContext | None = None

def _ssl():
    global _CTX
    if _CTX is None:
        _CTX = _ssl_mod.SSLContext(_ssl_mod.PROTOCOL_TLS_CLIENT)
        _CTX.load_verify_locations(str(_CERT / 'cert.pem'))
        _CTX.check_hostname = False
        _CTX.verify_mode = _ssl_mod.CERT_NONE
        _CTX.set_alpn_protocols(['h2'])
    return _CTX

_VALID_TYPES = [0,1,2,3,4,5,6,7,8,9,10]
_EDGE_TYPES  = [11,12,13,14,15,254,255]
_HPACK_GET   = bytes.fromhex('828684418b9788e0b0db17a7638fb1cf4f2f4f3495db178f2f4f')

_USER_CORPUS = Path(__file__).parent / 'user-corpus'
_DIVERGENCE_COUNT = 0


# ---- structured generation ----
def _gen_frames(fdp: atheris.FuzzedDataProvider) -> bytes:
    buf = bytearray()
    n = fdp.ConsumeIntInRange(1, 5)
    for _ in range(n):
        c = fdp.ConsumeIntInRange(0, 99)
        if c < 70:   typ = fdp.PickValueInList(_VALID_TYPES)
        elif c < 85: typ = fdp.PickValueInList(_EDGE_TYPES)
        else:        typ = fdp.ConsumeIntInRange(0, 255)
        flg = fdp.ConsumeIntInRange(0, 255)
        sc = fdp.ConsumeIntInRange(0, 99)
        if sc < 30:      sid = 0
        elif sc < 90:    sid = fdp.ConsumeIntInRange(1, 255) | 1
        else:            sid = fdp.ConsumeIntInRange(0, 0x7FFFFFFF)
        pc = fdp.ConsumeIntInRange(0, 99)
        if pc < 10 and typ == 1:
            pay = _HPACK_GET
        elif pc < 30:
            pay = fdp.ConsumeBytes(fdp.ConsumeIntInRange(0, 50))
        else:
            pay = fdp.ConsumeBytes(fdp.ConsumeIntInRange(0, 16384))
        buf.extend(len(pay).to_bytes(3, 'big'))
        buf.append(typ); buf.append(flg)
        buf.extend(sid.to_bytes(4, 'big'))
        buf.extend(pay)
    if fdp.ConsumeIntInRange(0, 99) < 10:
        buf.extend(fdp.ConsumeBytes(fdp.ConsumeIntInRange(0, 64)))
    return bytes(buf)


# ---- network ----
async def _connect_h2c(port, timeout=5.0):
    return await asyncio.wait_for(
        asyncio.open_connection('127.0.0.1', port), timeout)

async def _connect_h2t(host, port, timeout=5.0):
    return await asyncio.wait_for(
        asyncio.open_connection(host, port, ssl=_ssl()), timeout)

async def _send_recv(r, w, data, timeout=3.0):
    try:
        w.write(_PREFACE + data); await w.drain()
        chunks = []
        while True:
            try: c = await asyncio.wait_for(r.read(4096), timeout=0.5)
            except asyncio.TimeoutError: break
            if not c: break
            chunks.append(c)
        return 'response' if chunks else 'no_response'
    except Exception:
        return 'error'
    finally:
        w.close()
        try: await asyncio.wait_for(w.wait_closed(), timeout=1.0)
        except: pass

def _ready(url, timeout=10.0):
    t0 = time.time()
    while True:
        try:
            if requests.get(url, timeout=0.5, verify=False).status_code in (200, 404):
                return
        except Exception: pass
        if time.time() - t0 > timeout:
            raise RuntimeError(f'{url} not ready')
        time.sleep(0.1)


# ---- fuzz target ----
def TestOneInput(data: bytes) -> None:
    global _DIVERGENCE_COUNT
    if len(data) < 2: return
    fdp = atheris.FuzzedDataProvider(data)
    s = fdp.ConsumeIntInRange(0, 99)

    if s < 70:
        pay = fdp.ConsumeBytes(len(data))
    elif s < 95:
        pay = _gen_frames(fdp)
    else:
        n_rst = fdp.ConsumeIntInRange(5, 40)
        pay = b''.join(
            (0).to_bytes(3,'big') + b'\x03\x00'
            + (fdp.ConsumeIntInRange(1,0x7FFFFFFE)|1).to_bytes(4,'big')
            + b'\x00\x00\x00\x08'
            for _ in range(n_rst))
    if not pay: return

    if _DIFF:
        try:
            br,bw = asyncio.run(_connect_h2c(_PORT, timeout=5.0))
            nr,nw = asyncio.run(_connect_h2t(_NGINX_HOST, _NGINX_PORT, timeout=5.0))
        except Exception: return
        bo = asyncio.run(_send_recv(br, bw, pay, timeout=3.0))
        no = asyncio.run(_send_recv(nr, nw, pay, timeout=3.0))
        if bo != no:
            _DIVERGENCE_COUNT += 1
            d = hashlib.sha1(pay).hexdigest()[:16]
            _USER_CORPUS.mkdir(parents=True, exist_ok=True)
            (_USER_CORPUS / f'div_{_DIVERGENCE_COUNT:03d}_{d}_{bo}_vs_{no}.bin'
             ).write_bytes(pay)
            raise AssertionError(
                f'DIV #{_DIVERGENCE_COUNT}: BB={bo} NG={no} '
                f'len={len(pay)} strat={s}')
        return

    # single-server
    try:
        r,w = asyncio.run(_connect_h2c(_PORT, timeout=3.0))
        asyncio.run(_send_recv(r, w, pay, timeout=3.0))
    except (asyncio.TimeoutError, ConnectionError, OSError, TimeoutError):
        return
    except Exception:
        raise


# ---- main ----
_PORT = 0

def main():
    atheris.Setup(sys.argv, TestOneInput)
    atheris.Fuzz()

if __name__ == '__main__':
    srv = ASGIServer(app); p = None
    try:
        srv.open_socket(0); _PORT = srv.port
        p = Process(target=lambda: asyncio.run(srv.run())); p.start()
        _ready(f'http://localhost:{_PORT}')
        mode = 'differential' if _DIFF else 'single-server'
        print(f'[fuzz-h2] {mode} port={_PORT}', file=sys.stderr)
        if _DIFF:
            print(f'[fuzz-h2] nginx={_NGINX_HOST}:{_NGINX_PORT}', file=sys.stderr)
        main()
    except Exception as e:
        print(f'[fuzz-h2] error: {e}', file=sys.stderr)
    finally:
        srv.close()
        if p: p.terminate(); p.join(timeout=5)
