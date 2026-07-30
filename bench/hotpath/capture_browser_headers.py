#!/usr/bin/env python3
"""Capture the header lines a *real* browser sends, per connection.

`line_cache_realistic.py` encodes an assumption about what Chrome sends and how
it varies.  An assumption about the input is exactly the thing a measurement
should not rest on, and the classic web-workload literature (SURGE and
descendants) does not help here — it models *which resources are requested*,
their sizes, popularity and temporal locality, not the header lines that
accompany them.

So this takes the ground truth instead: serve a page with real subresources,
point a real Chromium at it, and record the exact bytes of every request head,
grouped by TCP connection.  The output is a JSON file of
``[[line, ...], ...]`` per connection, which `line_cache_hitrate.py` scores.

Two properties of real browser behaviour that a hand-written model is likely to
get wrong, and that this captures for free:

- **Connection parallelism.**  Chromium opens up to six connections per origin
  and spreads a page's requests across them, so each connection sees a fraction
  of the page — which lowers the per-connection repeat rate.
- **Header variation the model did not think of.**  Whatever Chrome actually
  does with `Priority`, `Sec-Fetch-*`, client hints and conditional requests.

    python bench/hotpath/capture_browser_headers.py --out capture.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import subprocess
import tempfile

# A page shaped like a real one: stylesheets, scripts, fonts, images, and an
# XHR issued after load.  Counts are what matters, not the bytes.
IMAGES = [f'/img/thumb-{i}.png' for i in range(12)]
SUBRESOURCES = (['/static/app.css', '/static/theme.css']
                + ['/static/vendor.js', '/static/app.js', '/static/analytics.js']
                + IMAGES)

INDEX = """<!doctype html><html><head><meta charset=utf-8><title>capture</title>
<link rel=stylesheet href=/static/app.css><link rel=stylesheet href=/static/theme.css>
<script src=/static/vendor.js></script><script src=/static/app.js></script>
</head><body>
""" + ''.join(f'<img src="{p}" width=10 height=10>' for p in IMAGES) + """
<script src=/static/analytics.js></script>
<script>
fetch('/api/session', {headers: {'X-Requested-With': 'XMLHttpRequest'}})
  .then(() => fetch('/api/prefs'))
  .then(() => setTimeout(() => { document.title = 'done'; }, 300));
</script>
</body></html>"""

BODIES = {
    '/': (b'text/html; charset=utf-8', INDEX.encode()),
    '/api/session': (b'application/json', b'{"ok":true}'),
    '/api/prefs': (b'application/json', b'{"theme":"dark"}'),
}
for _p in SUBRESOURCES:
    if _p.endswith('.css'):
        BODIES[_p] = (b'text/css', b'body{margin:0}')
    elif _p.endswith('.js'):
        BODIES[_p] = (b'application/javascript', b'/*x*/')
    else:
        # 1x1 PNG
        BODIES[_p] = (b'image/png', bytes.fromhex(
            '89504e470d0a1a0a0000000d4948445200000001000000010806000000'
            '1f15c4890000000a49444154789c6300010000050001'
            '0d0a2db40000000049454e44ae426082'))


class Capture(asyncio.Protocol):
    """Minimal HTTP/1.1 responder that records each request's header lines."""

    def __init__(self, sink: list[list[list[str]]]) -> None:
        self._sink = sink
        self._mine: list[list[str]] = []
        self._buf = b''

    def connection_made(self, transport) -> None:
        self._t = transport
        self._sink.append(self._mine)

    def data_received(self, data: bytes) -> None:
        self._buf += data
        while b'\r\n\r\n' in self._buf:
            head, _, self._buf = self._buf.partition(b'\r\n\r\n')
            lines = head.split(b'\r\n')
            # Record the header lines only; the request line is not cacheable
            # (its target differs every time by construction).
            self._mine.append([ln.decode('latin-1') for ln in lines[1:] if ln])
            target = lines[0].split(b' ')[1].decode('latin-1')
            ctype, body = BODIES.get(target, (b'text/plain', b'nope'))
            self._t.write(
                b'HTTP/1.1 200 OK\r\ncontent-type: ' + ctype
                + b'\r\ncontent-length: ' + str(len(body)).encode()
                # no-store so a second load re-requests everything rather than
                # answering from cache and hiding the header sets.
                + b'\r\ncache-control: no-store\r\n'
                b'access-control-allow-origin: *\r\n\r\n' + body)


async def serve(sink, port: int):
    loop = asyncio.get_running_loop()
    server = await loop.create_server(lambda: Capture(sink), '0.0.0.0', port)
    return server


EDGE = '/mnt/c/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--out', type=pathlib.Path, required=True)
    ap.add_argument('--port', type=int, default=8788)
    ap.add_argument('--browser', default=EDGE)
    ap.add_argument('--settle', type=float, default=8.0)
    args = ap.parse_args()

    sink: list[list[list[str]]] = []
    server = await serve(sink, args.port)

    profile = tempfile.mkdtemp(prefix='hdrcap-')
    # Windows-side Chromium reaches a WSL2 listener through localhost
    # forwarding.  A throwaway profile keeps a warm cache or an existing
    # session from suppressing requests.
    proc = subprocess.Popen(
        [args.browser, '--headless=new', '--disable-gpu', '--no-first-run',
         f'--user-data-dir={profile}'.replace('/mnt/c', 'C:').replace('/', '\\'),
         '--disable-features=NetworkServiceInProcess',
         f'http://localhost:{args.port}/'],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        await asyncio.sleep(args.settle)
    finally:
        proc.terminate()
        server.close()
        await server.wait_closed()

    conns = [c for c in sink if c]
    args.out.write_text(json.dumps(conns, indent=1))
    reqs = sum(len(c) for c in conns)
    print(f'captured {reqs} requests over {len(conns)} connections '
          f'-> {args.out}')
    for i, c in enumerate(conns):
        print(f'  conn {i}: {len(c)} requests')
    return 0 if reqs else 1


if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))
