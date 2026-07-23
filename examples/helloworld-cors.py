"""
helloworld-cors.py
==================
Demonstrates the built-in ``CORS`` middleware.

The app exposes two JSON API endpoints and serves a browser test page at ``/``.

How the demo works
------------------
BlackBull listens on HTTP (port 8000).  Nginx sits in front on HTTPS (port 443).
The browser loads the test page from ``https://localhost/`` (via nginx), then
the page's JavaScript fetches ``http://localhost:8000/api/hello`` directly.
Because the page origin (``https://localhost``) differs from the API origin
(``http://localhost:8000``), the browser treats it as a cross-origin request and
sends a preflight ``OPTIONS`` before the actual fetch.  The ``CORS`` middleware
handles the preflight and adds ``Access-Control-*`` headers so the browser permits
the response.

Run (direct HTTP — same-origin only; CORS not triggered by the browser):
    python helloworld-cors.py

Run (with nginx fronting on HTTPS — enables the cross-origin demo):
    python helloworld-cors.py --cors-origin https://localhost

Test with curl (no browser needed):

    # Preflight
    curl -s -X OPTIONS http://localhost:8000/api/hello \\
      -H 'Origin: https://localhost' \\
      -H 'Access-Control-Request-Method: GET' -v 2>&1 | grep '< '

    # Actual cross-origin GET
    curl -s http://localhost:8000/api/hello \\
      -H 'Origin: https://localhost'

    # Cross-origin POST
    curl -s -X POST http://localhost:8000/api/echo \\
      -H 'Origin: https://localhost' \\
      -H 'Content-Type: application/json' \\
      -d '{"message": "hello"}'

Nginx config (for reference)::

    server {
        listen 443 ssl http2;
        server_name localhost;

        ssl_certificate     /path/to/cert.pem;
        ssl_certificate_key /path/to/key.pem;

        location / {
            proxy_pass http://127.0.0.1:8000;
            proxy_http_version 1.1;
            proxy_set_header Host $host;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto $scheme;
            proxy_set_header Upgrade $http_upgrade;
            proxy_set_header Connection "upgrade";
        }
    }
"""

import argparse
import json
from http import HTTPMethod

from blackbull import CORS, BlackBull, JSONResponse, TrustedProxy
from blackbull.request import read_body

# ---------------------------------------------------------------------------
# HTML test page (embedded so the example stays self-contained)
# ---------------------------------------------------------------------------

_HTML = """\
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>BlackBull CORS demo</title>
<style>
  body { font-family: monospace; max-width: 720px; margin: 2rem auto; }
  button { margin: .25rem; padding: .4rem .8rem; cursor: pointer; }
  pre { background: #f4f4f4; padding: .75rem; white-space: pre-wrap; word-break: break-all; }
  .ok  { color: green; }
  .err { color: red; }
</style>
</head>
<body>
<h2>BlackBull CORS demo</h2>

<p>This page is loaded from <strong id="page-origin"></strong>.<br>
The buttons below fetch from <strong id="api-origin"></strong>.<br>
If those origins differ, the browser sends a CORS preflight first.</p>

<button onclick="doGet()">GET /api/hello</button>
<button onclick="doPost()">POST /api/echo</button>
<button onclick="doPreflight()">OPTIONS /api/hello (manual preflight)</button>

<h3>Result</h3>
<pre id="out">—</pre>

<script>
const API = 'http://localhost:8000';   // direct to BlackBull (bypasses nginx)

document.getElementById('page-origin').textContent = location.origin;
document.getElementById('api-origin').textContent   = API;

function show(label, status, headers, body) {
  const cls = status < 400 ? 'ok' : 'err';
  const hdrs = [...headers.entries()].map(([k,v]) => `  ${k}: ${v}`).join('\\n');
  document.getElementById('out').innerHTML =
    `<span class="${cls}">${label}  →  HTTP ${status}</span>\\n\\nHeaders:\\n${hdrs}\\n\\nBody:\\n${body}`;
}

async function doGet() {
  const r = await fetch(`${API}/api/hello`, {
    headers: { 'Accept': 'application/json' },
  });
  show('GET /api/hello', r.status, r.headers, await r.text());
}

async function doPost() {
  const r = await fetch(`${API}/api/echo`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ message: 'hello from the browser' }),
  });
  show('POST /api/echo', r.status, r.headers, await r.text());
}

async function doPreflight() {
  // fetch() fires OPTIONS automatically for non-simple requests;
  // this button forces a non-simple request so the preflight is visible
  // in DevTools → Network tab.
  const r = await fetch(`${API}/api/hello`, {
    headers: {
      'Accept': 'application/json',
      'X-Custom-Header': 'test',   // triggers preflight
    },
  });
  show('OPTIONS + GET /api/hello', r.status, r.headers, await r.text());
}
</script>
</body>
</html>
""".encode()   # UTF-8: the page contains non-ASCII glyphs (— and →)

# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------

app = BlackBull()


@app.route(path='/')
async def index(conn, receive, send):
    await send({
        'type': 'http.response.start',
        'status': 200,
        'headers': [(b'content-type', b'text/html; charset=utf-8')],
    })
    await send({'type': 'http.response.body', 'body': _HTML})


@app.route(path='/api/hello')
async def hello():
    return {'greeting': 'Hello, world!', 'cors': 'enabled'}


@app.route(path='/api/echo', methods=[HTTPMethod.POST])
async def echo(conn, receive, send):
    body = await read_body(receive)
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        await send(JSONResponse({'error': 'Invalid JSON'}, status=400))
        return
    await send(JSONResponse({'echo': data}))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='BlackBull CORS demo')
    parser.add_argument('--port', type=int, default=8000)
    parser.add_argument(
        '--cors-origin',
        metavar='ORIGIN',
        action='append',
        dest='cors_origins',
        default=[],
        help='Allowed CORS origin (repeat for multiple). '
             'Defaults to wildcard (*) when omitted.',
    )
    args = parser.parse_args()

    origins = args.cors_origins or ['*']

    # When running behind nginx, trust the loopback proxy so that
    # conn.scheme reflects the client's HTTPS (not plain HTTP).
    app.use(TrustedProxy(['127.0.0.1', '::1']))

    app.use(CORS(
        allow_origins=origins,
        allow_methods=['GET', 'POST', 'OPTIONS'],
        allow_headers=['Content-Type', 'Accept', 'X-Custom-Header'],
        max_age=3600,
    ))

    print(f'Listening on http://localhost:{args.port}')
    print(f'CORS allow_origins = {origins}')
    print()
    print('Test with curl:')
    print(f'  curl -s http://localhost:{args.port}/api/hello '
          f"-H 'Origin: https://localhost'")
    print()
    print('Test preflight:')
    print(f'  curl -s -X OPTIONS http://localhost:{args.port}/api/hello \\')
    print(f"    -H 'Origin: https://localhost' \\")
    print(f"    -H 'Access-Control-Request-Method: GET' -v 2>&1 | grep '< '")

    app.run(port=args.port)
