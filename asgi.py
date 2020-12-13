import logging
import asyncio
# import json
from BlackBull import BlackBull
from BlackBull.utils import do_nothing
from BlackBull.response import respond
from BlackBull.logger import get_logger_set, ColoredFormatter
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


@app.route(path='/')
async def top(scope, receive, send, next_func=do_nothing):
    """
    The top level middleware. This middleware is the end point of the stack.
    """
    request = await receive()
    logger.info(request)

    next_func(scope, receive, send)

    await respond(send, render_login_page())


@app.route(path='/favicon.ico')
async def favicon(scope, ctx):
    return ctx


@app.route(methods='POST', path='/login')
async def login(scope, ctx):
    return str(scope) + str(ctx)


@app.not_found
async def not_found(scope, receive, send):
    await respond(send, b'Not found in asgi.py.')


if __name__ == "__main__":
    # asyncio.run(app.run(port=8000), debug=True)
    try:
        asyncio.run(app.run(port=8000, debug=True))
    except KeyboardInterrupt:
        logger.info('Caught a keyboard interrupt.')
