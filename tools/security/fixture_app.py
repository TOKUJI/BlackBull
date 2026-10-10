"""Minimal fixture app — the deterministic probe target for BLA-526.

Definition and invariants: docs/security/fixture-app.md.  Start it with
``just vuln-target-up``; it binds 127.0.0.1 only.  The same app serves two
lanes: HTTP/1.1 on ``--port`` (8000) and TLS + ALPN ``h2`` on ``--tls-port``
(8443), so the probe's HTTP/1 and HTTP/2 checks run against one process with
one route table.  The TLS certificate is generated at startup (never
committed); ``--health`` is the recipe's two-lane health oracle.
"""
from dataclasses import dataclass
import gzip
from http import HTTPStatus
from pathlib import Path
import shutil
import ssl
import sys
from urllib.parse import parse_qs

if __package__ in (None, ''):
    # By-path invocation: the project is non-packaged (see pyproject.toml),
    # so blackbull is importable only with the repository root on sys.path.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    import paths as _paths
else:
    from tools.security import paths as _paths

from blackbull import (
    BlackBull, Connection, JSONResponse, Listener, QUERY, Response,
    StreamingResponse, Tcp, WebSocket,
)
from blackbull.middleware import Cache, CORS, Compression, TrustedProxy
from blackbull.utils import Scheme

app = BlackBull()

_STATIC_DIR = Path(__file__).resolve().parent / 'static'

#: The well-known probe certificate (public part only) is published inside
#: the per-user private runtime directory for tools/security/probe.py's TLS
#: verification; see docs/security/fixture-app.md.
cert_path = _paths.cert_file

HOST = '127.0.0.1'
HTTP_PORT = 8000
TLS_PORT = 8443


@app.route(path='/', methods=['GET'])
async def root() -> Response:
    return Response('ok', content_type='text/plain; charset=utf-8')


@app.route(path='/json', methods=['GET'])
async def json_ok() -> JSONResponse:
    return JSONResponse({'ok': True})


@app.route(path='/echo-headers', methods=['GET'])
async def echo_headers(conn: Connection) -> JSONResponse:
    return JSONResponse({
        'headers': [
            [name.decode('latin-1'), value.decode('latin-1')]
            for name, value in conn.headers
        ],
    })


@app.route(path='/echo-body', methods=['POST'])
async def echo_body(body: bytes) -> Response:
    return Response(body, content_type='application/octet-stream')


@app.route(path='/square/{n:int}', methods=['GET'])
async def square(n: int) -> JSONResponse:
    return JSONResponse({'n': n, 'square': n * n})


@app.route(path='/search', methods=[QUERY])
async def search(body: bytes) -> JSONResponse:
    return JSONResponse({'echo': body.decode('utf-8', 'replace')})


@app.route(path='/ws', scheme=Scheme.websocket)
async def ws_echo(ws: WebSocket) -> None:
    await ws.accept()
    async for message in ws:
        await ws.send(message)


app.static('/static', _STATIC_DIR)

# ---- G4-4 surface: parsing, streaming, an error route, middleware ------
#
# The middleware set is the fixture's default startup configuration: CORS,
# Compression, Cache and TrustedProxy run application-wide (the probe's
# ROUTES-005 asserts their contracts), and /pcstatic serves a precompressed
# sibling generated into the private runtime directory at startup.

app.use(CORS(allow_origins=['http://127.0.0.1:8000']))
app.use(Compression())
app.use(Cache())
app.use(TrustedProxy(trusted_proxies=['127.0.0.1/32', '::1/128']))


@dataclass
class Item:
    """Parsed from the JSON request body by the router (JSON -> dataclass)."""

    name: str
    qty: int = 1


@app.route(path='/items', methods=['POST'])
async def create_item(item: Item) -> JSONResponse:
    return JSONResponse({'name': item.name, 'qty': item.qty})


@app.route(path='/form', methods=['POST'])
async def form(body: bytes) -> JSONResponse:
    fields = {key: values[0]
              for key, values in parse_qs(body.decode('utf-8', 'replace'),
                                          keep_blank_values=True).items()}
    return JSONResponse({'fields': fields})


@app.route(path='/stream/{n:int}', methods=['GET'])
async def stream(n: int) -> StreamingResponse:
    """Exactly *n* bytes, chunked deterministically (capped at 1 MiB)."""
    total = max(0, min(n, 1 << 20))

    async def _chunks():
        remaining = total
        chunk = b'x' * 64
        while remaining > 0:
            piece = chunk[:min(remaining, len(chunk))]
            remaining -= len(piece)
            yield piece

    return StreamingResponse(_chunks(),
                             media_type='application/octet-stream')


@app.route(path='/big', methods=['GET'])
async def big() -> Response:
    """A buffered, compressible body (streaming bodies are never compressed)."""
    return Response('compress me ' * 200,
                    content_type='text/plain; charset=utf-8')


@app.route(path='/raise', methods=['GET'])
async def raise_route() -> Response:
    raise RuntimeError('fixture failure')


@app.route(path='/client-ip', methods=['GET'])
async def client_ip(conn: Connection) -> JSONResponse:
    return JSONResponse({'client': conn.client[0]})


def _precompressed_root() -> Path:
    """A static root whose hello.txt has a .gz sibling (precompressed)."""
    root = _paths.runtime_dir() / 'static'
    root.mkdir(parents=True, exist_ok=True)
    src = _STATIC_DIR / 'hello.txt'
    shutil.copyfile(src, root / 'hello.txt')
    with gzip.open(root / 'hello.txt.gz', 'wb') as fh:
        fh.write(src.read_bytes())
    return root


app.static('/pcstatic', _precompressed_root())


#: The declared route table (G4-4).  tests/unit/test_security_probe.py
#: asserts this matches ``app.get_routes()`` exactly — a route added to the
#: app without this table (or the reverse) fails the test.
ROUTE_TABLE: tuple[tuple[str, str], ...] = (
    ('GET', '/'),
    ('GET', '/json'),
    ('GET', '/echo-headers'),
    ('POST', '/echo-body'),
    ('GET', '/square/{n:int}'),
    ('QUERY', '/search'),
    ('GET', '/ws'),
    ('GET', '/static'),
    ('HEAD', '/static'),
    ('GET', '/static/{filepath:path}'),
    ('HEAD', '/static/{filepath:path}'),
    ('POST', '/items'),
    ('POST', '/form'),
    ('GET', '/stream/{n:int}'),
    ('GET', '/big'),
    ('GET', '/raise'),
    ('GET', '/client-ip'),
    ('GET', '/pcstatic'),
    ('HEAD', '/pcstatic'),
    ('GET', '/pcstatic/{filepath:path}'),
    ('HEAD', '/pcstatic/{filepath:path}'),
)


@app.on_error(HTTPStatus.NOT_FOUND)
async def handle_404(conn, receive, send):
    await send(JSONResponse({'error': 'not found'}, status=HTTPStatus.NOT_FOUND))


@app.on_error(HTTPStatus.INTERNAL_SERVER_ERROR)
async def handle_500(conn, receive, send):
    await send(JSONResponse({'error': 'internal server error'},
                            status=HTTPStatus.INTERNAL_SERVER_ERROR))


def make_tls_context() -> ssl.SSLContext:
    """Self-signed loopback TLS context with ALPN ``h2`` first.

    Reuses ``blackbull.fault_injection._tls`` rather than generating a
    certificate here; the helper's key material lives in its own tempdir and
    is removed when the context is collected.  Only the public certificate is
    copied to [`cert_path`][] so the probe (a different process) can verify
    the handshake without trusting blindly.
    """
    from blackbull.fault_injection._tls import make_self_signed_h2_context
    ctx = make_self_signed_h2_context()
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    shutil.copyfile(ctx.bb_ca_cert_path, cert_path())
    return ctx


def listeners(http_port: int = HTTP_PORT, tls_port: int = TLS_PORT) -> list[Listener]:
    return [
        Listener(Tcp(http_port, host=HOST)),
        Listener(Tcp(tls_port, host=HOST), tls=make_tls_context()),
    ]


async def _health(http_port: int, tls_port: int) -> bool:
    """Both lanes must answer GET / with exactly ``ok``."""
    import asyncio
    import urllib.request

    def _http_ok() -> bool:
        url = f'http://{HOST}:{http_port}/'
        return urllib.request.urlopen(url, timeout=2).read() == b'ok'

    loop = asyncio.get_running_loop()
    if not await loop.run_in_executor(None, _http_ok):
        return False

    if not cert_path().is_file():
        return False
    ctx = ssl.create_default_context(cafile=str(cert_path()))
    from blackbull.client.http2 import HTTP2Client
    async with HTTP2Client(HOST, tls_port, ssl=ctx, connect_timeout=2.0) as client:
        res = await client.request('GET', '/')
        return res.status == 200 and res.body == b'ok'


def health(http_port: int = HTTP_PORT, tls_port: int = TLS_PORT) -> bool:
    import asyncio
    try:
        return asyncio.run(asyncio.wait_for(_health(http_port, tls_port), 8.0))
    except Exception:
        return False


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description='BLA-526 probe target app')
    parser.add_argument('--port', type=int, default=HTTP_PORT)
    parser.add_argument('--tls-port', type=int, default=TLS_PORT)
    parser.add_argument('--health', action='store_true',
                        help='exit 0 iff both lanes serve GET / = ok')
    args = parser.parse_args()
    if args.health:
        sys.exit(0 if health(args.port, args.tls_port) else 1)
    app.run(listeners=listeners(args.port, args.tls_port))
