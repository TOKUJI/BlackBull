"""BlackBull benchmark target application.

Exposes minimal-overhead routes so load tests measure the framework's
request/response pipeline, not application logic.

Routes — internal characterization
----------------------------------
GET  /ping     4-byte pong — latency baseline
GET  /1kb      1 KiB response — small-response throughput
GET  /16kb     16 KiB response — medium response
GET  /64kb     64 KiB response — large-response / flow-control
GET  /1mb      1 MiB response — exercises flow-control window
POST /echo     echo back the request body — input processing
GET  /ws       WebSocket echo — RTT benchmark
GET  /metrics  JSON event loop lag stats from the in-process monitor

Routes — public-comparable (TechEmpower / granian)
--------------------------------------------------
GET  /plaintext  "Hello, World!" text/plain — TechEmpower Plaintext
GET  /json       {"message":"Hello, World!"} application/json — TechEmpower JSON

Start (HTTPS + HTTP/2 via ALPN)::

    python bench/app.py [--port 8443] [--cert cert.pem] [--key key.pem]

The cert/key default to the mkcert certificate in the repo root.
"""
import argparse
import asyncio
import json
import os
import sys

# Allow running as `python bench/app.py` from the repo root
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from http import HTTPMethod

from blackbull import BlackBull, Response, read_body
from blackbull.utils import Scheme
from bench.lag_monitor import LoopLagMonitor

# ---------------------------------------------------------------------------
# Application setup
# ---------------------------------------------------------------------------

app = BlackBull()
monitor = LoopLagMonitor(interval=0.05, window=400)

_PLAINTEXT = b'Hello, World!'
_JSON      = b'{"message":"Hello, World!"}'
_1KB  = os.urandom(1024)
_16KB = os.urandom(16000)
_64KB = os.urandom(65536)
_1MB  = os.urandom(1024 * 1024)


@app.on_startup
async def _start_monitor():
    monitor.start()


@app.on_shutdown
async def _stop_monitor():
    monitor.stop()


# ---------------------------------------------------------------------------
# HTTP routes
# ---------------------------------------------------------------------------

@app.route(path='/ping', methods=[HTTPMethod.GET])
async def ping():
    return b'pong'


@app.route(path='/plaintext', methods=[HTTPMethod.GET])
async def plaintext():
    return Response(_PLAINTEXT, content_type='text/plain; charset=utf-8')


@app.route(path='/json', methods=[HTTPMethod.GET])
async def json_hello():
    return Response(_JSON, content_type='application/json')


@app.route(path='/1kb', methods=[HTTPMethod.GET])
async def small():
    return _1KB


@app.route(path='/16kb', methods=[HTTPMethod.GET])
async def large():
    return _16KB


@app.route(path='/64kb', methods=[HTTPMethod.GET])
async def extra_large():
    return _64KB


@app.route(path='/1mb', methods=[HTTPMethod.GET])
async def one_mb():
    return _1MB


@app.route(path='/echo', methods=[HTTPMethod.POST])
async def echo(scope, receive, send):
    body = await read_body(receive)
    await send({'type': 'http.response.start', 'status': 200,
                'headers': [(b'content-type', b'application/octet-stream')]})
    await send({'type': 'http.response.body', 'body': body})


@app.route(path='/metrics', methods=[HTTPMethod.GET])
async def metrics():
    return json.dumps(monitor.snapshot()).encode()


@app.route(path='/config', methods=[HTTPMethod.GET])
async def config():
    from blackbull.env import get_settings
    cfg = get_settings()
    import multiprocessing
    workers = cfg.workers if cfg.workers != 0 else multiprocessing.cpu_count()
    return json.dumps({
        'workers': workers,
        'uvloop': cfg.use_uvloop,
        'h2_active_streams_1w': cfg.h2_active_streams_1w,
        'h2_active_streams': cfg.h2_active_streams,
        'h2_initial_window_size': cfg.h2_initial_window_size,
        'h2_connection_window_size': cfg.h2_connection_window_size,
    }).encode()


# ---------------------------------------------------------------------------
# WebSocket echo
# ---------------------------------------------------------------------------

@app.route(path='/ws', methods=[HTTPMethod.GET], scheme=Scheme.websocket)
async def ws_echo(scope, receive, send):
    event = await receive()
    if event.get('type') != 'websocket.connect':
        return
    await send({'type': 'websocket.accept'})

    while True:
        event = await receive()
        t = event.get('type', '')
        if t == 'websocket.disconnect':
            break
        if t == 'websocket.receive':
            payload = event.get('text') or event.get('bytes')
            if isinstance(payload, str):
                await send({'type': 'websocket.send', 'text': payload})
            else:
                await send({'type': 'websocket.send', 'bytes': payload or b''})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _parse_args():
    import os
    p = argparse.ArgumentParser(description='BlackBull benchmark server')
    p.add_argument('--port', type=int, default=8443)
    p.add_argument('--cert', default='cert.pem')
    p.add_argument('--key',  default='key.pem')
    p.add_argument('--workers', type=int, default=None,
                   help='Worker processes (default BB_WORKERS env or 1; 0 = cpu_count)')
    p.add_argument('--stream-queue-depth', type=int, default=None,
                   help='HTTP/2 per-stream queue depth (default BB_STREAM_QUEUE_DEPTH or 64)')
    p.add_argument('--ws-queue-depth', type=int, default=None,
                   help='WebSocket event queue depth (default BB_WS_QUEUE_DEPTH or 256)')
    return p.parse_args()


if __name__ == '__main__':
    args = _parse_args()
    print(f'Starting bench server on https://localhost:{args.port}')
    print(f'  cert={args.cert}  key={args.key}')
    print('Routes: /ping  /1kb  /16kb  /echo  /ws  /metrics')
    app.serve(
        port=args.port,
        certfile=args.cert,
        keyfile=args.key,
        workers=args.workers,
        stream_queue_depth=args.stream_queue_depth,
        ws_queue_depth=args.ws_queue_depth,
    )
