"""Native BlackBull benchmark app — wire-compatible with bench/peers/asgi_app.py.

Uses BlackBull's native Connection API (no ASGI scope dict, no compat shim).
Same endpoints, same response bodies, same content-types as asgi_app.py.

Loaded by BlackBull's own server:
    blackbull bench.peers.native_app:app --bind 127.0.0.1:8443 ...
"""
import os
from http import HTTPMethod

from blackbull import BlackBull, Response

app = BlackBull()

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


@app.route(path="/ping", methods=[HTTPMethod.GET])
async def ping():
    return Response(_PONG, content_type=_HTML_CT)


@app.route(path="/plaintext", methods=[HTTPMethod.GET])
async def plaintext():
    return Response(_PLAINTEXT, content_type=_PLAIN_CT)


@app.route(path="/json", methods=[HTTPMethod.GET])
async def json_endpoint():
    return Response(_JSON, content_type=_JSON_CT)


@app.route(path="/1kb", methods=[HTTPMethod.GET])
async def _1kb_endpoint():
    return Response(_1KB, content_type=_HTML_CT)


@app.route(path="/16kb", methods=[HTTPMethod.GET])
async def _16kb_endpoint():
    return Response(_16KB, content_type=_HTML_CT)


@app.route(path="/64kb", methods=[HTTPMethod.GET])
async def _64kb_endpoint():
    return Response(_64KB, content_type=_HTML_CT)


@app.route(path="/1mb", methods=[HTTPMethod.GET])
async def _1mb_endpoint():
    return Response(_1MB, content_type=_HTML_CT)


@app.route(path="/echo", methods=[HTTPMethod.POST])
async def echo(body: bytes):
    return Response(body, content_type=_OCTET_CT)


@app.route(path="/ws", methods=[HTTPMethod.GET])
async def ws_echo(conn, receive, send):
    """WebSocket echo — full-form handler (conn, receive, send)."""
    event = await receive()
    if event.get("type") != "websocket.connect":
        return
    await send({"type": "websocket.accept"})
    while True:
        event = await receive()
        t = event.get("type", "")
        if t == "websocket.disconnect":
            break
        if t != "websocket.receive":
            continue
        text = event.get("text")
        if text is not None:
            await send({"type": "websocket.send", "text": text})
        else:
            await send({"type": "websocket.send",
                        "bytes": event.get("bytes") or b""})
