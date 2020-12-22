import logging.config
import asyncio
from blackbull import BlackBull, Response, JSONResponse, WebSocketResponse
from blackbull.utils import do_nothing, Scheme, HTTPMethods
from blackbull.middlewares import websocket
from blackbull.logger import get_logger_set, ColoredFormatter
from render import render_login_page, render_dummy_page, render_table_page

logger, log = get_logger_set()
logging.config.fileConfig('logging.cfg')

print('========================================================')
app = BlackBull()


@app.route(path='/')
async def top(scope, receive, send):
    """
    The top level middleware. This middleware is the end point of the stack.
    """
    request = await receive()
    logger.info(request)
    await Response(send, render_login_page())


@app.route(path='/favicon.ico')
async def favicon(scope, receive, send):
    await Response(send, b'login is called.')


@app.route(path='/json')
async def jsonapi(scope, receive, send):
    request = await receive()
    logger.info(request)
    await JSONResponse(send, {'a': 'b'})


async def websocket_sample(scope, receive, send):
    while msg := (await receive()):
        await WebSocketResponse(send, msg)

app.route(path="/websocket", scheme=Scheme.websocket,
          functions=[websocket, websocket_sample])


@app.route(methods=[HTTPMethods.post], path='/login')
async def login(scope, receive, send):
    logger.info('login()')
    request = await receive()
    logger.info(request)
    await Response(send, b'login is called.')


@app.route_404
async def not_found(scope, receive, send):
    await Response(send, b'Not found in asgi.py.')


if __name__ == "__main__":
    try:
        asyncio.run(
            app.run(port=8000,
                    debug=True,
                    certfile='server.crt',
                    keyfile='server.key'))
    except KeyboardInterrupt:
        logger.info('Caught a keyboard interrupt.')
