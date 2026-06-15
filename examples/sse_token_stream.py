"""Server-Sent Events token-streaming example.

Demonstrates the Sprint 45 streaming surface end-to-end:

* A simplified handler returns an :class:`EventSourceResponse` whose
  async generator yields one event per fake LLM token.
* A second handler shows the bare ``async def stream(): yield ...``
  shape — no class needed; BlackBull wraps it in a default
  :class:`StreamingResponse` automatically.

Run with:

    python examples/sse_token_stream.py

Then in another shell:

    curl -N http://localhost:8000/sse              # see SSE frames
    curl -N http://localhost:8000/raw              # see raw chunked stream
    open  http://localhost:8000/                   # browser EventSource demo

The browser page subscribes to ``/sse`` via the standard
:js:class:`EventSource` API and renders each token as it arrives.
"""
from __future__ import annotations

import asyncio

from blackbull import BlackBull, EventSourceResponse


app = BlackBull()


_HTML = b"""<!doctype html>
<title>BlackBull SSE demo</title>
<body style="font-family: ui-monospace, monospace; padding: 2rem;">
<h1>BlackBull SSE demo</h1>
<p>Tokens stream in below as the server yields them.</p>
<pre id="out" style="background: #f4f4f4; padding: 1rem; min-height: 4rem;"></pre>
<script>
const out = document.getElementById('out');
const es  = new EventSource('/sse');
es.addEventListener('token', e => { out.textContent += e.data + ' '; });
es.addEventListener('done',  () => { es.close(); });
</script>
"""


@app.route(path='/')
async def index():
    """Browser-side EventSource demo page."""
    from blackbull import Response
    return Response(_HTML, content_type='text/html; charset=utf-8')


@app.route(path='/sse')
async def sse():
    """SSE token stream — one labelled event per token, terminated by ``done``."""
    async def tokens():
        for word in 'Hello from BlackBull streaming over HTTP/2 SSE'.split():
            yield {'event': 'token', 'data': word}
            await asyncio.sleep(0.3)
        yield {'event': 'done', 'data': ''}
    return EventSourceResponse(tokens())


@app.route(path='/raw')
async def raw():
    """Bare async-generator handler — no class, just yield bytes/str."""
    for i in range(10):
        yield f'chunk {i}\n'
        await asyncio.sleep(0.1)


if __name__ == '__main__':
    app.run(port=8000)
