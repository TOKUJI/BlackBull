"""FastAPI benchmark app — wire-compatible with bench/peers/asgi_app.py.

Same endpoints, same response bodies, same content-types.

Run with:
    uvicorn bench.peers.fastapi_app:app --host 127.0.0.1 --port 8443 \\
        --ssl-certfile tests/cert.pem --ssl-keyfile tests/key.pem \\
        --loop auto --http auto --workers 1 --log-level warning --no-access-log
"""
import os

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import PlainTextResponse, Response

app = FastAPI()

# PEER_MW=httparena reproduces what HttpArena's own fastapi entry mounts
# (post-PR-#1054: gzip at minimum_size=1000, no BaseHTTPMiddleware) so the
# two frameworks can be compared carrying the same accessories.
if os.environ.get("PEER_MW") == "httparena":
    from fastapi.middleware.gzip import GZipMiddleware
    app.add_middleware(GZipMiddleware, minimum_size=1000, compresslevel=5)

# -- pre-encoded bodies (same as asgi_app.py) -------------------------------
_PLAINTEXT = b"Hello, World!"
_JSON = b'{"message":"Hello, World!"}'
_PONG = b"pong"
_1KB = os.urandom(1024)
_16KB = os.urandom(16000)
_64KB = os.urandom(65536)
_1MB = os.urandom(1024 * 1024)

_PLAIN_CT = "text/plain; charset=utf-8"
_HTML_CT = "text/html; charset=utf-8"
_JSON_CT = "application/json"
_OCTET_CT = "application/octet-stream"


@app.get("/ping")
async def ping():
    return Response(_PONG, media_type=_HTML_CT)


@app.get("/plaintext")
async def plaintext():
    return Response(_PLAINTEXT, media_type=_PLAIN_CT)


@app.get("/json")
async def json_endpoint():
    return Response(_JSON, media_type=_JSON_CT)


@app.get("/1kb")
async def _1kb_endpoint():
    return Response(_1KB, media_type=_HTML_CT)


@app.get("/16kb")
async def _16kb_endpoint():
    return Response(_16KB, media_type=_HTML_CT)


@app.get("/64kb")
async def _64kb_endpoint():
    return Response(_64KB, media_type=_HTML_CT)


@app.get("/1mb")
async def _1mb_endpoint():
    return Response(_1MB, media_type=_HTML_CT)


@app.post("/echo")
async def echo(request: Request):
    body = await request.body()
    return Response(body, media_type=_OCTET_CT)


# HttpArena's `baseline` profile endpoint, copied from that harness's own
# fastapi entry so a local A/B exercises the same handler as the leaderboard
# run.  /ping measures the framework floor; this measures the floor plus
# query parsing and a dynamically built body.
@app.api_route("/baseline11", methods=["GET", "POST"])
async def baseline11(request: Request):
    total = 0
    for val in request.query_params.values():
        try:
            total += int(val)
        except ValueError:
            pass
    if request.method == "POST":
        body = await request.body()
        if body:
            try:
                total += int(body.strip())
            except ValueError:
                pass
    return PlainTextResponse(str(total))


# Mounted LAST, as HttpArena's entry does: starlette matches routes in
# registration order, so a mount registered ahead of the benchmark routes
# would tax every request with a prefix check it does not need.
if os.environ.get("PEER_MW") == "httparena":
    from fastapi.staticfiles import StaticFiles
    app.mount("/static", StaticFiles(
        directory=os.path.dirname(os.path.abspath(__file__))), name="static")


@app.websocket("/ws")
async def ws_echo(websocket: WebSocket):
    await websocket.accept()
    try:
        while True:
            data = await websocket.receive_text()
            await websocket.send_text(data)
    except WebSocketDisconnect:
        pass


# Registered LAST on purpose — see the note on native_app.pingz.  Starlette
# matches routes in registration order with a regex per candidate, so the
# /ping vs /pingz delta is the cost of the scan.
@app.get("/pingz")
async def pingz():
    return Response(_PONG, media_type=_HTML_CT)
