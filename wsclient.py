import logging.config
import json
import time
import asyncio
import ssl
import pathlib
import websockets
from blackbull.logger import get_logger_set
# from hyper import HTTPConnection

with open('logging.json', 'r') as stream:
    config = json.load(stream)
    config['handlers']['file']['filename'] = 'wsclient.log'
    config['handlers']['console']['level'] = 'INFO'

logger, log = get_logger_set()
logging.config.dictConfig(config)

print('====================== Chat Client ======================')

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

        await client.send('Bye')


# async def http2client():
#     uri = "127.0.0.1:8000"
#     ssl_context.set_alpn_protocols(['h2'])

#     with HTTPConnection(uri, secure=True, enable_push=True, ssl_context=ssl_context) as conn:
#         conn.request('post', '/http2', body=b'hello')
#         time.sleep(1)

#         for push in conn.get_pushes():  # all pushes promised before response headers
#             logger.info(push.path)

#         response = conn.get_response()
#         logger.info(response.read())

#         for push in conn.get_pushes():  # all other pushes
#             logger.info(push.path)


if __name__ == '__main__':
    asyncio.run(
        # asyncio.wait_for(
        wsclient()
        # , timeout=0.5)
    )
