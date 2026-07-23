"""
Http11Probe target server for BlackBull.

Endpoints:
  GET  /        → 200 OK "Hello, world!"
  POST /        → 200 OK with request body echoed back
  GET  /echo    → 200 OK with all request headers echoed (name: value\\n)
  POST /echo    → same as GET /echo
  GET  /cookie  → 200 OK with parsed cookies echoed (name=value\\n)
  POST /cookie  → same as GET /cookie
"""
from http import HTTPStatus
from blackbull import BlackBull
from blackbull.response import Response

app = BlackBull()


# ── Root: GET returns plain text, POST echoes body ──────────────────────

@app.route(path='/', methods=['GET'])
async def root_get():
    return Response("Hello, world!", content_type="text/plain; charset=utf-8")


@app.route(path='/', methods=['POST'])
async def root_post(body: bytes):
    return Response(body, content_type="application/octet-stream")


# ── /echo: echo all request headers ─────────────────────────────────────

@app.route(path='/echo', methods=['GET', 'POST'])
async def echo_headers(conn, receive, send):   # full form: `conn` is a native Connection
    """Echo all request headers as ``name: value\\n`` per line."""
    lines: list[str] = []
    for name, value in conn.headers:
        lines.append(f"{name.decode('latin-1')}: {value.decode('latin-1')}")
    body = "\n".join(lines).encode()
    await send({
        'type': 'http.response.start',
        'status': 200,
        'headers': [(b'content-type', b'text/plain; charset=utf-8')],
    })
    await send({
        'type': 'http.response.body',
        'body': body,
    })


# ── /cookie: echo parsed cookies ────────────────────────────────────────

@app.route(path='/cookie', methods=['GET', 'POST'])
async def cookie_echo(conn, receive, send):   # full form: `conn` is a native Connection
    """Parse cookies and echo as ``name=value\\n`` per line."""
    cookies = conn.cookies
    lines: list[str] = []
    for name, value in cookies.items():
        lines.append(f"{name}={value}")
    body = "\n".join(lines).encode()
    await send({
        'type': 'http.response.start',
        'status': 200,
        'headers': [(b'content-type', b'text/plain; charset=utf-8')],
    })
    await send({
        'type': 'http.response.body',
        'body': body,
    })


if __name__ == '__main__':
    app.run(port=8080)
