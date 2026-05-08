"""Typed routes — startup validation fails intentionally.

The route /double/{n:int} uses the int converter, but the handler
annotates n as str.  Router.validate() detects the mismatch via
typeguard.check_type and raises ConfigurationError before the server
accepts any connection.

Run:
    python examples/typed_routes_fail.py

Expected output:
    ConfigurationError: Route '/double/{n:int}' param 'n': converter
    'int' yields 'int' but annotation is <class 'str'>: ...
"""
import asyncio
from http import HTTPMethod

from blackbull import BlackBull
from blackbull.router import ConfigurationError

app = BlackBull()


@app.route(path='/greet/{name:str}', methods=HTTPMethod.GET)
async def greet(name: str):
    return f'Hello, {name}!'


@app.route(path='/double/{n:int}', methods=HTTPMethod.GET)
async def double(n: str):      # BUG: converter is int, annotation is str
    return f'double of {n}'


if __name__ == '__main__':
    try:
        asyncio.run(app.run(port=8000))
    except* ConfigurationError as eg:
        for exc in eg.exceptions:
            print(f'ConfigurationError: {exc}')
