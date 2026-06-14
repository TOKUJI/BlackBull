"""Minimal HTTPS+HTTP/2 BlackBull server for h2spec conformance runs.

``h2spec`` exercises RFC 7540 (HTTP/2) + RFC 7541 (HPACK) against any
HTTP/2 endpoint.  The handler body doesn't matter for the framing,
flow-control, and HPACK assertions h2spec cares about — every test
case opens a fresh stream, sends some shape of HEADERS / DATA /
control frames, and inspects the framing / error codes BlackBull
replies with.

ALPN negotiates ``h2`` automatically when the client offers it (which
h2spec does); HTTP/1.1 clients fall back via the same socket.

Run from the repo root::

    python bench/conformance/h2spec_app.py --port 8443 \\
        --cert tests/cert.pem --key tests/key.pem

then point ``h2spec`` at ``https://localhost:8443/``.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from blackbull import BlackBull

app = BlackBull()


@app.route(path='/')
async def root(scope, receive, send):
    await send({'type': 'http.response.start', 'status': 200,
                'headers': [(b'content-type', b'text/plain')]})
    await send({'type': 'http.response.body', 'body': b'ok'})


def _parse_args():
    p = argparse.ArgumentParser(description='BlackBull HTTPS+H2 server for h2spec')
    p.add_argument('--port', type=int, default=8443)
    p.add_argument('--cert', required=True, help='TLS certificate file (PEM)')
    p.add_argument('--key', required=True, help='TLS private key file (PEM)')
    return p.parse_args()


if __name__ == '__main__':
    args = _parse_args()
    print(f'HTTPS+H2 conformance server on https://localhost:{args.port}/')
    app.run(port=args.port, certfile=args.cert, keyfile=args.key)
