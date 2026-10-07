"""Minimal fixture app — the deterministic probe target for BLA-526.

Definition and invariants: docs/security/fixture-app.md.  Start it with
``just vuln-target-up``; it binds 127.0.0.1 only.
"""
from http import HTTPStatus
from pathlib import Path
import sys

if __package__ in (None, ''):
    # By-path invocation: the project is non-packaged (see pyproject.toml),
    # so blackbull is importable only with the repository root on sys.path.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from blackbull import (
    BlackBull, Connection, JSONResponse, Listener, QUERY, Response, Tcp,
)

app = BlackBull()

_STATIC_DIR = Path(__file__).resolve().parent / 'static'


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


app.static('/static', _STATIC_DIR)


@app.on_error(HTTPStatus.NOT_FOUND)
async def handle_404(conn, receive, send):
    await send(JSONResponse({'error': 'not found'}, status=HTTPStatus.NOT_FOUND))


@app.on_error(HTTPStatus.INTERNAL_SERVER_ERROR)
async def handle_500(conn, receive, send):
    await send(JSONResponse({'error': 'internal server error'},
                            status=HTTPStatus.INTERNAL_SERVER_ERROR))


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description='BLA-526 probe target app')
    parser.add_argument('--port', type=int, default=8000)
    args = parser.parse_args()
    app.run(listeners=[Listener(Tcp(args.port, host='127.0.0.1'))])
