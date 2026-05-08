"""Typed routes — startup validation passes.

Routes: GET /greet/{name:str}   → greets by name
        GET /double/{n:int}     → returns n * 2
        GET /info/{uid:uuid}    → echoes the UUID

All handler annotations match their converter types, so
Router.validate() succeeds and the server starts normally.

Run:
    python examples/typed_routes_ok.py

Try:
    curl http://localhost:8000/greet/Alice
    curl http://localhost:8000/double/21
    curl http://localhost:8000/info/12345678-1234-5678-1234-567812345678
"""
import asyncio
import uuid
from http import HTTPMethod

from blackbull import BlackBull

app = BlackBull()


@app.route(path='/greet/{name:str}', methods=HTTPMethod.GET, name='greet')
async def greet(name: str):
    return f'Hello, {name}!'


@app.route(path='/double/{n:int}', methods=HTTPMethod.GET, name='double')
async def double(n: int):
    return {'input': n, 'result': n * 2}


@app.route(path='/info/{uid:uuid}', methods=HTTPMethod.GET, name='info')
async def info(uid: uuid.UUID):
    return {'uuid': str(uid), 'version': uid.version}


if __name__ == '__main__':
    print('url_path_for examples:')
    print(' ', app.url_path_for('greet', name='Alice'))
    print(' ', app.url_path_for('double', n=21))
    asyncio.run(app.run(port=8000))
