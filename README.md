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

These are todos for the developper.

1. Client -> Enable Server Push.
1. Server (HTTP2, HTTP1.1, WebSocket)
1. Server push -> daphne does not yet support extend features like Server Push.
1. Static files
1. Cacheing
1. Event handling (?)
1. Asynchronous SQL ORM (Asynchronous support of SQL Alchemy ORM seems to be not sufficient.)
1. Model
1. Template
