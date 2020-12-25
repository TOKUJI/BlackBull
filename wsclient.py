import asyncio
import ssl
import pathlib
import websockets
import httpx
from blackbull.logger import get_logger_set

logger, log = get_logger_set()

ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
localhost_pem = pathlib.Path(__file__).with_name("cert.pem")
ssl_context.load_verify_locations(localhost_pem)


async def wsclient():
    uri = "wss://localhost:8000/websocket"

    async with websockets.connect(uri, ssl=ssl_context) as client:

        name = 'Toshio'
        for i in range(10):
            await client.send(f'{name}{i}')
            logger.debug(f"> {name}")

            greeting = await client.recv()
            logger.debug(f"< {greeting}")


async def http2client():
    uri = "https://localhost:8000/http2"
    async with httpx.AsyncClient(http2=True, verify=False) as client:

        name = 'Toshio'
        for i in range(10):
            coro = client.post(uri, data=f'{name}{i}')
            logger.debug(f"> {name}")

            res = await coro
            logger.debug(f"< {res}")
            await asyncio.sleep(2)

        await client.post(uri, data='Bye')


if __name__ == '__main__':
    asyncio.run(
        # asyncio.wait_for(
        http2client()
        # , timeout=0.5)
    )
