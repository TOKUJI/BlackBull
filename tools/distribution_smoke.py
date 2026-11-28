"""Exercise a BlackBull installation using only its runtime dependencies."""

import asyncio
from importlib.resources import files

from blackbull import BlackBull, Connection, Headers


async def main():
    app = BlackBull()

    @app.route(path="/hello/{name}")
    async def hello(name: str):
        return f"Hello, {name}"

    path = "/hello/BlackBull"
    conn = Connection("GET", path, path.encode(), Headers([(b"host", b"localhost")]))
    responses = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(response):
        responses.append(response)

    await app(conn, receive, send)
    (response,) = responses
    assert response.status == 200
    assert response.body == b"Hello, BlackBull"
    assert files("blackbull").joinpath("py.typed").is_file()


if __name__ == "__main__":
    asyncio.run(main())
