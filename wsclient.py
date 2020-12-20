import asyncio
import ssl
import pathlib
import websockets
from blackbull.logger import get_logger_set

logger, log = get_logger_set()

ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
localhost_pem = pathlib.Path(__file__).with_name("cert.pem")
ssl_context.load_verify_locations(localhost_pem)


async def main():
    uri = "wss://localhost:8000/websocket"

    async with websockets.connect(uri, ssl=ssl_context) as client:

        name = 'Toshio'
        for i in range(1):
            await client.send(f'{name}{i}')
            logger.debug(f"> {name}")
            print(f"> {name}")

            greeting = await client.recv()
            logger.debug(f"< {greeting}")
            print(f"< {greeting}")


if __name__ == '__main__':
    asyncio.run(
        main()
        )
