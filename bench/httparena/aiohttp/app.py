"""aiohttp entrypoint for HttpArena — BlackBull-equivalent baseline app.

HttpArena's baseline profile (conns 512/4096) drives three request shapes
against /baseline11 (see scripts/lib/tools/gcannon.sh --raw rotation):

  GET  /baseline11?a=13&b=42            → "55"   (query-param sum)
  POST /baseline11?a=13&b=42  body=20   → "75"   (query + body sum)
  POST /baseline11?a=13&b=42  chunked   → "75"   (same, TE:chunked)

validate.sh additionally checks Content-Type: text/plain, randomized
anti-cheat inputs, TCP fragmentation (partial recv() buffers), lower-cased
header field names, and Connection: close — all handled natively by
aiohttp's HTTP parser.  /baseline2 is the TLS/h2 readiness fallback target
kept for parity with the sanic app.

Run with gunicorn + aiohttp.GunicornWebWorker for N worker processes
(launcher.py passes WEB_WORKERS; default = nproc inside the container).
"""
import os

from aiohttp import web

_PLAIN_CT = "text/plain"


def _baseline_total(request: web.Request, body: bytes) -> int:
    """Sum all query-param values plus the (optional) POST body."""
    total = 0
    for value in request.query.values():
        try:
            total += int(value)
        except (ValueError, TypeError):
            pass
    if body:
        try:
            total += int(body.strip())
        except (ValueError, TypeError):
            pass
    return total


async def _baseline(request: web.Request) -> web.Response:
    body = await request.read() if request.method == "POST" else b""
    total = _baseline_total(request, body)
    return web.Response(text=str(total), content_type=_PLAIN_CT)


async def _pipeline(_: web.Request) -> web.Response:
    return web.Response(text="ok", content_type=_PLAIN_CT)


async def _healthz(_: web.Request) -> web.Response:
    return web.Response(text="ok", content_type=_PLAIN_CT)


async def create_app() -> web.Application:
    """App factory for gunicorn's aiohttp worker.

    aiohttp's GunicornWebWorker only accepts an Application instance or an
    *async* factory (`inspect.iscoroutinefunction`); a plain sync factory is
    rejected with RuntimeError.  Hence this is `async def`, not `def`.
    """
    app = web.Application()
    app.router.add_route("*", "/baseline11", _baseline)
    app.router.add_route("*", "/baseline2", _baseline)
    app.router.add_get("/pipeline", _pipeline)
    app.router.add_get("/healthz", _healthz)
    return app


if __name__ == "__main__":
    # Direct-run mode for local smoke testing (single process).  The launcher
    # runs N of these with --reuse-port (one per worker) so each process owns
    # its own SO_REUSEPORT listening socket — see launcher.py.
    import asyncio
    import sys

    port = int(os.environ.get("PORT", "8080"))
    kwargs = {}
    if "--reuse-port" in sys.argv:
        kwargs["reuse_port"] = True
    web.run_app(asyncio.run(create_app()), host="0.0.0.0", port=port, **kwargs)
