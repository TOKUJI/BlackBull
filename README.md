# BlackBull

Blackbull is a Python ASGI 3.0 web framework that can run HTTP/2.0 (, websocket, and HTTP/1.0.)

# Introduction

This is a basic example to run a simple HTTP/2.0 server. Make sure that you are running Python 3.8 or later because Blackbull uses some newly developped syntax such as walrus operator. As you clearly see in the example below, Blackbull is a middleware framework.

```Python
import asyncio

from BlackBull import BlackBull
from BlackBull.response import Response

app = BlackBull()


@app.route(path='/')
async def top(scope, receive, send, next_func, **kwargs):
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

### Regular Expression

Regular expression is available if you specify URL patterns as below.

```Python
@router.route(path=r'^/test/\d+$', methods='get')
async def fn(*args, **kwargs):
    await Response(send, 'sample')
```


## Middleware

In case you use middleware stack, you have to tell it to the framework.

```python
async def test_fn1(scope, receive, send, next_):
    res = await next_(scope, receive, send)
    await Response(send, res + 'fn1')


async def test_fn2(scope, receive, send, next_):
    res = await next_(scope, receive, send)
    return res + 'fn2'


async def test_fn3(scope, receive, send, next_):
    await next_(scope, receive, send)
    return 'fn3'

app.route(methods='get', path='/home', functions=[test_fn1, test_fn2, test_fn3])
```

The first element of functions is the most outer application and the next element is called when the first application calls next_(). Note that the second argument of Response() can be str.

## URL Parameters

URL parameters are parameters that is located in path part of URL. For example, if there is a URL, "http://somewhere:port/path1/path_parameter?query+parameters", you can get parameter from path part as below.

```python
@app.route(path=r'^test/(?P<id_>\d+)$', methods=['get'])
async def fn(scope, receive, send, next_, **kwargs):
    return kwargs.pop('id_', None)
```

# Testing
This server uses pytest. Some tests depends on a sample applciation.

# Sample application

main.py runs a web server that provide basic financial ledger service. This depends on peewee, mako

# Todo

These are todo for the developper.

1. Response (JSON, HTML, Stream, WebSocket)
1. Event handling (?)
1. Server
1. Template
1. Model
1. Cacheing
