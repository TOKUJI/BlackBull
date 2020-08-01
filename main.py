import asyncio
from functools import partial
import json
import logging

from BlackBull import BlackBull
from BlackBull.BlackBull import make_response_template
from BlackBull.util import parse_post_data
from BlackBull.logger import get_logger_set, ColoredFormatter, log
# logger, _ = get_logger_set()
logger = logging.getLogger()


# application local
from data_store import User, UserEncoder, Account, init_db, SessionManager
# from playhouse.shortcuts import model_to_dict, dict_to_model
from render import render_login_page, render_dummy_page, render_table_page, render_403_page
from plugin import LedgerInfo


def logger_config(logger):
    import logging
    logger.setLevel(logging.DEBUG)

    handler = logging.StreamHandler()
    handler.setLevel(logging.WARNING)
    cf = ColoredFormatter('%(levelname)-17s:%(name)s:%(lineno)d %(message)s')
    handler.setFormatter(cf)
    logger.addHandler(handler)

    fh = logging.FileHandler('app.log','w')
    fh.setLevel(logging.DEBUG)
    ff = logging.Formatter('%(levelname)-17s:%(name)s:%(lineno)d %(message)s')
    fh.setFormatter(ff)
    logger.addHandler(fh)


def server_config():
    # select * from User join where not X.isExpired()
    active_users = SessionManager.select().where(not SessionManager.isExpired(SessionManager))
    users = [u.name for u in active_users]
    logger.info(users)
    l = LedgerInfo(prefix='local.Abank',
                   currencyCode='JPY',
                   currencyScale=2,
                   connectors=users,
                   maxBalance=100000000000,
                   )
    return l


logger_config(logger)
app = BlackBull()

@app.route(path='/')
async def top(scope, ctx):
    return render_login_page()


@app.route(path='/info')
async def info(scope, ctx):
    info = server_config()
    res = make_response_template(scope)
    res['body']['body'] = json.dumps(info, cls=LedgerInfo.Encoder)
    return res

@app.route(path='/balance')
async def balance(scope, ctx):
    return ""


@app.route(path='/favicon.ico')
async def favicon(scope, ctx):
    return open('favicon.ico', 'rb').read()

@log(logger)
@app.route(method='POST', path='/login')
async def login(scope, ctx):

    try:
        d = parse_post_data(ctx['body'].decode())
        logger.debug(d)
    except:
        logger.error(f'Failed to get information of input')

    res = make_response_template(scope)

    try:
        # check if the user is in the DB
        if await User.authenticate(d['user_name'], d['user_password']):
            user = User.get(name=d['user_name'])
            res['body']['body'] = render_dummy_page(data={'user': user})

            session_id = SessionManager.register(user=user)
            res['start']['headers'].append([b'set-cookie', session_id])
            logger.debug(res)
            # Set this id to the cookie of the response.
        else:
            res['body']['body'] = render_403_page()

    except Exception as e:
        logger.error(e)
        res['body']['body'] = render_403_page()

    finally:
        logger.info(res)
        return res


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
        tasks.append(init_db())

        tasks.append(asyncio.create_task(watcher.watch()))
        tasks.append(server.run(port=8000))

        await asyncio.gather(*tasks)

    # logger_config(logger)
    asyncio.run(main(app))
    
