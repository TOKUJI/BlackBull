import asyncio
from functools import partial
from BlackBull import BlackBull
from BlackBull.logger import get_logger_set, ColoredFormatter
logger, log = get_logger_set()

print('========================================================')
import logging
# logging.basicConfig(level=logging.DEBUG)

logger.setLevel(logging.DEBUG)

handler = logging.StreamHandler()
handler.setLevel(logging.WARNING)
cf = ColoredFormatter('%(levelname)-17s:%(name)s:%(lineno)d %(message)s')
handler.setFormatter(cf)
logger.addHandler(handler)

fh = logging.FileHandler('app.log', 'w')
fh.setLevel(logging.DEBUG)
ff = logging.Formatter('%(levelname)-17s:%(name)s:%(lineno)d %(message)s')
fh.setFormatter(ff)
logger.addHandler(fh)

app = BlackBull()

@app.route(path='/')
async def top(scope, ctx):
    message = ctx['event']
    logger.info(ctx)
    ctx['response']['text'] = 'accepted'
    logger.info(ctx)
    return ctx

@app.route(path='/favicon.ico')
def favicon(scope, ctx):
    logger.info('favicon.ico')
    return ctx


async def main():
    import server.watch
    watcher = server.watch.Watcher()
    watcher.add_watch(__file__, server.watch.force_reload(__file__))
    watcher.add_watch('data_store.py', server.watch.force_reload(__file__))
    watcher.add_watch('render.py', server.watch.force_reload(__file__))
    watcher.add_watch('server', server.watch.force_reload(__file__))

    tasks = []
    """ Create Data Base """
    tasks.append(init_db())

    tasks.append(asyncio.create_task(watcher.watch()))
    tasks.append(app.run(port=8000))

    await asyncio.gather(*tasks)


if __name__ == "__main__":
    app.run()
