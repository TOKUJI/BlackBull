# BlackBull

BlackBull is a Python ASGI 3.0 web framework for HTTP/1.1, HTTP/2, and WebSocket — no uvicorn or starlette underneath.

- [User Guide](guide.md) — routing, middleware, WebSocket, streaming, logging
- [API Reference](api/) — generated from source docstrings

## Install

```bash
pip install .                       # core
pip install '.[compression]'        # gzip / brotli / zstandard
```

## Quick start

```python
import asyncio
from blackbull import BlackBull, Response

app = BlackBull()

@app.route(path='/')
async def hello():
    return "Hello, world!"

if __name__ == '__main__':
    asyncio.run(app.run(port=8000))
```
