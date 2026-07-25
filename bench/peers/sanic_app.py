"""Sanic benchmark app — wire-compatible with bench/peers/asgi_app.py.

Same endpoints, same response bodies, same content-types.
Runs on Sanic's built-in server (not ASGI).

Usage:
    python3 bench/peers/sanic_app.py [--port PORT] [--cert CERT] [--key KEY]
"""
import argparse
import os

from sanic import Sanic
from sanic.response import text, json, raw

app = Sanic("bench")

# -- pre-encoded bodies (same as asgi_app.py) -------------------------------
_PLAINTEXT = b"Hello, World!"
_JSON_BODY = b'{"message":"Hello, World!"}'
_PONG = b"pong"
_1KB = os.urandom(1024)
_16KB = os.urandom(16000)
_64KB = os.urandom(65536)
_1MB = os.urandom(1024 * 1024)


@app.get("/ping")
async def ping(_request):
    return raw(_PONG, content_type="text/html; charset=utf-8")


@app.get("/plaintext")
async def plaintext(_request):
    return raw(_PLAINTEXT, content_type="text/plain; charset=utf-8")


@app.get("/json")
async def json_endpoint(_request):
    return raw(_JSON_BODY, content_type="application/json")


@app.get("/1kb")
async def _1kb_endpoint(_request):
    return raw(_1KB, content_type="text/html; charset=utf-8")


@app.get("/16kb")
async def _16kb_endpoint(_request):
    return raw(_16KB, content_type="text/html; charset=utf-8")


@app.get("/64kb")
async def _64kb_endpoint(_request):
    return raw(_64KB, content_type="text/html; charset=utf-8")


@app.get("/1mb")
async def _1mb_endpoint(_request):
    return raw(_1MB, content_type="text/html; charset=utf-8")


@app.post("/echo")
async def echo(request):
    return raw(request.body or b"", content_type="application/octet-stream")


@app.websocket("/ws")
async def ws_echo(_request, ws):
    while True:
        data = await ws.recv()
        await ws.send(data)


# -- Entry point ------------------------------------------------------------
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Sanic benchmark app")
    parser.add_argument("--port", type=int, default=8443)
    parser.add_argument("--cert", default=None)
    parser.add_argument("--key", default=None)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()

    kwargs = dict(host=args.host, port=args.port,
                  access_log=False, motd=False, single_process=True)

    if args.cert and args.key:
        kwargs["ssl"] = {"cert": args.cert, "key": args.key}

    app.run(**kwargs)
