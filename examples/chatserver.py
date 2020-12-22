"""
Chat Server
Examples of chat server that uses http2 or websocket.
"""
import logging
import asyncio

from blackbull import BlackBull, Response
from blackbull.utils import HTTPMethods
from blackbull.logger import get_logger_set, ColoredFormatter

app = BlackBull()

logger, log = get_logger_set()

print('========================================================')
logger.setLevel(logging.DEBUG)

handler = logging.StreamHandler()
handler.setLevel(logging.WARNING)
cf = ColoredFormatter('%(levelname)-17s:%(name)s:%(lineno)d %(message)s')
handler.setFormatter(cf)
logger.addHandler(handler)

fh = logging.FileHandler('chatserver.log', 'w')
fh.setLevel(logging.DEBUG)
ff = logging.Formatter('%(levelname)-17s:%(name)s:%(lineno)d %(message)s')
fh.setFormatter(ff)
logger.addHandler(fh)


@app.route(path='/http2', methods=[HTTPMethods.post])
async def chat_http2(scope, receive, send):
    while msg := (await receive()):
        await Response(send, msg)

if __name__ == '__main__':
    try:
        asyncio.run(
            app.run(port=8000,
                    debug=True,
                    certfile='server.crt',
                    keyfile='server.key'))
    except KeyboardInterrupt:
        logger.info('Caught a keyboard interrupt.')
