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

# GC observation sampler (Sprint 100 Phase 1 mean-vs-tail fork).  Env-gated;
# observation-only — no request-path boundary is added.  Off by default.
# Sanic runs as `python3 bench/peers/sanic_app.py`, so sys.path[0] is this
# file's directory, not the repo root — add the root so `bench.peers` is
# importable (the blackbull CLI does this itself; a plain script does not).
if os.environ.get("BB_GC_STATS_OUT"):
    import sys
    from pathlib import Path

    _repo_root = str(Path(__file__).resolve().parents[2])
    if _repo_root not in sys.path:
        sys.path.insert(0, _repo_root)
    from bench.peers import gc_stats  # noqa: F401  (import activates)

# Loop-identity stamp (Sprint 100 Phase 2).  Env-gated; off by default.
if os.environ.get("BB_LOOP_STAMP_OUT"):
    import sys
    from pathlib import Path

    _repo_root = str(Path(__file__).resolve().parents[2])
    if _repo_root not in sys.path:
        sys.path.insert(0, _repo_root)
    from bench.peers import loop_stamp  # noqa: F401  (import activates)

# Response-transmit timing (Sprint 100 Phase 2 F1 fork).  Env-gated; ONE
# boundary (the response send seam).  The app declares its server so the
# instrument wraps the right seam.  The F3 fork adds the parse seam
# (bytes-delivered → parsed-request-ready) via BB_PARSE_TIMING_OUT; the F4
# fork adds the app-dispatch seam (Sanic.handle_request) via
# BB_DISPATCH_TIMING_OUT; the F5 fork adds the read-path seam
# (data_received) via BB_READ_TIMING_OUT.
if (os.environ.get("BB_RESP_TIMING_OUT") or os.environ.get("BB_TIMING_SNAP")
        or os.environ.get("BB_PARSE_TIMING_OUT")
        or os.environ.get("BB_DISPATCH_TIMING_OUT")
        or os.environ.get("BB_READ_TIMING_OUT")):
    import sys
    from pathlib import Path

    _repo_root = str(Path(__file__).resolve().parents[2])
    if _repo_root not in sys.path:
        sys.path.insert(0, _repo_root)
    os.environ["BB_RESP_TIMING_SERVER"] = "sanic"
    os.environ["BB_PARSE_TIMING_SERVER"] = "sanic"
    os.environ["BB_DISPATCH_TIMING_SERVER"] = "sanic"
    os.environ["BB_READ_TIMING_SERVER"] = "sanic"
    from bench.peers import response_timing  # noqa: F401  (import activates)

# Armed-state gate stamp (Sprint 100 Phase 2 F3+ review fix).  Env-gated;
# writes the resp/handler/parse armed flags on EVERY launch (bare included)
# so every calibration arm can prove itself.
if os.environ.get("BB_GATE_STAMP_OUT"):
    import sys
    from pathlib import Path

    _repo_root = str(Path(__file__).resolve().parents[2])
    if _repo_root not in sys.path:
        sys.path.insert(0, _repo_root)
    from bench.peers import gate_stamp  # noqa: F401  (import writes the stamp)

app = Sanic("bench")

# F3 parse seam (Sprint 100 Phase 2 F3 fork).  Env-gated; registers a
# before_server_start listener that patches Http.http1_request_header AFTER
# TouchUp.run (sanic re-execs that method from source at startup — an
# import-time class patch would be re-exec'd and break startup).  The same
# listener also arms the F4 app-dispatch seam (Sanic.handle_request, also in
# Sanic.__touchup__) when BB_DISPATCH_TIMING_OUT is set, and the F5
# read-path seam (HttpProtocol.data_received) when BB_READ_TIMING_OUT is set.
if (os.environ.get("BB_PARSE_TIMING_OUT") or os.environ.get("BB_DISPATCH_TIMING_OUT")
        or os.environ.get("BB_READ_TIMING_OUT")):
    import sys
    from pathlib import Path

    _repo_root = str(Path(__file__).resolve().parents[2])
    if _repo_root not in sys.path:
        sys.path.insert(0, _repo_root)
    from bench.peers import response_timing

    response_timing.register_parse_timing(app)

# Handler-region timing (Sprint 100 Phase 2 F2 fork).  Env-gated; registers
# http.handler.before/after signal brackets on the app.  Per-request pairing
# by id(request) — valid at B1 (one in-flight request per connection).
if os.environ.get("BB_HANDLER_TIMING"):
    import sys
    from pathlib import Path

    _repo_root = str(Path(__file__).resolve().parents[2])
    if _repo_root not in sys.path:
        sys.path.insert(0, _repo_root)
    from bench.peers import response_timing

    response_timing.register_handler_timing(app)

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
