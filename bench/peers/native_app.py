"""Native BlackBull benchmark app — wire-compatible with bench/peers/asgi_app.py.

Uses BlackBull's native Connection API (no ASGI scope dict, no compat shim).
Same endpoints, same response bodies, same content-types as asgi_app.py.

Loaded by BlackBull's own server:
    blackbull bench.peers.native_app:app --bind 127.0.0.1:8443 ...
"""
import os
from http import HTTPMethod
from urllib.parse import parse_qs

from blackbull import BlackBull, Connection, Response
from blackbull.utils import Scheme
from blackbull.websocket import WebSocket

# GC observation sampler (Sprint 100 Phase 1 mean-vs-tail fork).  Env-gated:
# with BB_GC_STATS_OUT set, a background thread appends gc.get_stats()
# snapshots to that file.  Observation-only — no request-path boundary is
# added, so whole-server measurements stay whole-server.  Off by default.
if os.environ.get("BB_GC_STATS_OUT"):
    from bench.peers import gc_stats  # noqa: F401  (import activates)

# Loop-identity stamp (Sprint 100 Phase 2).  Env-gated; writes the effective
# loop class to BB_LOOP_STAMP_OUT at import.  Off by default.
if os.environ.get("BB_LOOP_STAMP_OUT"):
    from bench.peers import loop_stamp  # noqa: F401  (import activates)

# Response-transmit timing (Sprint 100 Phase 2 F1 fork).  Env-gated; ONE
# boundary (the response send seam).  The app declares its server so the
# instrument wraps the right seam (blackbull is importable even under sanic).
# The F2 fork also enables the handler-region bracket (registered below once
# the app object exists) and the SIGUSR1 per-scenario snapshot; the F3 fork
# adds the parse seam (bytes-delivered → parsed-request-ready) via
# BB_PARSE_TIMING_OUT; the F4 fork adds the app-dispatch seam
# (BlackBull.__call__) via BB_DISPATCH_TIMING_OUT; the F5 fork adds the
# read-path seam (get_buffer + buffer_updated) via BB_READ_TIMING_OUT.
if (os.environ.get("BB_RESP_TIMING_OUT") or os.environ.get("BB_TIMING_SNAP")
        or os.environ.get("BB_PARSE_TIMING_OUT")
        or os.environ.get("BB_DISPATCH_TIMING_OUT")
        or os.environ.get("BB_READ_TIMING_OUT")):
    os.environ["BB_RESP_TIMING_SERVER"] = "blackbull"
    os.environ["BB_PARSE_TIMING_SERVER"] = "blackbull"
    os.environ["BB_DISPATCH_TIMING_SERVER"] = "blackbull"
    os.environ["BB_READ_TIMING_SERVER"] = "blackbull"
    from bench.peers import response_timing  # noqa: F401  (import activates)

# Armed-state gate stamp (Sprint 100 Phase 2 F3+ review fix).  Env-gated;
# writes the resp/handler/parse armed flags on EVERY launch (bare included,
# where response_timing is never imported) so every calibration arm can prove
# itself.
if os.environ.get("BB_GATE_STAMP_OUT"):
    from bench.peers import gate_stamp  # noqa: F401  (import writes the stamp)

app = BlackBull()

# Lifecycle-event knock-out (Sprint 100 Phase C2 measurement).  Env-gated;
# strips the four request-lifecycle event guards (request_received /
# before_handler / after_handler / request_completed) for the events bare
# A/B.  Installed at import (before serving); the no-events variant must
# serve byte-identical responses.
if os.environ.get("BB_NO_EVENTS"):
    from bench.peers import no_events

    no_events._install_no_events(app)

# Handler-region timing (Sprint 100 Phase 2 F2 fork).  Env-gated; registers
# before_handler/after_handler brackets on the app.  Per-request pairing by
# id(conn) — valid at B1 (one in-flight request per connection).
if os.environ.get("BB_HANDLER_TIMING"):
    from bench.peers import response_timing

    response_timing.register_handler_timing(app)

# PEER_MW=httparena reproduces the middleware stack bench/httparena/app.py
# registers, so a local A/B can ask whether the leaderboard gap lives in the
# framework or in what the leaderboard entry mounts on top of it.  Off by
# default: the bare app is the framework floor.
if os.environ.get("PEER_MW") == "httparena":
    from blackbull.middleware.compression import Compression
    app.use(Compression())
    app.static("/static", os.path.dirname(os.path.abspath(__file__)))

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


@app.route(path="/conn", methods=[HTTPMethod.GET])
async def conn_route(conn: Connection):
    # A/B-measurable simplified-handler profile: a ``conn`` parameter makes
    # the plain pin's per-request dispatch loop execute (the no-arg
    # /plaintext iterates an empty loop), so ab_commit.sh URL_PATH=/conn
    # measures the wrapper dispatch itself.  The "Hello" body keeps the
    # harness's server-ready check passing.
    return Response(_PLAINTEXT, content_type=_PLAIN_CT)


@app.route(path="/json", methods=[HTTPMethod.GET])
async def json_endpoint():
    return Response(_JSON, content_type=_JSON_CT)


@app.route(path="/1kb", methods=[HTTPMethod.GET])
async def _1kb_endpoint():
    return Response(_1KB, content_type=_HTML_CT)


# Sprint 94 A/B lane: a pre-encoded response — the static-asset shape
# (StaticFiles precompressed siblings carry Content-Encoding before the
# Compression middleware sees them).  With PEER_MW=httparena the middleware
# must pass it through verbatim: no re-compression, no Vary stamp.  This is
# the lane the v0.67.0 → v0.70.0 static regression was measured on.
@app.route(path="/preencoded", methods=[HTTPMethod.GET])
async def preencoded():
    return Response(_16KB, content_type=_HTML_CT,
                    headers=[(b"content-encoding", b"br")])


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


@app.route(path="/upload", methods=[HTTPMethod.POST, HTTPMethod.GET])
async def upload_route(conn: Connection):
    # Mirrors the HttpArena upload profile (bench/httparena/app.py): stream the
    # body — never materialize — and return the byte count.  GET answers 200 so
    # ab_commit.sh's server-ready probe passes on the POST lane.
    if conn.method != "POST":
        return Response(b"ok", content_type=_HTML_CT)
    size = 0
    async for chunk in conn.stream():
        size += len(chunk)
    return Response(str(size).encode(), content_type=_PLAIN_CT)


# HttpArena's `baseline` profile endpoint, copied from bench/httparena/app.py
# so a local A/B exercises the same handler as the leaderboard run.  /ping
# measures the framework floor; this measures the floor plus query parsing
# and a dynamically built body, which is where the leaderboard gap shows up.
@app.route(path="/baseline11", methods=[HTTPMethod.GET, HTTPMethod.POST])
async def baseline11(conn: Connection):
    total = 0
    raw = conn.query_string or b""
    for vals in parse_qs(raw.decode("latin-1"), keep_blank_values=True).values():
        for v in vals:
            try:
                total += int(v)
            except ValueError:
                pass
    if conn.method == "POST":
        body = await conn.body()
        if body:
            try:
                total += int(body.strip())
            except ValueError:
                pass
    return Response(str(total).encode(), content_type=_PLAIN_CT)


@app.route(path="/ws", methods=[HTTPMethod.GET], scheme=Scheme.websocket)
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


@app.route(path="/ws-object", methods=[HTTPMethod.GET], scheme=Scheme.websocket)
async def ws_echo_object(ws: WebSocket):
    """The same echo, object form — the native send/receive channel.

    Registered alongside /ws so the two channels can be compared in one
    process, on one build, with the load generator the only thing that
    changes.  /ws is the raw form: it mints a `websocket.receive` dict per
    message and sends dicts back, which is the ASGI compat boundary.  This
    one takes `next_message()` / `NativeWSMessage` and pays neither.
    """
    await ws.accept()
    async for message in ws:
        await ws.send(message)


# Registered LAST on purpose.  Paired with /ping — the first route — this
# measures what the router costs as the table grows: BlackBull resolves a
# static path with one dict probe, so the pair should cost the same.
@app.route(path="/pingz", methods=[HTTPMethod.GET])
async def pingz():
    return Response(_PONG, content_type=_HTML_CT)
