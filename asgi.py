import asyncio
import json
from functools import partial
from BlackBull import BlackBull
from BlackBull.logger import get_logger_set, ColoredFormatter
logger, log = get_logger_set()
# from playhouse.shortcuts import model_to_dict, dict_to_model
from render import render_login_page, render_dummy_page, render_table_page

print('========================================================')
import logging
logger.setLevel(logging.DEBUG)

handler = logging.StreamHandler()
handler.setLevel(logging.WARNING)
cf = ColoredFormatter('%(levelname)-17s:%(name)s:%(lineno)d %(message)s')
handler.setFormatter(cf)
logger.addHandler(handler)

fh = logging.FileHandler('app.log',)
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


@app.route(method='POST', path='/login')
async def login(scope, ctx):
    return str(scope) + str(ctx)


if __name__ == "__main__":
    async def main(app):
        import BlackBull.watch
        import BlackBull.server
        server = BlackBull.server.ASGIServer(app, certfile='server.csr', keyfile='server.key')
        watcher = BlackBull.watch.Watcher()

        watcher.add_watch(__file__, BlackBull.watch.force_reload(__file__))
        # watcher.add_watch('data_store.py', BlackBull.watch.force_reload(__file__))
        # watcher.add_watch('render.py', BlackBull.watch.force_reload(__file__))
        watcher.add_watch('BlackBull', BlackBull.watch.force_reload(__file__))

        tasks = []
        """ Create Data Base """
        # tasks.append(init_db())

        tasks.append(asyncio.create_task(watcher.watch()))
        tasks.append(server.run(port=8000))

        await asyncio.gather(*tasks)

    asyncio.run(main(app))  
    
