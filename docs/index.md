# BlackBull

BlackBull is a Python web framework that speaks the
[**ASGI 3.0**](https://asgi.readthedocs.io/en/latest/specs/main.html)
interface[^asgi], with native implementations of HTTP/1.1, HTTP/2 (with
ALPN), and WebSocket.  Pure-Python protocol stack, no required C
extensions outside the standard library, one `pip install`, one
deployable.

[^asgi]: ASGI is the async successor to WSGI — a single small interface
    that lets the same app object speak HTTP and WebSocket without
    separate adapters.  Apps written against
    [Starlette](https://www.starlette.io/),
    [Quart](https://quart.palletsprojects.com/),
    [FastAPI](https://fastapi.tiangolo.com/),
    or any other ASGI 3.0 framework work on BlackBull.

!!! warning "Early Alpha"
    BlackBull is in **Early Alpha**.  The API may change between MINOR
    versions per [ZeroVer](https://0ver.org/).  See
    [Known Limitations](https://github.com/TOKUJI/BlackBull/blob/master/KNOWN_LIMITATIONS.md)
    for the explicit list of behaviours to expect, and
    [Conformance](about/conformance.md) for the protocol-level
    test coverage behind the standards-compliance claims.

## What you get

- **Routing + middleware** in the shape ASGI apps already use, with
  optional simplified handler signatures that drop `(scope, receive,
  send)` boilerplate when you don't need them.
- **HTTP/1.1, HTTP/2, and WebSocket** on the same listener — ALPN
  negotiates HTTP/2; cleartext h2c is detected on first preface bytes;
  WebSocket-over-HTTP/2 (RFC 8441) is available as an opt-in.
- **Standards conformance** — RFC 9112 (HTTP/1.1), RFC 9113
  (HTTP/2 — h2spec passes), RFC 6455 (WebSocket — Autobahn passes),
  RFC 8441 (Extended CONNECT for WebSocket over HTTP/2).
- **Predictable behaviour under load** — per-connection deadline
  subsystem, per-stream queue depth controls, cooperative event-loop
  yielding.
- **Pre-fork multi-worker** with `SO_REUSEPORT`, optional `uvloop`,
  hot-reload via `watchfiles`, AF_UNIX + systemd socket activation.
- **PEP 561 typed** distribution (downstream type-checkers honour
  the inline annotations).

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

- New to BlackBull? Start with [Installation](getting-started/installation.md).
- Building something? The [Guide](guide/index.md) covers routing,
  middleware, WebSockets, error handling, HTTP/2, and configuration.
- Deploying? See [Deployment](deployment/running.md) for multi-worker,
  TLS, AF_UNIX, systemd activation, and behind-nginx topologies.
- Curious about the internals? [Internals](about/internals.md) walks
  the actor model and the protocol stack.
