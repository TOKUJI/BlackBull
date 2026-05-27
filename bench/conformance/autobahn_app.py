"""Plaintext WebSocket echo server for Autobahn|Testsuite conformance runs.

Autobahn|Testsuite (``wstest --mode=fuzzingserver``) drives a peer over
``ws://`` to exercise RFC 6455 framing, control-frame handling, UTF-8
validation, close codes, fragmentation, and (optionally) permessage-deflate.

The expected behaviour for an echo server is:

* accept the upgrade
* for every text message received, send the same text back
* for every binary message received, send the same bytes back
* preserve message fragmentation only insofar as the suite expects (it
  treats a fragmented message as one logical message); we echo a
  single coalesced message back, which is what BlackBull's ASGI
  ``websocket.receive`` event already delivers.

Run from the repo root::

    python bench/conformance/autobahn_app.py --port 9001

then point ``wstest`` at ``ws://host.docker.internal:9001``.
"""
import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from http import HTTPMethod

from blackbull import BlackBull
from blackbull.utils import Scheme

app = BlackBull()


@app.route(path='/', methods=[HTTPMethod.GET], scheme=Scheme.websocket)
async def echo(scope, receive, send):
    event = await receive()
    if event.get('type') != 'websocket.connect':
        return
    await send({'type': 'websocket.accept'})
    while True:
        event = await receive()
        t = event.get('type')
        if t == 'websocket.disconnect':
            return
        if t != 'websocket.receive':
            continue
        if (text := event.get('text')) is not None:
            await send({'type': 'websocket.send', 'text': text})
        else:
            await send({'type': 'websocket.send', 'bytes': event.get('bytes') or b''})


def _parse_args():
    p = argparse.ArgumentParser(description='BlackBull WS echo for Autobahn')
    p.add_argument('--port', type=int, default=9001)
    return p.parse_args()


if __name__ == '__main__':
    args = _parse_args()
    print(f'WS echo server on ws://localhost:{args.port}/')
    app.run(port=args.port)
