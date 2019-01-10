from functools import partial

# import from this package
from .util import RouteRecord
from .logger import get_logger_set
logger, log = get_logger_set('BlackBull')

async def scheme(scope, ctx, next_):
    logger.info('in scheme({scope}, {ctx}, {next_}'.format(scope=scope, ctx=ctx, next_=next_))
    ctx = await next_(scope, ctx)

    if scope['type'] == 'http':
        message = []
        message.append({'type': 'http.response.start', 'status': 200, 'headers': [], })
        message.append({'type': 'http.response.body', 'body': ctx['response']['text'].encode(), })
        message.append({'type': 'http.disconnect', })
        ctx['response']['message'] = message
    return ctx


class BlackBull:
    def __init__(self,
                 router = RouteRecord(),
                 ):
        self._router = router
        self.stack = [scheme]

    async def use(self, fn):
        """ fn must require 3 arguments
        scope, ctx (context) and next_
        """
        self.stack.append(fn)

    def __call__(self, scope):
        def _fn(scope):
            endpoint, methods = self._router.find(scope['path'])
            logger.debug(endpoint)

            async def __fn(receive, send):
                event = await receive()
                logger.debug('_fn({}, {}) is called'.format(event, send))
                nonlocal endpoint

                for fn in self.stack:
                    endpoint = partial(fn, next_=endpoint)

                ctx = await endpoint(scope, {'event': event, 'response': {}})
                logger.debug(ctx)
                for s in ctx['response']['message']:
                    await send(s)

            return __fn
        f = _fn(scope)
        return f

    def route(self, method='GET', path='/'):
        """ set endpoint function here"""
        return self._router.route(method=method, path=path)
