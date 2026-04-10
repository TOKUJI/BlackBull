# BlackBull

Blackbull is a Python ASGI 3.0 web framework that can run HTTP/2.0 (, websocket, and HTTP/1.0.)

# Introduction

This is a basic example to run a simple HTTP/2.0 server. Make sure that you are running Python 3.8 or later because Blackbull uses some newly developped syntax such as walrus operator. As you clearly see in the example below, Blackbull is a middleware framework.

```Python
import asyncio

from blackbull import BlackBull
from blackbull.response import Response
from blackbull.utils import Scheme, HTTPMethods

app = BlackBull()


@app.route(path='/')
async def top(scope, receive, send, inner, **kwargs):
    """
    The top level middleware. This middleware does not call any inner application.
    """
    request = await receive()
    await Response(send, b'Returns text.')

if __name__ == "__main__":
    asyncio.run(app.run(port=80, debug=True))
```

# Routing

Routing can be done Flask-like syntax if you do not use middleware stack.

```python
@app.route(path='/test')
async def test_(scope, receive, send):
    await Response(send, 'sample')
```

## Regular Expression

Regular expression is available if you specify URL patterns as below.

```Python
@router.route(path=r'^/test/\d+$', methods=[HTTPMethods.get])
async def fn(*args, **kwargs):
    await Response(send, 'sample')
```


## Middleware

In case you use middleware stack, you have to tell it to the framework.

```python
async def test_fn1(scope, receive, send, inner):
    res = await inner(scope, receive, send)
    await Response(send, res + 'fn1')


async def test_fn2(scope, receive, send, inner):
    res = await inner(scope, receive, send)
    return res + 'fn2'


async def test_fn3(scope, receive, send, inner):
    await inner(scope, receive, send)
    return 'fn3'

app.route(methods=[HTTPMethods.get], path='/home', functions=[test_fn1, test_fn2, test_fn3])
```

The first element of functions is the most outer application and the next element is called when the first application calls inner(). Note that the second argument of Response() can be str.

## URL Parameters

URL parameters are parameters that is located in path part of URL. For example, if there is a URL, "http://somewhere:port/path1/1234567?query+parameters", you can get parameter(s) from path part as below.

```python
@app.route(path=r'^path1/(?P<id_>+)$', methods=[HTTPMethods.get])
async def fn(scope, receive, send, inner, **kwargs):
    return kwargs.pop('id_', None)
```

Or, this specification can be used to get value of URL parameter(s).

```python
@app.route(path=r'^path1/(?P<id_>+)$', methods=[HTTPMethods.get])
async def fn(scope, receive, send, inner, id_):
    return id_
```

# Testing

This server uses pytest.

# Sample application

asgi.py runs a web application that demonstrate basic functionalities.

# Todo

## P1 — Spec violations / breaks conformant ASGI apps

- [x] `make_sender` (HTTP/1.1): serialize ASGI event dict → HTTP/1.1 wire bytes — replaced by `HTTP1Sender` in `sender.py`
- [x] `WebsocketHandler.receive()`: emit `{"type": "websocket.connect"}` on the first call before reading any frame
- [x] `middlewares.py` `websocket()`: check `msg.get('type') != 'websocket.connect'` instead of exact dict equality
- [ ] WebSocket Ping (opcode `0x9`): immediately reply with Pong (opcode `0xA`)
- [ ] WebSocket: reject unmasked client frames (MUST per RFC 6455 §5.1)
- [ ] HTTP/2 `CONTINUATION` frame: concatenate header blocks until `END_HEADERS` flag is set on `HEADERS`
- [ ] HTTP/2: normalize header names to lowercase after HPACK decode (RFC 7540 §8.1.2)

## P2 — Important protocol features

- [ ] HTTP/1.1 Keep-Alive: loop over multiple requests on a single connection
- [ ] HTTP/1.1: handle duplicate header names as a list (multi-value headers, RFC 7230 §3.2.2)
- [ ] HTTP/2 flow control: check window size before sending `DATA`; issue `WINDOW_UPDATE` after consuming received data
- [ ] HTTP/2 `MAX_CONCURRENT_STREAMS`: send `RST_STREAM(REFUSED_STREAM)` when the limit is exceeded
- [ ] ASGI lifespan scope: dispatch `lifespan.startup` / `lifespan.shutdown` events on server start/stop
- [ ] ASGI `http.disconnect` receive event: detect client disconnect and return `{"type": "http.disconnect"}`
- [ ] Timeout handling: wrap header and body reads with `asyncio.wait_for`
- [ ] HTTP/1.1 `100 Continue`: send interim response when request includes `Expect: 100-continue`
- [ ] HTTP/2 `GOAWAY`: send on graceful shutdown (with last processed stream ID) and on protocol errors

## P3 — Features and enhancements

- [ ] HTTP/1.1 responses: automatically add `Content-Length` and `Date` headers
- [ ] `scope['root_path']`: populate from server mount path (currently commented out)
- [ ] WebSocket `Sec-WebSocket-Version: 13` validation
- [ ] WebSocket subprotocol negotiation (`Sec-WebSocket-Protocol` header)
- [ ] HTTP response compression: gzip / br based on `Accept-Encoding`
- [ ] Static file serving middleware: stream files with `Range` / `206 Partial Content` support (RFC 7233)
- [ ] mTLS: `ssl.CERT_REQUIRED` + `load_verify_locations` for client certificate authentication
- [ ] HTTP/2 server push (`PUSH_PROMISE` frame)
- [ ] HTTP/2 stream state machine: idle → open → half-closed → closed (RFC 7540 §5.1)
- [ ] HTTP/2 priority scheduling by stream weight
- [ ] WebSocket per-message deflate compression (RFC 7692)
- [ ] WebSocket fragmentation: merge continuation frames (FIN=0) into a single message
- [ ] Streaming request body: support `more_body=True` in `http.request` receive events
- [ ] ASGI `http.response.trailers` send event
- [ ] Worker processes / multiprocessing support
- [ ] Access logging

## P4 — Application framework

- [ ] Caching
- [ ] Event handling
- [ ] Asynchronous SQL ORM (asyncio support in SQLAlchemy ORM was insufficient at time of writing)
- [ ] Model layer
- [ ] Template engine
