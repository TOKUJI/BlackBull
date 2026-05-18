"""FastAPI benchmark app — minimal /ping route to match BlackBull's bench shape.

Run with:
    hypercorn --bind 0.0.0.0:8443 \\
        --certfile tests/cert.pem --keyfile tests/key.pem \\
        --workers 1 --worker-class asyncio \\
        bench.peers.fastapi_app:app
"""
from fastapi import FastAPI
from fastapi.responses import Response

app = FastAPI()


@app.get('/ping')
async def ping():
    return Response(b'pong')
