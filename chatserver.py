"""
Chat Server
Examples of chat server that uses http2 or websocket.
Note that this server does not make data persistent.
"""
import logging.config
import asyncio
import json

from blackbull import BlackBull, Response, WebSocketResponse
from blackbull.utils import HTTPMethods, Scheme
from blackbull.middlewares import websocket
from blackbull.logger import get_logger_set


with open('logging.json', 'r') as stream:
    config = json.load(stream)

logger, log = get_logger_set()
logging.config.dictConfig(config)

print('====================== Chat Server ======================')
app = BlackBull()
messages = []


async def chat_websocket(scope, receive, send):
    msg = ''
    while (msg := await receive()) and msg['text'] != 'Bye':
        messages.append(msg['text'])
        await WebSocketResponse(send, msg)

    logger.info(messages)


app.route(path="/websocket", scheme=Scheme.websocket,
          functions=[websocket, chat_websocket])


@app.route(path='/http2', methods=[HTTPMethods.post])
async def chat_http2(scope, receive, send):
    await Response(send, 'Any message?', more_body=True)
    request = await receive()
    logger.warning(request)

    while request['type'] != 'http.disconnect' and request['body'] != 'Bye':
        msg = request['body']
        messages.append(msg)
        await Response(send, msg, more_body=True)

        try:
            request = await asyncio.wait_for(receive(), timeout=0.5)
            logger.warning(request)

        except asyncio.TimeoutError:
            logger.debug('Have not received any message in this second.')
            await Response(send, 'Any message?', more_body=True)

if __name__ == '__main__':
    try:
        asyncio.run(
            app.run(port=8000,
                    debug=True,
                    certfile='cert.pem',
                    keyfile='key.pem'))
    except KeyboardInterrupt:
        logger.info('Caught a keyboard interrupt.')
