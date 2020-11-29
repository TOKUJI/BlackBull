import logging
import asyncio
# import json
# from functools import partial
from BlackBull import BlackBull
from BlackBull.logger import get_logger_set, ColoredFormatter
# from playhouse.shortcuts import model_to_dict, dict_to_model
from render import render_login_page, render_dummy_page, render_table_page

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
async def top(scope, ctx):
    message = ctx['event']
    # ctx['response']['body']['body'] = render_login_page()
    return render_login_page()


@app.route(path='/favicon.ico')
async def favicon(scope, ctx):
    return ctx


@app.route(methods='POST', path='/login')
async def login(scope, ctx):
    return str(scope) + str(ctx)


if __name__ == "__main__":
    # asyncio.run(app.run(port=8000), debug=True)
    try:
        asyncio.run(app.run(port=8000))
    except KeyboardInterrupt:
        logger.info('Caught a keyboard interrupt.')
