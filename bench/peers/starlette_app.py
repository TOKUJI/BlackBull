"""Starlette benchmark app — minimal /ping route to match BlackBull's bench shape.

Run with:
    hypercorn --bind 0.0.0.0:8443 \\
        --certfile tests/cert.pem --keyfile tests/key.pem \\
        --workers 1 --worker-class asyncio \\
        bench.peers.starlette_app:app
"""
from starlette.applications import Starlette
from starlette.responses import Response
from starlette.routing import Route


async def ping(request):
    return Response(b'pong')


app = Starlette(routes=[Route('/ping', ping)])
