import logging
import asyncio
import json
from blackbull import BlackBull, Response, JSONResponse, WebSocketResponse
from blackbull.utils import do_nothing, Scheme
from blackbull.logger import get_logger_set, ColoredFormatter
from render import render_login_page, render_dummy_page, render_table_page

# fileConfig('logging.conf')
logger, log = get_logger_set()

print('========================================================')
logger.setLevel(logging.DEBUG)

handler = logging.StreamHandler()
handler.setLevel(logging.WARNING)
cf = ColoredFormatter('%(levelname)-17s:%(name)s:%(lineno)d %(message)s')
handler.setFormatter(cf)
logger.addHandler(handler)

fh = logging.FileHandler('asgi.log', 'w')
fh.setLevel(logging.DEBUG)
ff = logging.Formatter('%(levelname)-17s:%(name)s:%(lineno)d %(message)s')
fh.setFormatter(ff)
logger.addHandler(fh)

app = BlackBull()


# @app.route(path='/')
# async def top(scope, receive, send):
#     """
#     The top level middleware. This middleware is the end point of the stack.
#     """
#     request = await receive()
#     logger.info(request)
#     await Response(send, render_login_page())


# @app.route(path='/favicon.ico')
# async def favicon(scope, receive, send):
#     await Response(send, b'login is called.')


# @app.route(path='/json')
# async def jsonapi(scope, receive, send):
#     request = await receive()
#     logger.info(request)
#     await JSONResponse(send, {'a': 'b'})


@app.route(path='/websocket', scheme=Scheme.websocket)
async def websocket(scope, receive, send):
    accept = {"type": "websocket.accept", "subprotocol": None}
    msg = await receive()
    await send(accept)

    while msg := (await receive()):
        if 'text' in msg:
            logger.debug(f'Got a text massage ({msg}.)')
        elif 'bytes' in msg:
            logger.debug(f'Got a byte-string massage ({msg}.)')
        else:
            logger.info('The received message does not contain any message.')
            break

        await WebSocketResponse(send, msg)

    await send({'type': 'websocket.close'})


# @app.route(methods='POST', path='/login')
# async def login(scope, receive, send):
#     logger.info('login()')
#     request = await receive()
#     logger.info(request)
#     await Response(send, b'login is called.')


# @app.route_404
# async def not_found(scope, receive, send):
#     await Response(send, b'Not found in asgi.py.')


if __name__ == "__main__":
    try:
        asyncio.run(
            app.run(port=8000,
                    debug=True,
                    certfile='server.crt',
                    keyfile='server.key'))
    except KeyboardInterrupt:
        logger.info('Caught a keyboard interrupt.')
