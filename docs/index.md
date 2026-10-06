# BlackBull

BlackBull is a Python web framework with pure-Python implementations of
HTTP/1.1, HTTP/2 (with ALPN), WebSocket, gRPC, and an MQTT 5 broker — all
on one process, no reverse proxy or sidecar required.  No required C
extensions outside the standard library, one `pip install`, one
deployable.

Internally it threads its own typed `Connection` end to end; the
[**ASGI 3.0**](https://asgi.readthedocs.io/en/latest/specs/main.html)
interface[^asgi] is kept as an interop boundary, so a BlackBull app also
runs unchanged under uvicorn, Hypercorn, or `httpx.ASGITransport`.

[^asgi]: ASGI is the async successor to WSGI — a single small interface
    that lets the same app object speak HTTP and WebSocket without
    separate adapters.  BlackBull apps are ASGI-callable, so any ASGI
    host can serve them.  The reverse direction — hosting a *foreign*
    ASGI app (Starlette, Quart, FastAPI) on BlackBull's own server —
    needs `BB_FORCE_ASGI_SCOPE=1`, which makes the server emit a plain
    ASGI `scope` dict instead of a `Connection`.

!!! warning "Early Alpha"
    BlackBull is in **Early Alpha**.  The API may change between MINOR
    versions per [ZeroVer](https://0ver.org/).  See
    [Known Limitations](https://github.com/TOKUJI/BlackBull/blob/master/KNOWN_LIMITATIONS.md)
    for the explicit list of behaviours to expect, and
    [Conformance](about/conformance.md) for the protocol-level
    test coverage behind the standards-compliance claims.

## Serving and interoperability

- HTTP/1.1, prior-knowledge h2c and WebSocket upgrades share the HTTP listener.
  HTTP/2 over TLS uses ALPN; RFC 8441 WebSocket streams are opt-in.
- gRPC uses HTTP/2 on that listener. MQTT uses an extension and its own
  listener, with one worker owning broker state.
- Native handlers and middleware use `Connection`. External-ASGI hosting
  converts at the boundary; use `BB_FORCE_ASGI_SCOPE=1` to serve a foreign
  ASGI app on BlackBull's server.
- See [Conformance](about/conformance.md) for coverage and
  [Security model](about/security-model.md) for default resource bounds.

## Install

```bash
pip install blackbull                        # core
pip install 'blackbull[compression]'         # gzip / brotli / zstandard
pip install 'blackbull[reload]'              # watchfiles for --reload
pip install 'blackbull[speed]'               # uvloop
```

## Hello world

```python
from blackbull import BlackBull

app = BlackBull()

@app.route(path='/')
async def hello():
    return "Hello, world!"

if __name__ == '__main__':
    app.run(port=8000)
```

```bash
$ python myapp.py
$ curl localhost:8000/
Hello, world!
```

That's a *simplified handler* — no `scope`, `receive`, `send`
boilerplate, return value becomes the response body.  See
[Your First App](getting-started/first-app.md) for the next steps,
or [Hello World](getting-started/hello-world.md) for the full
ASGI-triplet form.

## Where to go next

- Not sure BlackBull fits your project? [Why BlackBull?](getting-started/why-blackbull.md)
  walks through the scenarios where its architectural bets pay off — and where
  another framework may serve you better.
- New to BlackBull? Start with [Installation](getting-started/installation.md).
- Building something? The [Guide](guide/index.md) covers routing,
  middleware, WebSockets, error handling, HTTP/2, and configuration.
- Serving a local model from a small box?
  [Edge inference serving](guide/edge-inference.md) walks the
  one-process shape end to end, with a runnable example.
- Deploying? See [Deployment](deployment/running.md) for multi-worker,
  TLS, AF_UNIX, systemd activation, and reverse-proxy topologies.
- Curious about the design? [Architecture](about/architecture.md) covers
  the actor model, protocol ownership, and fault injection;
  [Internals](about/internals.md) states implementation invariants and code ownership.
