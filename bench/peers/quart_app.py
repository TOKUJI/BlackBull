"""Quart benchmark app — minimal /ping route to match BlackBull's bench shape.

Run with:
    hypercorn --bind 0.0.0.0:8443 \\
        --certfile tests/cert.pem --keyfile tests/key.pem \\
        --workers 1 --worker-class asyncio \\
        bench.peers.quart_app:app
"""
from quart import Quart

app = Quart(__name__)


@app.route('/ping')
async def ping():
    return b'pong'
