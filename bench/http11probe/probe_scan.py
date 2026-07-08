"""Local substitute for the .NET Http11Probe — fires the exact Cluster A/B/C
vectors from the baseline proposal at a live BlackBull server and reports the
actual status / behaviour, so we can see the post-Sprint-61 delta and what
Sprint 63 still needs to fix.  Not a pytest file; run directly."""
import asyncio
import socket
from http import HTTPMethod
from multiprocessing import Process

from blackbull import BlackBull, read_body
from blackbull.server import ASGIServer


def make_app():
    app = BlackBull()

    @app.route(path='/', methods=[HTTPMethod.GET, HTTPMethod.POST])
    async def index(scope, receive, send):
        await read_body(receive)
        await send({'type': 'http.response.start', 'status': 200,
                    'headers': [(b'content-type', b'text/plain')]})
        await send({'type': 'http.response.body', 'body': b'ok'})

    @app.route(path='/echo', methods=[HTTPMethod.GET, HTTPMethod.POST])
    async def echo(scope, receive, send):
        body = await read_body(receive)
        await send({'type': 'http.response.start', 'status': 200,
                    'headers': [(b'content-type', b'application/octet-stream')]})
        await send({'type': 'http.response.body', 'body': body})

    return app


def send_raw(port, request, timeout=2.0):
    sock = socket.create_connection(('127.0.0.1', port), timeout=timeout)
    try:
        sock.sendall(request)
        try:
            sock.shutdown(socket.SHUT_WR)
        except OSError:
            pass
        chunks = []
        sock.settimeout(timeout)
        try:
            while True:
                buf = sock.recv(4096)
                if not buf:
                    break
                chunks.append(buf)
        except (socket.timeout, TimeoutError):
            return b''.join(chunks), 'TIMEOUT'
    finally:
        sock.close()
    return b''.join(chunks), 'CLOSED'


def status_of(raw):
    if not raw:
        return None
    line = raw.split(b'\r\n', 1)[0]
    parts = line.split(b' ', 2)
    if len(parts) >= 2 and parts[0].startswith(b'HTTP/'):
        try:
            return int(parts[1])
        except ValueError:
            return None
    return None


# (name, expected-desc, raw request)
VECTORS = [
    # Cluster A — chunked
    ('SMUG-CHUNK-NEGATIVE', '400',
     b'POST /echo HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n-1\r\nhello\r\n0\r\n\r\n'),
    ('SMUG-CHUNK-HEX-PREFIX', '400',
     b'POST /echo HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n0x5\r\nhello\r\n0\r\n\r\n'),
    ('SMUG-CHUNK-LEADING-SP', '400',
     b'POST /echo HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n 5\r\nhello\r\n0\r\n\r\n'),
    ('SMUG-CHUNK-SIZE-PLUS', '400',
     b'POST /echo HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n+0\r\n\r\n'),
    ('SMUG-CHUNK-UNDERSCORE', '400',
     b'POST /echo HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n1_0\r\n0123456789abcdef\r\n0\r\n\r\n'),
    ('SMUG-CHUNK-BARE-SEMICOLON', '400',
     b'POST /echo HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n5;\r\nhello\r\n0\r\n\r\n'),
    ('SMUG-CHUNK-EXT-INVALID-TOKEN', '400',
     b'POST /echo HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n5;a@b=c\r\nhello\r\n0\r\n\r\n'),
    ('SMUG-CHUNK-SIZE-TRAILING-OWS', '400',
     b'POST /echo HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n5 \r\nhello\r\n0\r\n\r\n'),
    ('SMUG-CHUNK-SPILL', '400',
     b'POST /echo HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n5\r\nhelloEXTRA\r\n0\r\n\r\n'),
    ('SMUG-CHUNK-EXT-CTRL', '400',
     b'POST /echo HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n5;a=\x01\r\nhello\r\n0\r\n\r\n'),
    # Cluster B — special URI forms
    ('COMP-ABSOLUTE-FORM', '2xx',
     b'GET http://localhost/ HTTP/1.1\r\nHost: localhost\r\n\r\n'),
    ('COMP-ASTERISK-WITH-GET', '400/close',
     b'GET * HTTP/1.1\r\nHost: localhost\r\n\r\n'),
    ('COMP-OPTIONS-STAR', '2xx/405',
     b'OPTIONS * HTTP/1.1\r\nHost: localhost\r\n\r\n'),
    ('COMP-METHOD-CONNECT', '400/405/501',
     b'CONNECT localhost:80 HTTP/1.1\r\nHost: localhost\r\n\r\n'),
    ('SMUG-ABSOLUTE-URI-HOST-MISMATCH', '400/2xx',
     b'GET http://evil.com/ HTTP/1.1\r\nHost: localhost\r\n\r\n'),
    # Cluster C — protocol validation
    ('COMP-HOST-WITH-USERINFO', '400',
     b'GET / HTTP/1.1\r\nHost: user@localhost\r\n\r\n'),
    ('MAL-NON-ASCII-URL', '400',
     b'GET /\xc3\xa9 HTTP/1.1\r\nHost: localhost\r\n\r\n'),
    ('SMUG-TE-NOT-FINAL-CHUNKED', '400',
     b'POST /echo HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked, gzip\r\n\r\n5\r\nhello\r\n0\r\n\r\n'),
]


def main():
    app = make_app()
    server = ASGIServer(app)
    server.open_socket(0)
    p = Process(target=lambda: asyncio.run(server.run()))
    p.start()
    try:
        server.wait_for_port(timeout=10.0)
        port = server.port
        print(f'{"TEST":<32} {"EXPECT":<12} {"GOT":<8} {"CONN"}')
        print('-' * 64)
        for name, expect, req in VECTORS:
            raw, conn = send_raw(port, req)
            st = status_of(raw)
            print(f'{name:<32} {expect:<12} {str(st):<8} {conn}')
    finally:
        server.close()
        p.terminate()
        p.join(timeout=5)


if __name__ == '__main__':
    main()
